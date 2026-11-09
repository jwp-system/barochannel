from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.deps import AuthzDep, CurrentUser, current_user
from app.core.templating import templates
from app.models import (
    Approval,
    ApprovalDispatch,
    ApprovalLineTemplate,
    ApprovalReference,
    ApprovalReview,
    ApprovalStep,
    Channel,
    ChannelDispatchTarget,
    Department,
    Notification,
    Post,
    User,
    UserDepartment,
)
from app.routers.posts import (
    Body,
    Files,
    apply_mentions,
    card,
    channel_access,
    check_upload_size,
    not_found,
    post_context,
    require_content,
    store_attachments,
)
from app.routers.spaces import REFRESH, managed_channel
from app.services import approvals as rules
from app.services.approvals import (
    Plan,
    PlanError,
    check_plan,
    default_line,
    people_rows,
    resolve_line,
    retain_until,
    search_people,
    steps_for,
    stored_plan,
)
from app.services.audit import record_audit
from app.services.dispatch import (
    auto_dispatch,
    available_targets,
    copy_attachments,
    document_body,
    receivers,
    send,
)
from app.services.navigation import build_navigation
from app.services.permissions import NO_ACCESS, Authorizer, ChannelAccess, can_view_replies
from app.services.post_views import build_views
from app.services.realtime import Event, publish, publish_counts

router = APIRouter(
    tags=["approvals"], include_in_schema=False, dependencies=[Depends(current_user)]
)

Title = Annotated[str, Form(min_length=1, max_length=200)]


def conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


@dataclass
class Document:
    post: Post
    parent: Post | None
    channel: Channel
    access: ChannelAccess
    approval: Approval
    steps: list[ApprovalStep]
    reviews: list[ApprovalReview]


async def locked_document(session: AsyncSession, authz: Authorizer, post_id: int) -> Document:
    """결재 행을 잠근 뒤 단계를 읽는다. 동시 요청은 앞선 처리가 끝난 상태를 보고 판정한다."""
    post, parent, channel, access = await post_context(session, authz, post_id, lock=True)
    return await load_document(session, post, parent, channel, access)


async def load_document(
    session: AsyncSession,
    post: Post,
    parent: Post | None,
    channel: Channel,
    access: ChannelAccess,
) -> Document:
    fresh = {"populate_existing": True}
    approval = await session.scalar(
        select(Approval)
        .where(Approval.post_id == post.id)
        .with_for_update()
        .execution_options(**fresh)
    )
    if approval is None:
        raise not_found()
    steps = await session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.approval_id == approval.id)
        .order_by(ApprovalStep.step_no)
        .execution_options(**fresh)
    )
    reviews = await session.scalars(
        select(ApprovalReview)
        .where(ApprovalReview.approval_id == approval.id)
        .execution_options(**fresh)
    )
    return Document(post, parent, channel, access, approval, list(steps), list(reviews))


def audit(session: AsyncSession, doc: Document, actor: User, action: str, **detail: object) -> None:
    record_audit(
        session,
        actor_id=actor.id,
        action=f"approval.{action}",
        target_type="approval",
        target_id=doc.approval.id,
        post_id=doc.post.id,
        **detail,
    )


def close(session: AsyncSession, doc: Document, result: str, actor: User, now: datetime) -> None:
    doc.approval.status = result
    doc.approval.closed_at = now
    doc.approval.retain_until = retain_until(now)
    if doc.approval.drafter_id != actor.id:
        session.add(
            Notification(
                user_id=doc.approval.drafter_id, type="approval_result", ref_id=doc.post.id
            )
        )


async def respond(
    session: AsyncSession, authz: Authorizer, doc: Document, notify: set[int]
) -> HTMLResponse:
    await publish(session, Event("updated", doc.channel.id, doc.post.id, authz.user.id))
    await publish_counts(session, notify | {authz.user.id})
    await session.commit()
    return await card(session, authz, doc.post, doc.parent, doc.channel, doc.access)


async def reference_document(session: AsyncSession, authz: Authorizer, post_id: int) -> Approval:
    """볼 수 있고 지워지지 않은 결재 문서만 참조한다. 지운 글의 본문이 되살아나면 안 된다."""
    post, *_ = await post_context(session, authz, post_id)
    approval = await session.scalar(select(Approval).where(Approval.post_id == post.id))
    if approval is None or post.deleted_at is not None:
        raise not_found()
    return approval


def waiting_reviewers(doc: Document) -> set[int]:
    return {review.reviewer_id for review in doc.reviews if review.result is None}


async def own_departments(session: AsyncSession, user_id: int) -> list[Department]:
    rows = await session.scalars(
        select(Department)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id)
        .order_by(UserDepartment.is_primary.desc(), Department.name)
    )
    return list(rows)


IdList = Annotated[list[int] | None, Form()]
# 작성 화면의 지정 편집기가 보내는 표시. 있으면 결재선·검토자·참조자 목록을 그대로 쓴다.
PlanMarker = Annotated[str | None, Form(alias="plan")]


async def planner_context(session: AsyncSession, channel: Channel, plan: Plan) -> dict[str, object]:
    return {
        "data": {
            "line": await people_rows(session, plan.line),
            "reviewers": await people_rows(session, plan.reviewers),
            "references": await people_rows(session, plan.references),
        },
        "people_url": f"/channels/{channel.id}/approval-people",
        "default_url": f"/channels/{channel.id}/default-line",
    }


def plan_failed(error: PlanError) -> HTTPException:
    if error.stale:
        return conflict(str(error))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(error))


async def drafting_channel(session: AsyncSession, authz: Authorizer, channel_id: int) -> Channel:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "approval" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "결재를 올릴 수 없습니다.")
    return channel


@router.get("/channels/{channel_id}/approvals/new", response_class=HTMLResponse)
async def new_approval(
    request: Request,
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    reference: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "approval" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "결재를 올릴 수 없습니다.")
    body = channel.approval_template_md or ""
    title = ""
    if reference is not None:
        source = await reference_document(session, authz, reference)
        body = (await session.get(Post, source.post_id)).body_md
        title = source.title
    departments = await own_departments(session, authz.user.id)
    line = await default_line(
        session, channel.id, departments[0].id if departments else None, authz.user.id
    )
    return templates.TemplateResponse(
        request,
        "approval_new.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
            "departments": departments,
            "body": body,
            "title_text": title or "",
            "reference": reference,
            "planner": await planner_context(session, channel, Plan(line)),
        },
    )


@router.post("/channels/{channel_id}/approvals", dependencies=[Depends(check_upload_size)])
async def create_approval(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    title: Title,
    body: Body = "",
    department_id: Annotated[int | None, Form()] = None,
    reference_post_id: Annotated[int | None, Form()] = None,
    files: Files = None,
    plan: PlanMarker = None,
    line_ids: IdList = None,
    reviewer_ids: IdList = None,
    reference_ids: IdList = None,
) -> Response:
    files = files or []
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "approval" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "결재를 올릴 수 없습니다.")
    departments = await own_departments(session, authz.user.id)
    if department_id is None and departments:
        department_id = departments[0].id
    if department_id is not None and department_id not in [row.id for row in departments]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "자신의 소속만 지정할 수 있습니다.")
    if reference_post_id is not None:
        await reference_document(session, authz, reference_post_id)
    else:
        require_content(body, files)
    chosen = None
    if plan is not None:
        try:
            chosen = await check_plan(
                session,
                channel.id,
                department_id,
                authz.user.id,
                line_ids or [],
                reviewer_ids or [],
                reference_ids or [],
            )
        except PlanError as error:
            raise plan_failed(error) from error

    post = Post(channel_id=channel.id, author_id=authz.user.id, body_md=body, is_draft=True)
    session.add(post)
    await session.flush()
    session.add(
        Approval(
            post_id=post.id,
            drafter_id=authz.user.id,
            department_id=department_id,
            title=title.strip(),
            reference_post_id=reference_post_id,
            plan=chosen.as_json() if chosen else None,
        )
    )
    await store_attachments(session, post, files)
    if reference_post_id is not None:
        await copy_attachments(session, reference_post_id, post.id)
    await session.commit()
    return Response(headers={"HX-Redirect": f"/approvals/{post.id}"})


@router.get("/approvals/{post_id}", response_class=HTMLResponse)
async def approval_page(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    post, parent, channel, access = await post_context(session, authz, post_id)
    if parent is not None or channel.type != "approval":
        raise not_found()
    (view,) = await build_views(session, [post], authz, channel, access)
    if view.approval is None:
        raise not_found()
    dispatch_targets = (
        await available_targets(session, view.approval.approval, channel.id)
        if view.approval.can_dispatch
        else []
    )
    planner = None
    if view.approval.can_submit:
        planner = await planner_context(
            session, channel, await stored_plan(session, view.approval.approval, channel.id)
        )
    await mark_reference_read(session, view.approval.approval, authz.user.id)
    targets: list[Channel] = []
    if view.approval.can_publish:
        for row in await session.scalars(
            select(Channel).where(Channel.type == "talk").order_by(Channel.name)
        ):
            if (await authz.access(row)).write:
                targets.append(row)
    return templates.TemplateResponse(
        request,
        "approval.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "todo",
            "channel": channel,
            "v": view,
            "publish_targets": targets,
            "dispatch_targets": dispatch_targets,
            "default_target": channel.default_publish_channel_id,
            "show_thread": can_view_replies(access, post, authz.user.id) and not post.is_draft,
            "planner": planner,
            # 채널 실시간 연결은 채널에 들어갈 수 있는 사람만 연다.
            # 문서만 맡은 사람(참조자, 검토자 등)은 새로고침으로 본다.
            "live_channel": (await authz.access(channel)).can_enter,
        },
    )


async def mark_reference_read(session: AsyncSession, approval: Approval, user_id: int) -> None:
    """참조자가 문서를 열면 내 할 일의 참조 목록과 개수에서 뺀다."""
    if approval.status == "draft":
        return
    result = await session.execute(
        update(ApprovalReference)
        .where(
            ApprovalReference.approval_id == approval.id,
            ApprovalReference.user_id == user_id,
            ApprovalReference.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    if result.rowcount:
        await publish_counts(session, {user_id})
        await session.commit()


@router.post("/approvals/{post_id}/submit", response_class=HTMLResponse)
async def submit(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    reviewer_ids: IdList = None,
    plan: PlanMarker = None,
    line_ids: IdList = None,
    reference_ids: IdList = None,
) -> HTMLResponse:
    """기안자 승인. 지정 편집기가 보낸 목록이 있으면 그것을, 없으면 임시저장의 지정을 쓴다."""
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if approval.drafter_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "기안자만 상신할 수 있습니다.")
    if approval.status != "draft":
        raise conflict("이미 상신한 결재입니다.")

    if plan is not None:
        line, reviewer_list, reference_list = (
            line_ids or [],
            reviewer_ids or [],
            reference_ids or [],
        )
    else:
        saved = await stored_plan(session, approval, doc.channel.id)
        line, reference_list = saved.line, saved.references
        reviewer_list = reviewer_ids if reviewer_ids is not None else saved.reviewers
    try:
        chosen = await check_plan(
            session,
            doc.channel.id,
            approval.department_id,
            user.id,
            line,
            reviewer_list,
            reference_list,
        )
    except PlanError as error:
        raise plan_failed(error) from error
    approvers, reviewers, references = chosen.line, set(chosen.reviewers), chosen.references

    now = datetime.now(UTC)
    session.add(
        ApprovalStep(
            approval_id=approval.id,
            step_no=0,
            step_type="draft",
            approver_id=user.id,
            status="approved",
            acted_at=now,
        )
    )
    session.add_all(
        ApprovalStep(approval_id=approval.id, step_no=number, approver_id=approver_id)
        for number, approver_id in enumerate(approvers, start=1)
    )
    session.add_all(
        ApprovalReview(approval_id=approval.id, reviewer_id=reviewer_id)
        for reviewer_id in reviewers
    )
    session.add_all(
        ApprovalReference(approval_id=approval.id, user_id=reference_id)
        for reference_id in references
    )
    approval.plan = chosen.as_json()
    approval.status = "in_progress"
    approval.current_step = 1
    approval.submitted_at = now
    doc.post.is_draft = False
    await session.flush()
    await apply_mentions(session, doc.post, None, doc.channel)
    audit(
        session,
        doc,
        user,
        "submit",
        approvers=approvers,
        reviewers=sorted(reviewers),
        references=references,
        department_id=approval.department_id,
        line_changed=approvers
        != await default_line(session, doc.channel.id, approval.department_id, user.id),
    )
    # 임시저장은 다른 사람의 화면에 없었으므로 새 글로도 알린다.
    await publish(session, Event("created", doc.channel.id, doc.post.id, user.id))
    return await respond(session, authz, doc, {approvers[0], *reviewers, *references})


@router.post("/approvals/{post_id}/approve", response_class=HTMLResponse)
async def approve(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if not rules.is_turn(approval, doc.steps, user.id):
        raise conflict("지금은 승인할 수 없습니다.")
    if not rules.can_approve(approval, doc.steps, doc.reviews, user.id):
        raise conflict("모든 검토가 끝나야 최종 승인할 수 있습니다.")
    now = datetime.now(UTC)
    step = rules.current(approval, doc.steps)
    step.status = "approved"
    step.acted_at = now
    audit(session, doc, user, "approve", step_no=step.step_no)
    if rules.is_final_turn(approval, doc.steps):
        close(session, doc, "approved", user, now)
        await auto_dispatch(session, approval, doc.post, doc.steps)
        return await respond(session, authz, doc, {approval.drafter_id})
    approval.current_step += 1
    upcoming = rules.current(approval, doc.steps)
    return await respond(session, authz, doc, {upcoming.approver_id})


@router.post("/approvals/{post_id}/reject", response_class=HTMLResponse)
async def reject(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    comment: Annotated[str, Form(max_length=1000)] = "",
) -> HTMLResponse:
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if not comment.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "반려 사유를 입력해 주세요.")
    if not rules.is_turn(approval, doc.steps, user.id):
        raise conflict("지금은 반려할 수 없습니다.")
    now = datetime.now(UTC)
    step = rules.current(approval, doc.steps)
    step.status = "rejected"
    step.comment = comment.strip()
    step.acted_at = now
    audit(session, doc, user, "reject", step_no=step.step_no, comment=comment.strip())
    close(session, doc, "rejected", user, now)
    return await respond(session, authz, doc, {approval.drafter_id, *waiting_reviewers(doc)})


@router.post("/approvals/{post_id}/cancel", response_class=HTMLResponse)
async def cancel_approval(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if not rules.can_cancel(approval, doc.steps, user.id):
        raise conflict("승인을 취소할 수 없습니다.")
    waiting = rules.current(approval, doc.steps)
    approval.current_step -= 1
    step = rules.current(approval, doc.steps)
    step.status = "waiting"
    step.acted_at = None
    audit(session, doc, user, "cancel", step_no=step.step_no)
    return await respond(session, authz, doc, {waiting.approver_id})


@router.post("/approvals/{post_id}/withdraw", response_class=HTMLResponse)
async def withdraw(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    now = datetime.now(UTC)
    if not rules.can_withdraw(approval, user.id, now):
        hours = get_settings().withdraw_hours
        raise conflict(
            f"기안을 취소할 수 없습니다. 상신 후 {hours}시간 이내, 최종 승인 전에만 됩니다."
        )
    waiting = rules.current(approval, doc.steps)
    audit(session, doc, user, "withdraw", step_no=approval.current_step)
    close(session, doc, "withdrawn", user, now)
    return await respond(session, authz, doc, {waiting.approver_id, *waiting_reviewers(doc)})


@router.post("/approvals/{post_id}/discard")
async def discard(
    post_id: int,
    session: SessionDep,
    user: CurrentUser,
    reason: Annotated[str, Form(max_length=1000)] = "",
) -> Response:
    """승인자가 비활성이라 멈춘 결재를 그룹 관리자가 승인도 반려도 아닌 폐기로 닫는다.

    관리자는 글 읽기 권한이 없을 수 있으므로 글 권한이 아니라 관리자 지위로 판정한다.
    """
    post = await session.get(Post, post_id, with_for_update=True)
    if post is None:
        raise not_found()
    channel = await managed_channel(session, post.channel_id, user)
    doc = await load_document(session, post, None, channel, NO_ACCESS)
    if not reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "폐기 사유를 입력해 주세요.")
    waiting = [step.approver_id for step in doc.steps if step.status == "waiting"]
    blocked = list(
        await session.scalars(select(User.id).where(User.id.in_(waiting), User.status != "active"))
    )
    if doc.approval.status != "in_progress" or not blocked:
        raise conflict("승인자가 비활성인 진행 중 결재만 폐기할 수 있습니다.")
    doc.approval.discard_reason = reason.strip()
    audit(session, doc, user, "discard", reason=reason.strip(), blocked=sorted(blocked))
    close(session, doc, "discarded", user, datetime.now(UTC))
    await publish(session, Event("updated", channel.id, post.id, user.id))
    await publish_counts(
        session, {user.id, doc.approval.drafter_id, *waiting, *waiting_reviewers(doc)}
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/approvals/{post_id}/receive", response_class=HTMLResponse)
async def receive(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    """F-66, F-67: 먼저 처리된 한 명만 성립한다. 체크한 사람이 기안자가 되고 기안자 승인이다."""
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if approval.status != "receiving":
        raise conflict("이미 수신 완료된 문서입니다.")
    access = await authz.access(doc.channel)
    if not (access.read and access.write):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "읽기와 쓰기 권한이 있는 사람만 수신할 수 있습니다."
        )
    departments = await own_departments(session, user.id)
    department_id = departments[0].id if departments else None
    line = await resolve_line(session, doc.channel.id, department_id)
    if not line:
        raise conflict("승인선이 등록되지 않은 소속은 수신 완료를 체크할 수 없습니다.")
    approvers = steps_for(line, user.id)
    active = set(
        await session.scalars(
            select(User.id).where(User.id.in_(approvers), User.status == "active")
        )
    )
    if set(approvers) - active:
        raise conflict("승인선에 비활성 사용자가 있습니다. 승인선을 고친 뒤 체크해 주세요.")

    now = datetime.now(UTC)
    eligible = await receivers(session, doc.channel)
    session.add(
        ApprovalStep(
            approval_id=approval.id,
            step_no=0,
            step_type="draft",
            approver_id=user.id,
            status="approved",
            acted_at=now,
        )
    )
    session.add_all(
        ApprovalStep(approval_id=approval.id, step_no=number, approver_id=approver_id)
        for number, approver_id in enumerate(approvers, start=1)
    )
    approval.status = "in_progress"
    approval.current_step = 1
    approval.submitted_at = now
    approval.drafter_id = user.id
    approval.department_id = department_id
    dispatch = await session.scalar(
        select(ApprovalDispatch).where(ApprovalDispatch.received_approval_id == approval.id)
    )
    dispatch.received_by = user.id
    dispatch.received_at = now
    audit(session, doc, user, "receive", approvers=approvers, department_id=department_id)
    source = await session.scalar(
        select(Post)
        .join(Approval, Approval.post_id == Post.id)
        .where(Approval.id == dispatch.source_approval_id)
    )
    await publish(session, Event("updated", source.channel_id, source.id, user.id))
    await session.flush()
    return await respond(session, authz, doc, {approvers[0], *eligible})


@router.post("/approvals/{post_id}/dispatch")
async def dispatch(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    channel_ids: Annotated[list[int] | None, Form()] = None,
) -> Response:
    """F-65: 기안자가 남은 지정 채널을 골라 보낸다. 여러 채널에 한 번에 보낼 수 있다."""
    doc = await locked_document(session, authz, post_id)
    approval = doc.approval
    if approval.status != "approved" or approval.drafter_id != authz.user.id:
        raise conflict("승인이 끝난 결재를 기안자만 발신할 수 있습니다.")
    chosen = set(channel_ids or [])
    targets = {
        channel.id: channel
        for channel, _ in await available_targets(session, approval, doc.channel.id)
    }
    if not chosen or not chosen <= set(targets):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "지정된 채널 중 아직 보내지 않은 채널만 고를 수 있습니다."
        )
    for channel_id in sorted(chosen):
        await send(session, approval, doc.post, doc.steps, targets[channel_id], authz.user.id)
    await publish(session, Event("updated", doc.channel.id, doc.post.id, authz.user.id))
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/channels/{channel_id}/dispatch-targets")
async def save_dispatch_target(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    target_channel_id: Annotated[int, Form()],
    auto_send: Annotated[bool, Form()] = False,
    remove: Annotated[bool, Form()] = False,
) -> Response:
    """F-64: 발신 채널의 그룹 관리자가 정한다. 수신 채널은 결재 채널만 된다."""
    channel = await managed_channel(session, channel_id, user)
    target = await session.get(Channel, target_channel_id)
    if channel.type != "approval" or target is None or target.type != "approval":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "결재 채널끼리만 지정할 수 있습니다.")
    if target.id == channel.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "자기 채널로는 발신할 수 없습니다.")
    row = await session.get(ChannelDispatchTarget, (channel.id, target.id))
    before = None if row is None else {"auto_send": row.auto_send}
    if remove:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(
            ChannelDispatchTarget(
                source_channel_id=channel.id, target_channel_id=target.id, auto_send=auto_send
            )
        )
    else:
        row.auto_send = auto_send
    record_audit(
        session,
        actor_id=user.id,
        action="channel.dispatch_target",
        target_type="channel",
        target_id=channel.id,
        target_channel_id=target.id,
        before=before,
        after=None if remove else {"auto_send": auto_send},
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/approvals/{post_id}/review", response_class=HTMLResponse)
async def review(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    result: Annotated[Literal["pass", "revise", "fail"], Form()],
) -> HTMLResponse:
    doc = await locked_document(session, authz, post_id)
    mine = next((row for row in doc.reviews if row.reviewer_id == authz.user.id), None)
    if mine is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "검토자가 아닙니다.")
    if doc.approval.status != "in_progress":
        raise conflict("결재가 끝나 검토 결과를 바꿀 수 없습니다.")
    audit(session, doc, authz.user, "review", before=mine.result, after=result)
    mine.result = result
    mine.reviewed_at = datetime.now(UTC)
    return await respond(session, authz, doc, set())


@router.post("/approvals/{post_id}/publish", response_class=HTMLResponse)
async def publish_approval(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    channel_id: Annotated[int, Form()],
    include_attachments: Annotated[bool, Form()] = False,
) -> Response:
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if not rules.can_publish(approval, doc.steps, user.id):
        raise conflict("게시할 수 없습니다.")
    target = await session.get(Channel, channel_id)
    if target is None or target.type != "talk" or not (await authz.access(target)).write:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "쓰기 권한이 있는 소통 채널만 고를 수 있습니다."
        )
    body = await document_body(session, approval, doc.post, doc.steps)
    published = Post(channel_id=target.id, author_id=user.id, body_md=body)
    session.add(published)
    await session.flush()
    if include_attachments:
        await copy_attachments(session, doc.post.id, published.id)
    approval.published_post_id = published.id
    audit(
        session,
        doc,
        user,
        "publish",
        channel_id=target.id,
        published_post_id=published.id,
        attachments=include_attachments,
    )
    await publish(session, Event("created", target.id, published.id, user.id))
    await publish(session, Event("updated", doc.channel.id, doc.post.id, user.id))
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/notifications/{notification_id}/read")
async def read_notification(
    notification_id: int, session: SessionDep, user: CurrentUser
) -> Response:
    notice = await session.get(Notification, notification_id)
    if notice is None or notice.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "알림을 찾을 수 없습니다.")
    if notice.read_at is None:
        notice.read_at = datetime.now(UTC)
        await publish_counts(session, [user.id])
        await session.commit()
    return Response()


@router.get("/channels/{channel_id}/approver-candidates")
async def approver_candidates(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    q: Annotated[str, Query(max_length=100)] = "",
) -> list[dict[str, object]]:
    """승인선 편집용 검색. 그룹 관리자는 채널 읽기 권한이 없어도 쓸 수 있다."""
    await managed_channel(session, channel_id, user)
    return await search_people(session, q)


@router.get("/channels/{channel_id}/approval-people")
async def approval_people(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    q: Annotated[str, Query(max_length=100)] = "",
) -> list[dict[str, object]]:
    """결재를 올릴 수 있는 사람이 결재선·검토자·참조자를 고를 때 쓴다. 자신은 빠진다."""
    await drafting_channel(session, authz, channel_id)
    return await search_people(session, q, exclude=[authz.user.id])


@router.get("/channels/{channel_id}/default-line")
async def default_line_people(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    department_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, object]]:
    """기안 소속을 바꾸면 그 소속의 등록 승인선으로 결재선을 다시 채운다."""
    channel = await drafting_channel(session, authz, channel_id)
    own = {row.id for row in await own_departments(session, authz.user.id)}
    if department_id is not None and department_id not in own:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "자신의 소속만 지정할 수 있습니다.")
    line = await default_line(session, channel.id, department_id, authz.user.id)
    return await people_rows(session, line)


@router.post("/channels/{channel_id}/approval-lines")
async def save_line(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    department_id: Annotated[int, Form()],
    approvers: Annotated[str, Form(max_length=5000)] = "",
) -> Response:
    """한 줄에 이메일 하나씩, 위에서부터 1차이고 마지막이 최종이다. 비우면 승인선을 지운다."""
    channel = await managed_channel(session, channel_id, user)
    if channel.type != "approval":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "결재 채널이 아닙니다.")
    if await session.get(Department, department_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "소속을 찾을 수 없습니다.")
    emails = [row.strip().lower() for row in approvers.replace(",", "\n").splitlines()]
    emails = [row for row in emails if row]
    if len(set(emails)) != len(emails):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "같은 사람을 두 번 넣을 수 없습니다.")
    found = dict(
        (
            await session.execute(
                select(User.email, User.id).where(User.email.in_(emails), User.status == "active")
            )
        ).all()
    )
    missing = [row for row in emails if row not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"사용자를 찾을 수 없습니다: {', '.join(missing)}"
        )
    before = list(
        await session.scalars(
            select(ApprovalLineTemplate.approver_id)
            .where(
                ApprovalLineTemplate.channel_id == channel.id,
                ApprovalLineTemplate.department_id == department_id,
            )
            .order_by(ApprovalLineTemplate.step_no)
        )
    )
    await session.execute(
        delete(ApprovalLineTemplate).where(
            ApprovalLineTemplate.channel_id == channel.id,
            ApprovalLineTemplate.department_id == department_id,
        )
    )
    session.add_all(
        ApprovalLineTemplate(
            channel_id=channel.id,
            department_id=department_id,
            step_no=number,
            approver_id=found[email],
        )
        for number, email in enumerate(emails, start=1)
    )
    record_audit(
        session,
        actor_id=user.id,
        action="approval.line_change",
        target_type="channel",
        target_id=channel.id,
        department_id=department_id,
        before=before,
        after=[found[email] for email in emails],
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/channels/{channel_id}/approval-settings")
async def save_approval_settings(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    template: Annotated[str, Form(max_length=50_000)] = "",
    default_publish_channel_id: Annotated[str, Form()] = "",
) -> Response:
    channel = await managed_channel(session, channel_id, user)
    if channel.type != "approval":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "결재 채널이 아닙니다.")
    target_id = int(default_publish_channel_id) if default_publish_channel_id.isdigit() else None
    if target_id is not None:
        target = await session.get(Channel, target_id)
        if target is None or target.type != "talk":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "소통 채널만 고를 수 있습니다.")
    channel.approval_template_md = template or None
    channel.default_publish_channel_id = target_id
    record_audit(
        session,
        actor_id=user.id,
        action="channel.update",
        target_type="channel",
        target_id=channel.id,
        after={"default_publish_channel_id": target_id, "template": bool(template)},
    )
    await session.commit()
    return Response(headers=REFRESH)

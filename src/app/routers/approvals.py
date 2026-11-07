from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.deps import AuthzDep, CurrentUser, current_user
from app.core.templating import format_time, templates
from app.models import (
    Approval,
    ApprovalLineTemplate,
    ApprovalReview,
    ApprovalStep,
    Attachment,
    Channel,
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
from app.services.approvals import resolve_line, retain_until, search_people, steps_for
from app.services.audit import record_audit
from app.services.navigation import build_navigation
from app.services.permissions import NO_ACCESS, Authorizer, ChannelAccess, can_view_replies
from app.services.post_views import build_views
from app.services.realtime import Event, publish, publish_counts
from app.services.storage import copy_file

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


async def copy_attachments(session: AsyncSession, source_post_id: int, target_post_id: int) -> None:
    """글마다 파일을 따로 둔다. 한쪽을 지워도 다른 쪽의 보존에 영향이 없다."""
    rows = await session.scalars(
        select(Attachment).where(
            Attachment.post_id == source_post_id, Attachment.deleted_at.is_(None)
        )
    )
    for row in list(rows):
        key = f"attachments/{uuid4().hex}"
        await copy_file(row.storage_key, key)
        session.add(
            Attachment(
                post_id=target_post_id,
                file_name=row.file_name,
                storage_key=key,
                size=row.size,
                mime=row.mime,
            )
        )


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
    return templates.TemplateResponse(
        request,
        "approval_new.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
            "departments": await own_departments(session, authz.user.id),
            "body": body,
            "title_text": title or "",
            "reference": reference,
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
            "default_target": channel.default_publish_channel_id,
            "show_thread": can_view_replies(access, post, authz.user.id) and not post.is_draft,
        },
    )


@router.post("/approvals/{post_id}/submit", response_class=HTMLResponse)
async def submit(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    reviewer_ids: Annotated[list[int] | None, Form()] = None,
) -> HTMLResponse:
    doc = await locked_document(session, authz, post_id)
    approval, user = doc.approval, authz.user
    if approval.drafter_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "기안자만 상신할 수 있습니다.")
    if approval.status != "draft":
        raise conflict("이미 상신한 결재입니다.")

    line = await resolve_line(session, doc.channel.id, approval.department_id)
    approvers = steps_for(line, user.id)
    active = set(
        await session.scalars(
            select(User.id).where(User.id.in_(approvers), User.status == "active")
        )
    )
    if set(approvers) - active:
        raise conflict("승인선에 비활성 사용자가 있습니다. 승인선을 고친 뒤 상신해 주세요.")

    reviewers = set(reviewer_ids or [])
    if reviewers & ({user.id} | set(approvers)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "기안자와 승인자는 검토자로 지정할 수 없습니다."
        )
    found = set(
        await session.scalars(
            select(User.id).where(User.id.in_(reviewers), User.status == "active")
        )
    )
    if found != reviewers:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "검토자를 찾을 수 없습니다.")

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
        department_id=approval.department_id,
    )
    # 임시저장은 다른 사람의 화면에 없었으므로 새 글로도 알린다.
    await publish(session, Event("created", doc.channel.id, doc.post.id, user.id))
    return await respond(session, authz, doc, {approvers[0], *reviewers})


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


def publish_body(doc: Document, names: dict[int, str]) -> str:
    last = max(step.step_no for step in doc.steps)
    line = " → ".join(
        f"{rules.step_label(step.step_no, last)} {names.get(step.approver_id, '')} "
        f"({format_time(step.acted_at)})"
        for step in doc.steps
    )
    return (
        f"## {doc.approval.title}\n\n{doc.post.body_md}\n\n---\n\n"
        f"승인선: {line}\n\n[원본 결재](/approvals/{doc.post.id})"
    )


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
    names = dict(
        (
            await session.execute(
                select(User.id, User.name).where(
                    User.id.in_([step.approver_id for step in doc.steps])
                )
            )
        ).all()
    )
    published = Post(channel_id=target.id, author_id=user.id, body_md=publish_body(doc, names))
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


@router.get("/approvals/{post_id}/reviewer-candidates")
async def reviewer_candidates(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    q: Annotated[str, Query(max_length=100)] = "",
) -> list[dict[str, object]]:
    """상신 전 기안자만 쓴다. 승인선에 있는 사람은 고를 수 없게 표시한다."""
    post, _, channel, _ = await post_context(session, authz, post_id)
    approval = await session.scalar(select(Approval).where(Approval.post_id == post.id))
    if approval is None or approval.drafter_id != authz.user.id or approval.status != "draft":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "검토자를 지정할 수 없습니다.")
    line = await resolve_line(session, channel.id, approval.department_id)
    approvers = set(steps_for(line, authz.user.id))
    people = await search_people(session, q, exclude=[authz.user.id])
    return [{**row, "blocked": "승인선에 있음" if row["id"] in approvers else ""} for row in people]


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

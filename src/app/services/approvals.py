"""결재 규칙. 버튼 표시와 서버 처리가 같은 판정 함수를 써서 어긋나지 않게 한다."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.models import (
    Approval,
    ApprovalDispatch,
    ApprovalLineTemplate,
    ApprovalReference,
    ApprovalReview,
    ApprovalStep,
    Channel,
    Department,
    Notification,
    Post,
    User,
    UserDepartment,
)
from app.models.approval import RECEIPT_LABELS, REVIEW_LABELS, STATUS_LABELS

RETENTION_YEARS = 5


def retain_until(closed_at: datetime) -> datetime:
    day = 28 if (closed_at.month, closed_at.day) == (2, 29) else closed_at.day
    return closed_at.replace(year=closed_at.year + RETENTION_YEARS, day=day)


def current(approval: Approval, steps: Sequence[ApprovalStep]) -> ApprovalStep | None:
    return next((step for step in steps if step.step_no == approval.current_step), None)


def is_final_turn(approval: Approval, steps: Sequence[ApprovalStep]) -> bool:
    return approval.current_step == max(step.step_no for step in steps)


def reviews_done(reviews: Iterable[ApprovalReview]) -> bool:
    return all(review.result is not None for review in reviews)


def is_turn(approval: Approval, steps: Sequence[ApprovalStep], user_id: int) -> bool:
    step = current(approval, steps)
    return approval.status == "in_progress" and step is not None and step.approver_id == user_id


def can_approve(
    approval: Approval,
    steps: Sequence[ApprovalStep],
    reviews: Iterable[ApprovalReview],
    user_id: int,
) -> bool:
    """검토는 최종 승인만 막는다(F-73). 1차와 중간 단계는 기다리지 않는다."""
    if not is_turn(approval, steps, user_id):
        return False
    return not is_final_turn(approval, steps) or reviews_done(reviews)


def can_cancel(approval: Approval, steps: Sequence[ApprovalStep], user_id: int) -> bool:
    """다음 승인자가 승인하기 전까지 본인 승인만 취소한다(F-61). 기안자 단계는 제외다."""
    if approval.status != "in_progress" or approval.current_step < 2:
        return False
    previous = next(step for step in steps if step.step_no == approval.current_step - 1)
    return previous.approver_id == user_id


def can_withdraw(approval: Approval, user_id: int, now: datetime) -> bool:
    return (
        approval.status == "in_progress"
        and not approval.received
        and approval.drafter_id == user_id
        and approval.submitted_at is not None
        and now < approval.submitted_at + timedelta(hours=get_settings().withdraw_hours)
    )


def can_publish(approval: Approval, steps: Sequence[ApprovalStep], user_id: int) -> bool:
    return (
        approval.status == "approved"
        and approval.published_post_id is None
        and any(step.approver_id == user_id for step in steps)
    )


async def resolve_line(
    session: AsyncSession, channel_id: int, department_id: int | None
) -> list[int]:
    """지정한 소속에 승인선이 없으면 상위 소속을 찾아 올라간다. 끝까지 없으면 빈 목록이다."""
    parents = dict((await session.execute(select(Department.id, Department.parent_id))).all())
    rows = await session.execute(
        select(ApprovalLineTemplate.department_id, ApprovalLineTemplate.approver_id)
        .where(ApprovalLineTemplate.channel_id == channel_id)
        .order_by(ApprovalLineTemplate.step_no)
    )
    lines: dict[int, list[int]] = defaultdict(list)
    for line_department, approver_id in rows:
        lines[line_department].append(approver_id)
    seen: set[int] = set()
    department = department_id
    while department is not None and department not in seen:
        if department in lines:
            return lines[department]
        seen.add(department)
        department = parents.get(department)
    return []


PEOPLE_LIMIT = 20


async def primary_departments(session: AsyncSession, user_ids: Iterable[int]) -> dict[int, str]:
    """겸직자는 주 소속을 보여준다. 주 소속 표시가 없으면 이름순으로 첫 소속이다."""
    rows = await session.execute(
        select(UserDepartment.user_id, Department.name)
        .join(Department, Department.id == UserDepartment.department_id)
        .where(UserDepartment.user_id.in_(set(user_ids)))
        .order_by(UserDepartment.is_primary, Department.name.desc())
    )
    return dict(rows.all())


async def search_people(
    session: AsyncSession, query: str, exclude: Iterable[int] = ()
) -> list[dict[str, object]]:
    """이름, 이메일, 소속으로 활성 사용자를 찾는다."""
    in_department = (
        select(UserDepartment.user_id)
        .join(Department, Department.id == UserDepartment.department_id)
        .where(Department.name.contains(query, autoescape=True))
    )
    users = list(
        await session.scalars(
            select(User)
            .where(
                User.status == "active",
                User.id.not_in(set(exclude)),
                or_(
                    User.name.contains(query, autoescape=True),
                    User.email.contains(query, autoescape=True),
                    User.id.in_(in_department),
                ),
            )
            .order_by(User.name, User.id)
            .limit(PEOPLE_LIMIT)
        )
    )
    departments = await primary_departments(session, [user.id for user in users])
    return [
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "department": departments.get(user.id, ""),
        }
        for user in users
    ]


@dataclass
class LineRow:
    department_id: int
    department_name: str
    approvers: list[dict[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return " → ".join(
            f"{row['name']} ({row['department']})" if row["department"] else row["name"]
            for row in self.approvers
        )


async def dispatch_statuses(
    session: AsyncSession, approval_ids: Sequence[int]
) -> dict[int, list[tuple[str, str]]]:
    """F-68: 발신한 문서에는 수신 채널별 처리 상태만 보여준다."""
    received = aliased(Approval)
    rows = await session.execute(
        select(ApprovalDispatch.source_approval_id, Channel.name, received.status)
        .join(Channel, Channel.id == ApprovalDispatch.target_channel_id)
        .join(received, received.id == ApprovalDispatch.received_approval_id)
        .where(ApprovalDispatch.source_approval_id.in_(approval_ids))
        .order_by(ApprovalDispatch.id)
    )
    statuses: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for source_id, channel_name, state in rows:
        statuses[source_id].append((channel_name, RECEIPT_LABELS.get(state, state)))
    return statuses


async def dispatch_origins(session: AsyncSession, approval_ids: Sequence[int]) -> dict[int, str]:
    source = aliased(Approval)
    rows = await session.execute(
        select(ApprovalDispatch.received_approval_id, Channel.name)
        .join(source, source.id == ApprovalDispatch.source_approval_id)
        .join(Post, Post.id == source.post_id)
        .join(Channel, Channel.id == Post.channel_id)
        .where(ApprovalDispatch.received_approval_id.in_(approval_ids))
    )
    return dict(rows.all())


async def line_rows(session: AsyncSession, channel_id: int) -> list[LineRow]:
    rows = (
        await session.execute(
            select(Department.id, Department.name, User.id, User.name, User.email)
            .select_from(ApprovalLineTemplate)
            .join(Department, Department.id == ApprovalLineTemplate.department_id)
            .join(User, User.id == ApprovalLineTemplate.approver_id)
            .where(ApprovalLineTemplate.channel_id == channel_id)
            .order_by(Department.name, Department.id, ApprovalLineTemplate.step_no)
        )
    ).all()
    homes = await primary_departments(session, [row[2] for row in rows])
    lines: dict[int, LineRow] = {}
    for department_id, department, user_id, name, email in rows:
        line = lines.setdefault(department_id, LineRow(department_id, department))
        line.approvers.append({"name": name, "email": email, "department": homes.get(user_id, "")})
    return list(lines.values())


def steps_for(line: Sequence[int], drafter_id: int) -> list[int]:
    """기안자는 중간 단계에서 빠지고, 최종 단계이면 상신한 뒤 직접 최종 승인한다.

    승인선을 찾지 못하면 기안자가 최종 승인자다.
    """
    if not line:
        return [drafter_id]
    return [approver_id for approver_id in line[:-1] if approver_id != drafter_id] + [line[-1]]


async def default_line(
    session: AsyncSession, channel_id: int, department_id: int | None, drafter_id: int
) -> list[int]:
    """기안 소속의 등록 승인선을 기안자 기준으로 정리한 결재선. 작성 화면의 기본값이다."""
    return steps_for(await resolve_line(session, channel_id, department_id), drafter_id)


MAX_PLAN_PEOPLE = 20


class PlanError(ValueError):
    """결재선·검토자·참조자 지정이 규칙에 맞지 않을 때. 메시지를 그대로 사용자에게 보여 준다.

    stale은 저장한 뒤에 사람이 비활성이 되는 등 상황이 바뀐 경우다.
    """

    def __init__(self, message: str, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


@dataclass
class Plan:
    """상신할 때 적용할 결재선(1차~최종 승인자 순서), 검토자, 참조자."""

    line: list[int]
    reviewers: list[int] = field(default_factory=list)
    references: list[int] = field(default_factory=list)

    def as_json(self) -> dict[str, list[int]]:
        return {"line": self.line, "reviewers": self.reviewers, "references": self.references}


def _unique(ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(ids))


async def check_plan(
    session: AsyncSession,
    channel_id: int,
    department_id: int | None,
    drafter_id: int,
    line: Sequence[int],
    reviewers: Iterable[int] = (),
    references: Iterable[int] = (),
) -> Plan:
    """기안자는 결재선을 자유롭게 고친다. 다만 다음은 지킨다.

    - 기안자는 0단계이므로 결재선에 넣지 않는다. 등록 승인선의 최종 승인자가 기안자일 때만
      기본값 그대로 마지막에 둘 수 있다(상신한 뒤 직접 최종 승인).
    - 등록 승인선이 없을 때만 승인자 없이 상신해 기안자가 최종 승인한다. 등록 승인선이 있으면
      승인자를 한 명 이상 둔다.
    - 한 사람은 승인자, 검토자, 참조자 중 하나만 맡는다.
    """
    if len(line) != len(set(line)):
        raise PlanError("결재선에 같은 사람이 두 번 있습니다.")
    reviewers, references = _unique(reviewers), _unique(references)
    if max(len(line), len(reviewers), len(references)) > MAX_PLAN_PEOPLE:
        raise PlanError(
            f"결재선, 검토자, 참조자는 각각 {MAX_PLAN_PEOPLE}명까지 지정할 수 있습니다."
        )
    default = await default_line(session, channel_id, department_id, drafter_id)
    line = list(line)
    if not line:
        if default != [drafter_id]:
            raise PlanError("승인자를 한 명 이상 지정해 주세요.")
        line = [drafter_id]
    if drafter_id in line[:-1]:
        raise PlanError("기안자는 결재선에 넣을 수 없습니다.")
    if line[-1] == drafter_id and default[-1:] != [drafter_id]:
        raise PlanError("기안자는 최종 승인자가 될 수 없습니다.")
    taken = set(line) | {drafter_id}
    if taken & set(reviewers):
        raise PlanError("기안자와 승인자는 검토자로 지정할 수 없습니다.")
    if (taken | set(reviewers)) & set(references):
        raise PlanError("기안자, 승인자, 검토자는 참조자로 지정할 수 없습니다.")
    everyone = set(line) | set(reviewers) | set(references)
    active = set(
        await session.scalars(select(User.id).where(User.id.in_(everyone), User.status == "active"))
    )
    if active != everyone:
        raise PlanError(
            "결재선이나 검토자·참조자에 없거나 비활성인 사용자가 있습니다. "
            "고친 뒤 다시 시도해 주세요.",
            stale=True,
        )
    return Plan(line, reviewers, references)


async def stored_plan(session: AsyncSession, approval: Approval, channel_id: int) -> Plan:
    """임시저장에 저장된 지정. 없으면 기안 소속의 등록 승인선이다."""
    saved = approval.plan or {}
    if "line" in saved:
        return Plan(
            list(saved["line"]), list(saved.get("reviewers", [])), list(saved.get("references", []))
        )
    line = await default_line(session, channel_id, approval.department_id, approval.drafter_id)
    return Plan(line)


async def people_rows(session: AsyncSession, user_ids: Sequence[int]) -> list[dict[str, object]]:
    """화면에서 고른 순서대로 사람 정보를 돌려준다. 작성 화면의 초기값에 쓴다."""
    users = {
        user.id: user for user in await session.scalars(select(User).where(User.id.in_(user_ids)))
    }
    departments = await primary_departments(session, user_ids)
    return [
        {
            "id": user_id,
            "name": users[user_id].name,
            "email": users[user_id].email,
            "department": departments.get(user_id, ""),
        }
        for user_id in user_ids
        if user_id in users
    ]


@dataclass
class StepView:
    label: str
    name: str
    status: str
    acted_at: datetime | None
    comment: str | None
    is_current: bool


@dataclass
class ReviewView:
    name: str
    result: str | None
    label: str
    reviewed_at: datetime | None


@dataclass
class ApprovalView:
    approval: Approval
    status_label: str
    steps: list[StepView] = field(default_factory=list)
    reviews: list[ReviewView] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    preview: list[str] = field(default_factory=list)
    can_submit: bool = False
    can_approve: bool = False
    waiting_review: bool = False
    can_reject: bool = False
    can_cancel: bool = False
    can_withdraw: bool = False
    can_review: bool = False
    can_publish: bool = False
    can_resubmit: bool = False
    can_receive: bool = False
    can_dispatch: bool = False
    origin: str | None = None
    dispatches: list[tuple[str, str]] = field(default_factory=list)


def step_label(step_no: int, last: int) -> str:
    if step_no == 0:
        return "기안"
    return "최종" if step_no == last else f"{step_no}차"


async def _names(session: AsyncSession, user_ids: Iterable[int]) -> dict[int, str]:
    rows = await session.execute(select(User.id, User.name).where(User.id.in_(set(user_ids))))
    return dict(rows.all())


async def approval_views(
    session: AsyncSession,
    post_ids: Sequence[int],
    channel_id: int,
    user_id: int,
    can_write_here: bool = False,
) -> dict[int, ApprovalView]:
    approvals = list(await session.scalars(select(Approval).where(Approval.post_id.in_(post_ids))))
    if not approvals:
        return {}
    ids = [approval.id for approval in approvals]
    dispatches = await dispatch_statuses(session, ids)
    origins = await dispatch_origins(session, ids)
    steps: dict[int, list[ApprovalStep]] = defaultdict(list)
    for step in await session.scalars(
        select(ApprovalStep).where(ApprovalStep.approval_id.in_(ids)).order_by(ApprovalStep.step_no)
    ):
        steps[step.approval_id].append(step)
    reviews: dict[int, list[ApprovalReview]] = defaultdict(list)
    for review in await session.scalars(
        select(ApprovalReview)
        .where(ApprovalReview.approval_id.in_(ids))
        .order_by(ApprovalReview.reviewer_id)
    ):
        reviews[review.approval_id].append(review)
    references: dict[int, list[int]] = defaultdict(list)
    for approval_id, reference_id in await session.execute(
        select(ApprovalReference.approval_id, ApprovalReference.user_id)
        .where(ApprovalReference.approval_id.in_(ids))
        .order_by(ApprovalReference.user_id)
    ):
        references[approval_id].append(reference_id)

    now = datetime.now(UTC)
    views: dict[int, ApprovalView] = {}
    for approval in approvals:
        own_steps, own_reviews = steps[approval.id], reviews[approval.id]
        view = ApprovalView(approval, STATUS_LABELS[approval.status])
        view.origin = origins.get(approval.id)
        view.dispatches = dispatches.get(approval.id, [])
        if approval.status == "receiving":
            # 수신 완료 체크는 그 채널의 읽기와 쓰기가 모두 있어야 한다.
            view.can_receive = can_write_here
        elif approval.status == "draft":
            if approval.drafter_id == user_id:
                view.can_submit = True
                plan = await stored_plan(session, approval, channel_id)
                names = await _names(session, plan.line + plan.references)
                view.preview = [names.get(item, "") for item in plan.line]
                view.references = [names.get(item, "") for item in plan.references]
        else:
            own_references = references[approval.id]
            names = await _names(
                session,
                [step.approver_id for step in own_steps]
                + [review.reviewer_id for review in own_reviews]
                + own_references,
            )
            view.references = [names.get(item, "") for item in own_references]
            last = max(step.step_no for step in own_steps)
            view.steps = [
                StepView(
                    step_label(step.step_no, last),
                    names.get(step.approver_id, ""),
                    step.status,
                    step.acted_at,
                    step.comment,
                    approval.status == "in_progress" and step.step_no == approval.current_step,
                )
                for step in own_steps
            ]
            view.reviews = [
                ReviewView(
                    names.get(review.reviewer_id, ""),
                    review.result,
                    REVIEW_LABELS.get(review.result or "", "검토 중"),
                    review.reviewed_at,
                )
                for review in own_reviews
            ]
            turn = is_turn(approval, own_steps, user_id)
            view.can_approve = can_approve(approval, own_steps, own_reviews, user_id)
            view.waiting_review = turn and not view.can_approve
            view.can_reject = turn
            view.can_cancel = can_cancel(approval, own_steps, user_id)
            view.can_withdraw = can_withdraw(approval, user_id, now)
            view.can_review = approval.status == "in_progress" and any(
                review.reviewer_id == user_id for review in own_reviews
            )
            view.can_publish = can_publish(approval, own_steps, user_id)
            view.can_dispatch = approval.status == "approved" and approval.drafter_id == user_id
            view.can_resubmit = (
                approval.status in ("rejected", "withdrawn", "discarded")
                and approval.drafter_id == user_id
            )
        views[approval.post_id] = view
    return views


@dataclass(frozen=True)
class ApprovalRow:
    post_id: int
    title: str
    status_label: str
    channel_name: str
    drafter_name: str
    submitted_at: datetime | None


def _row_query():
    return (
        select(Approval, Channel.name, User.name)
        .join(Post, Post.id == Approval.post_id)
        .join(Channel, Channel.id == Post.channel_id)
        .join(User, User.id == Approval.drafter_id)
    )


async def _rows(session: AsyncSession, query) -> list[ApprovalRow]:
    return [
        ApprovalRow(
            approval.post_id,
            approval.title,
            STATUS_LABELS[approval.status],
            channel_name,
            drafter_name,
            approval.submitted_at,
        )
        for approval, channel_name, drafter_name in await session.execute(query)
    ]


LIST_LIMIT = 30


async def my_turn(session: AsyncSession, user_id: int) -> list[ApprovalRow]:
    return await _rows(
        session,
        _row_query()
        .join(
            ApprovalStep,
            (ApprovalStep.approval_id == Approval.id)
            & (ApprovalStep.step_no == Approval.current_step),
        )
        .where(Approval.status == "in_progress", ApprovalStep.approver_id == user_id)
        .order_by(Approval.submitted_at),
    )


async def my_reviews(session: AsyncSession, user_id: int) -> list[ApprovalRow]:
    return await _rows(
        session,
        _row_query()
        .join(ApprovalReview, ApprovalReview.approval_id == Approval.id)
        .where(
            Approval.status == "in_progress",
            ApprovalReview.reviewer_id == user_id,
            ApprovalReview.result.is_(None),
        )
        .order_by(Approval.submitted_at),
    )


async def my_references(session: AsyncSession, user_id: int) -> list[ApprovalRow]:
    """참조로 지정되었고 아직 열어 보지 않은 결재. 열면 목록과 개수에서 빠진다."""
    return await _rows(
        session,
        _row_query()
        .join(ApprovalReference, ApprovalReference.approval_id == Approval.id)
        .where(
            ApprovalReference.user_id == user_id,
            ApprovalReference.read_at.is_(None),
            Approval.status != "draft",
            Post.deleted_at.is_(None),
        )
        .order_by(Approval.submitted_at.desc()),
    )


async def my_drafts(session: AsyncSession, user_id: int) -> list[ApprovalRow]:
    return await _rows(
        session,
        _row_query()
        .where(Approval.drafter_id == user_id, Post.deleted_at.is_(None))
        .order_by(Approval.id.desc())
        .limit(LIST_LIMIT),
    )


async def my_handled(session: AsyncSession, user_id: int) -> list[ApprovalRow]:
    return await _rows(
        session,
        _row_query()
        .join(ApprovalStep, ApprovalStep.approval_id == Approval.id)
        .where(
            ApprovalStep.approver_id == user_id,
            ApprovalStep.step_type == "approve",
            ApprovalStep.status != "waiting",
        )
        .order_by(ApprovalStep.acted_at.desc())
        .limit(LIST_LIMIT),
    )


@dataclass(frozen=True)
class ResultNotice:
    id: int
    post_id: int
    title: str
    status_label: str


async def unread_notices(session: AsyncSession, user_id: int) -> list[ResultNotice]:
    rows = await session.execute(
        select(Notification, Approval)
        .join(Approval, Approval.post_id == Notification.ref_id)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .order_by(Notification.id.desc())
    )
    return [
        ResultNotice(notice.id, approval.post_id, approval.title, STATUS_LABELS[approval.status])
        for notice, approval in rows
    ]


@dataclass
class StalledDocument:
    """승인자가 비활성이라 진행할 수 없는 결재. 제목과 승인자만 담고 본문은 싣지 않는다."""

    post_id: int
    title: str
    channel_id: int
    channel_name: str
    drafter_name: str
    blocked_names: list[str] = field(default_factory=list)


async def stalled_documents(session: AsyncSession, *conditions) -> list[StalledDocument]:
    drafter = aliased(User)
    approver = aliased(User)
    rows = await session.execute(
        select(Approval, Channel, drafter.name, approver.name)
        .join(Post, Post.id == Approval.post_id)
        .join(Channel, Channel.id == Post.channel_id)
        .join(drafter, drafter.id == Approval.drafter_id)
        .join(ApprovalStep, ApprovalStep.approval_id == Approval.id)
        .join(approver, approver.id == ApprovalStep.approver_id)
        .where(
            Approval.status == "in_progress",
            ApprovalStep.status == "waiting",
            approver.status != "active",
            *conditions,
        )
        .order_by(Approval.submitted_at, ApprovalStep.step_no)
    )
    documents: dict[int, StalledDocument] = {}
    for approval, channel, drafter_name, approver_name in rows:
        document = documents.setdefault(
            approval.post_id,
            StalledDocument(
                approval.post_id, approval.title, channel.id, channel.name, drafter_name
            ),
        )
        document.blocked_names.append(approver_name)
    return list(documents.values())


async def stalled_in_groups(
    session: AsyncSession, group_ids: Iterable[int]
) -> list[StalledDocument]:
    groups = set(group_ids)
    return await stalled_documents(session, Channel.group_id.in_(groups)) if groups else []

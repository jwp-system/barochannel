"""엑셀로 채널과 권한을 한꺼번에 등록한다.

채널과 권한은 그룹 관리자의 일이므로 관리하는 그룹 안에서만 바꾼다.
개발용 샘플 데이터만 모든 그룹에 넣는다. 규칙은 조직 일괄 등록과 같다.
모두 검사한 뒤 문제가 없을 때만 반영하고, 파일에 없는 항목은 지우지 않는다.
"""

from dataclasses import dataclass
from functools import partial

from anyio import to_thread
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CHANNEL_TYPES,
    Channel,
    Department,
    GroupManager,
    PermissionGrant,
    User,
    WorkspaceGroup,
)
from app.services.audit import record_audit
from app.services.bulk_import import (
    NAME_MAX,
    VIA,
    DepartmentIndex,
    Importer,
    ImportReport,
    department_paths,
    format_path,
    parse_path,
)
from app.services.permissions import PERMISSIONS, normalize_grant
from app.services.spreadsheet import Row, SheetSpec, build_workbook

CHANNEL_SHEET = "채널"
GRANT_SHEET = "권한"
CHANNEL_COLUMNS = ("그룹", "채널", "유형", "순서", "고정 글 수")
PERMISSION_COLUMNS = dict(zip(("읽기", "쓰기", "답글 읽기", "답글 쓰기"), PERMISSIONS, strict=True))
GRANT_COLUMNS = ("그룹", "채널", "대상", *PERMISSION_COLUMNS)
CHANNEL_SPECS = {
    CHANNEL_SHEET: SheetSpec(CHANNEL_COLUMNS, ("그룹", "채널")),
    GRANT_SHEET: SheetSpec(GRANT_COLUMNS, ("그룹", "대상")),
}
TYPE_BY_LABEL = {label: key for key, label in CHANNEL_TYPES.items()}
SETTINGS = {"허용": "allow", "거부": "deny", "": None}
SETTING_LABELS = {"allow": "허용", "deny": "거부", None: ""}
PIN_MAX = 50
ORDER_MAX = 10000

ChannelKey = tuple[int, str]


def _number(text: str, upper: int) -> tuple[int | None, bool]:
    """빈 칸은 (None, True), 숫자가 아니거나 범위를 벗어나면 (None, False)."""
    if not text:
        return None, True
    if not text.isdigit() or int(text) > upper:
        return None, False
    return int(text), True


@dataclass
class ChannelPlan:
    group: WorkspaceGroup
    name: str
    existing: Channel | None
    type: str
    sort_order: int | None
    pin_limit: int | None
    channel: Channel | None = None


@dataclass
class GrantPlan:
    group: WorkspaceGroup
    channel_key: ChannelKey | None
    subject_type: str
    subject_id: int
    values: dict[str, str | None]
    existing: PermissionGrant | None


class Scope:
    """편집할 수 있는 그룹과 그 안의 채널. 이름이 겹치면 고를 수 없다."""

    def __init__(self, groups: list[WorkspaceGroup], channels: list[Channel]) -> None:
        self.groups: dict[str, list[WorkspaceGroup]] = {}
        for group in groups:
            self.groups.setdefault(group.name, []).append(group)
        self.channels: dict[ChannelKey, list[Channel]] = {}
        for channel in channels:
            self.channels.setdefault((channel.group_id, channel.name), []).append(channel)

    def group(self, name: str) -> tuple[WorkspaceGroup | None, str | None]:
        if not name:
            return None, "그룹을 적어 주세요."
        matches = self.groups.get(name, [])
        if not matches:
            return None, f"관리하는 그룹이 아닙니다: {name}"
        if len(matches) > 1:
            return None, f"'{name}' 그룹이 둘 이상이라 고를 수 없습니다."
        return matches[0], None


async def _scope(session: AsyncSession, group_ids: set[int] | None) -> Scope:
    query = select(WorkspaceGroup).where(WorkspaceGroup.archived_at.is_(None))
    if group_ids is not None:
        query = query.where(WorkspaceGroup.id.in_(group_ids))
    groups = list(await session.scalars(query))
    channels = list(
        await session.scalars(
            select(Channel).where(Channel.group_id.in_([group.id for group in groups]))
        )
    )
    return Scope(groups, channels)


def _channel_text(group: WorkspaceGroup, name: str | None) -> str:
    return f"{group.name} > {name}" if name else f"{group.name} (그룹 전체)"


def _check_channels(
    rows: list[Row], scope: Scope, report: ImportReport
) -> dict[ChannelKey, ChannelPlan]:
    plans: dict[ChannelKey, ChannelPlan] = {}
    for row in rows:
        problems: list[str] = []
        group, problem = scope.group(row.get("그룹"))
        if problem:
            problems.append(problem)
        name = row.get("채널")
        if not name or len(name) > NAME_MAX:
            problems.append(f"채널 이름은 1~{NAME_MAX}자로 적어 주세요.")
        label = row.get("유형")
        if label and label not in TYPE_BY_LABEL:
            problems.append(f"유형은 {', '.join(TYPE_BY_LABEL)} 중 하나로 적어 주세요.")
        order, order_ok = _number(row.get("순서"), ORDER_MAX)
        if not order_ok:
            problems.append(f"순서는 0~{ORDER_MAX} 사이의 숫자로 적어 주세요.")
        pin_limit, pin_ok = _number(row.get("고정 글 수"), PIN_MAX)
        if not pin_ok:
            problems.append(f"고정 글 수는 0~{PIN_MAX} 사이의 숫자로 적어 주세요.")

        existing = None
        if group is not None and name:
            key = (group.id, name)
            matches = scope.channels.get(key, [])
            if key in plans:
                problems.append("파일 안에 같은 채널이 또 있습니다.")
            elif len(matches) > 1:
                problems.append("같은 이름의 채널이 둘 이상이라 고를 수 없습니다.")
            existing = matches[0] if matches else None
            if existing is None and not label:
                problems.append("새 채널은 유형을 적어 주세요.")
            if existing is not None and label and TYPE_BY_LABEL.get(label) != existing.type:
                problems.append("채널 유형은 바꿀 수 없습니다.")
        for problem in problems:
            report.error(CHANNEL_SHEET, row, problem)
        if problems or group is None:
            continue

        channel_type = existing.type if existing else TYPE_BY_LABEL[label]
        plans[(group.id, name)] = ChannelPlan(group, name, existing, channel_type, order, pin_limit)
        if existing is None:
            report.change("채널 추가", f"{_channel_text(group, name)} ({label})")
            continue
        changed = [
            text
            for text, differs in (
                ("순서", order is not None and order != existing.sort_order),
                ("고정 글 수", pin_limit is not None and pin_limit != existing.pin_limit),
            )
            if differs
        ]
        if changed:
            report.change("채널 변경", f"{_channel_text(group, name)} · {', '.join(changed)}")
        else:
            report.unchanged()
    return plans


async def _check_grants(
    session: AsyncSession,
    rows: list[Row],
    scope: Scope,
    channel_plans: dict[ChannelKey, ChannelPlan],
    report: ImportReport,
) -> list[GrantPlan]:
    index = DepartmentIndex(await session.scalars(select(Department)))
    users = {item.email: item for item in await session.scalars(select(User))}
    existing: dict[tuple[str, int, str, int], PermissionGrant] = {
        (grant.scope_type, grant.scope_id, grant.subject_type, grant.subject_id): grant
        for grant in await session.scalars(select(PermissionGrant))
    }
    plans: list[GrantPlan] = []
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        problems: list[str] = []
        group, problem = scope.group(row.get("그룹"))
        if problem:
            problems.append(problem)

        channel_name = row.get("채널") or None
        channel_key: ChannelKey | None = None
        channel: Channel | None = None
        if group is not None and channel_name:
            channel_key = (group.id, channel_name)
            matches = scope.channels.get(channel_key, [])
            if channel_key in channel_plans:
                channel = channel_plans[channel_key].existing
            elif len(matches) == 1:
                channel = matches[0]
            elif matches:
                problems.append("같은 이름의 채널이 둘 이상이라 고를 수 없습니다.")
            else:
                problems.append(f"없는 채널입니다: {channel_name} ('채널' 시트에 추가해 주세요)")

        subject_text = row.get("대상")
        subject: tuple[str, int, str] | None = None
        if "@" in subject_text:
            user = users.get(subject_text.lower())
            if user is None:
                problems.append(f"없는 사용자입니다: {subject_text}")
            else:
                subject = ("user", user.id, user.email)
        else:
            path = parse_path(subject_text)
            if path is None:
                problems.append(
                    "대상은 사용자 이메일이나 '본사 > 개발팀' 같은 소속으로 적어 주세요."
                )
            elif (problem := index.problem(path)) is not None:
                problems.append(problem)
            elif path not in index.ids:
                problems.append(f"없는 소속입니다: {format_path(path)}")
            else:
                subject = ("department", index.id(path), format_path(path))

        values: dict[str, str | None] = {}
        for column, permission in PERMISSION_COLUMNS.items():
            text = row.get(column)
            if text not in SETTINGS:
                problems.append(f"{column} 칸은 '허용', '거부' 또는 빈 칸으로 적어 주세요.")
            else:
                values[permission] = SETTINGS[text]

        if group is not None and subject is not None:
            key = (group.id, channel_name, subject[0], subject[1])
            if key in seen:
                problems.append("파일 안에 같은 대상의 권한이 또 있습니다.")
            seen.add(key)
        for problem in problems:
            report.error(GRANT_SHEET, row, problem)
        if problems or group is None or subject is None:
            continue

        values = normalize_grant(values)
        scope_type, scope_id = ("channel", channel.id) if channel else ("group", group.id)
        current = None
        if channel_key is None or channel is not None:
            current = existing.get((scope_type, scope_id, subject[0], subject[1]))
        plans.append(GrantPlan(group, channel_key, subject[0], subject[1], values, current))

        before = (
            {name: getattr(current, f"can_{name}") for name in PERMISSIONS} if current else None
        )
        empty = all(value is None for value in values.values())
        text = f"{_channel_text(group, channel_name)} · {subject[2]}"
        if empty and current is not None:
            report.change("권한 삭제", text)
        elif empty or before == values:
            report.unchanged()
        else:
            label = "권한 변경" if current else "권한 추가"
            settings = ", ".join(
                f"{column} {SETTING_LABELS[values[permission]]}"
                for column, permission in PERMISSION_COLUMNS.items()
                if values[permission]
            )
            report.change(label, f"{text} · {settings}")
    return plans


async def import_channels(
    session: AsyncSession,
    actor: User,
    sheets: dict[str, list[Row] | None],
    apply: bool,
    group_ids: set[int] | None = None,
) -> ImportReport:
    """group_ids가 None이면 모든 그룹을 다룬다. 샘플 데이터만 그렇게 부른다."""
    report = ImportReport()
    channel_rows, grant_rows = sheets.get(CHANNEL_SHEET), sheets.get(GRANT_SHEET)
    if channel_rows is None and grant_rows is None:
        report.errors.append("'채널' 또는 '권한' 시트가 필요합니다.")
        return report
    scope = await _scope(session, group_ids)
    channel_plans = _check_channels(channel_rows or [], scope, report)
    grant_plans = await _check_grants(session, grant_rows or [], scope, channel_plans, report)
    if report.errors or not apply:
        return report

    last = dict(
        (
            await session.execute(
                select(Channel.group_id, func.max(Channel.sort_order)).group_by(Channel.group_id)
            )
        ).all()
    )
    for plan in channel_plans.values():
        await _apply_channel(session, actor, plan, last)
    for plan in grant_plans:
        await _apply_grant(session, actor, plan, channel_plans, scope)
    await session.flush()
    report.applied = True
    return report


async def _apply_channel(
    session: AsyncSession, actor: User, plan: ChannelPlan, last: dict[int, int | None]
) -> None:
    channel = plan.existing
    if channel is None:
        order = plan.sort_order
        if order is None:
            order = (last.get(plan.group.id) or 0) + 1
        last[plan.group.id] = max(order, last.get(plan.group.id) or 0)
        channel = Channel(group_id=plan.group.id, type=plan.type, name=plan.name, sort_order=order)
        if plan.pin_limit is not None:
            channel.pin_limit = plan.pin_limit
        session.add(channel)
        await session.flush()
        record_audit(
            session,
            actor_id=actor.id,
            action="channel.create",
            target_type="channel",
            target_id=channel.id,
            name=channel.name,
            type=channel.type,
            via=VIA,
        )
    else:
        before = {"sort_order": channel.sort_order, "pin_limit": channel.pin_limit}
        if plan.sort_order is not None:
            channel.sort_order = plan.sort_order
        if plan.pin_limit is not None:
            channel.pin_limit = plan.pin_limit
        after = {"sort_order": channel.sort_order, "pin_limit": channel.pin_limit}
        if after != before:
            record_audit(
                session,
                actor_id=actor.id,
                action="channel.update",
                target_type="channel",
                target_id=channel.id,
                before=before,
                after=after,
                via=VIA,
            )
    plan.channel = channel


async def _apply_grant(
    session: AsyncSession,
    actor: User,
    plan: GrantPlan,
    channel_plans: dict[ChannelKey, ChannelPlan],
    scope: Scope,
) -> None:
    if plan.channel_key is None:
        scope_type, scope_id = "group", plan.group.id
    else:
        planned = channel_plans.get(plan.channel_key)
        channel = planned.channel if planned else scope.channels[plan.channel_key][0]
        scope_type, scope_id = "channel", channel.id
    grant = plan.existing
    before = {name: getattr(grant, f"can_{name}") for name in PERMISSIONS} if grant else None
    if all(value is None for value in plan.values.values()):
        if grant is None:
            return
        await session.delete(grant)
    else:
        if before == plan.values:
            return
        if grant is None:
            grant = PermissionGrant(
                scope_type=scope_type,
                scope_id=scope_id,
                subject_type=plan.subject_type,
                subject_id=plan.subject_id,
            )
            session.add(grant)
        for name, value in plan.values.items():
            setattr(grant, f"can_{name}", value)
    record_audit(
        session,
        actor_id=actor.id,
        action="permission.change",
        target_type=scope_type,
        target_id=scope_id,
        subject=f"{plan.subject_type}:{plan.subject_id}",
        before=before,
        after=plan.values,
        via=VIA,
    )


def channel_importer(group_ids: set[int]) -> Importer:
    return partial(import_channels, group_ids=group_ids)


async def managed_group_ids(session: AsyncSession, user: User) -> set[int]:
    return set(
        await session.scalars(
            select(GroupManager.group_id)
            .join(WorkspaceGroup, WorkspaceGroup.id == GroupManager.group_id)
            .where(GroupManager.user_id == user.id, WorkspaceGroup.archived_at.is_(None))
        )
    )


CHANNEL_GUIDE = [
    "채널·권한 일괄 등록 안내",
    "· 관리하는 그룹에만 쓸 수 있습니다. 그룹은 이름으로, 채널은 그룹과 채널 이름으로 찾습니다.",
    "· '채널' 시트: 없는 채널은 새로 만들고, 있는 채널은 적은 칸(순서, 고정 글 수)만 바꿉니다. "
    "유형(소통, 투표, 문서, 결재)은 새 채널에만 필요하며 만든 뒤에는 바꿀 수 없습니다.",
    "· 채널 이름 변경과 결재 채널의 승인선·발신 설정은 채널 설정 화면에서 합니다.",
    "· '권한' 시트: 채널 칸을 비우면 그룹 전체 권한입니다. "
    "대상은 사용자 이메일이나 '본사 > 개발팀' 같은 소속 경로로 적습니다.",
    "· 읽기, 쓰기, 답글 읽기, 답글 쓰기는 '허용', '거부' 또는 빈 칸(미지정)입니다. "
    "한 행이 그 대상의 권한 전체를 정하며, 네 칸을 모두 비우면 그 권한을 지웁니다.",
    "· 답글 쓰기를 허용하면 답글 읽기도 허용되는 등 화면과 같은 규칙으로 맞춰 저장합니다.",
    "· 파일에 없는 채널과 권한은 지우지 않습니다.",
    "· 한 행이라도 문제가 있으면 아무것도 반영하지 않습니다. 먼저 '미리 확인'으로 점검하세요.",
]


async def export_channels(session: AsyncSession, group_ids: set[int]) -> bytes:
    groups = {
        group.id: group
        for group in await session.scalars(
            select(WorkspaceGroup).where(WorkspaceGroup.id.in_(group_ids))
        )
    }
    channels = list(
        await session.scalars(
            select(Channel)
            .where(Channel.group_id.in_(group_ids))
            .order_by(Channel.group_id, Channel.sort_order, Channel.id)
        )
    )
    channel_rows = [
        [
            groups[channel.group_id].name,
            channel.name,
            CHANNEL_TYPES[channel.type],
            channel.sort_order,
            channel.pin_limit,
        ]
        for channel in sorted(channels, key=lambda item: groups[item.group_id].name)
    ]
    by_id = {channel.id: channel for channel in channels}
    paths = department_paths(await session.scalars(select(Department)))
    emails = dict((await session.execute(select(User.id, User.email))).all())
    grant_rows = []
    grants = await session.scalars(
        select(PermissionGrant).order_by(PermissionGrant.scope_type.desc(), PermissionGrant.id)
    )
    for grant in grants:
        if grant.scope_type == "group":
            if grant.scope_id not in groups:
                continue
            group, channel_name = groups[grant.scope_id], ""
        else:
            channel = by_id.get(grant.scope_id)
            if channel is None:
                continue
            group, channel_name = groups[channel.group_id], channel.name
        if grant.subject_type == "user":
            subject = emails.get(grant.subject_id, "")
        else:
            subject = format_path(paths.get(grant.subject_id, ()))
        grant_rows.append(
            [
                group.name,
                channel_name,
                subject,
                *(SETTING_LABELS[getattr(grant, f"can_{name}")] for name in PERMISSIONS),
            ]
        )
    grant_rows.sort(key=lambda row: (row[0], row[1] != "", row[1]))
    return await to_thread.run_sync(
        build_workbook,
        [
            (CHANNEL_SHEET, list(CHANNEL_COLUMNS), channel_rows),
            (GRANT_SHEET, list(GRANT_COLUMNS), grant_rows),
        ],
        CHANNEL_GUIDE,
    )

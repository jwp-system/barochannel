"""엑셀로 조직과 그룹을 한꺼번에 등록한다. 개발용 샘플 데이터도 같은 규칙으로 넣는다.

먼저 모든 행을 검사하고, 문제가 하나라도 있으면 아무것도 바꾸지 않는다.
기존 항목은 비워 둔 칸을 그대로 두고, 적은 칸만 바꾼다. 파일에 없는 항목은 지우지 않는다.
"""

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from anyio import to_thread
from fastapi import Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import Department, GroupManager, User, UserDepartment, WorkspaceGroup
from app.services.admin import is_admin_email
from app.services.audit import record_audit
from app.services.spreadsheet import (
    XLSX_MEDIA_TYPE,
    Choice,
    Row,
    SheetError,
    SheetSpec,
    build_workbook,
    read_workbook,
)

DEPARTMENT_SHEET = "소속"
USER_SHEET = "사용자"
GROUP_SHEET = "그룹"
USER_COLUMNS = ("이름", "이메일", "소속", "주 소속", "상태", "초기 비밀번호")
GROUP_COLUMNS = ("그룹", "관리자", "상태")
ORG_SPECS = {
    DEPARTMENT_SHEET: SheetSpec(("소속",), ("소속",)),
    USER_SHEET: SheetSpec(USER_COLUMNS, ("이름", "이메일")),
}
GROUP_SPECS = {GROUP_SHEET: SheetSpec(GROUP_COLUMNS, ("그룹",))}

PATH_SEPARATOR = ">"
USER_STATUS = {"사용": "active", "비활성": "inactive"}
USER_STATUS_LABEL = {value: label for label, value in USER_STATUS.items()}
GROUP_ARCHIVED = {"사용": False, "보관": True}
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
LIST_SPLIT = re.compile(r"[;\n]")
EMAIL_SPLIT = re.compile(r"[;,\s]+")
MIN_PASSWORD = 8
NAME_MAX = 100
VIA = "excel"

Path = tuple[str, ...]


@dataclass
class ImportReport:
    applied: bool = False
    errors: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def error(self, sheet: str, row: Row, message: str) -> None:
        self.errors.append(f"{sheet} 시트 {row.number}행: {message}")

    def change(self, label: str, text: str) -> None:
        self.counts[label] = self.counts.get(label, 0) + 1
        self.changes.append(f"{label}: {text}")

    def unchanged(self) -> None:
        self.counts["변경 없음"] = self.counts.get("변경 없음", 0) + 1


def format_path(path: Path) -> str:
    return f" {PATH_SEPARATOR} ".join(path)


def parse_path(text: str) -> Path | None:
    parts = tuple(part.strip() for part in text.split(PATH_SEPARATOR))
    if not text.strip() or any(not part or len(part) > NAME_MAX for part in parts):
        return None
    return parts


def split_list(text: str) -> list[str]:
    return [item.strip() for item in LIST_SPLIT.split(text) if item.strip()]


def department_paths(departments: Iterable[Department]) -> dict[int, Path]:
    by_id = {department.id: department for department in departments}
    paths: dict[int, Path] = {}
    for department in by_id.values():
        parts: list[str] = []
        current: Department | None = department
        seen: set[int] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            parts.append(current.name)
            current = by_id.get(current.parent_id) if current.parent_id else None
        paths[department.id] = tuple(reversed(parts))
    return paths


class DepartmentIndex:
    """경로로 소속을 찾는다. 이름은 겹칠 수 있으므로 같은 경로가 둘이면 고를 수 없다."""

    def __init__(self, departments: Iterable[Department]) -> None:
        self.path_of = department_paths(departments)
        self.ids: dict[Path, list[int]] = {}
        for department_id, path in self.path_of.items():
            self.ids.setdefault(path, []).append(department_id)
        self.planned: list[Path] = []

    def problem(self, path: Path) -> str | None:
        for size in range(1, len(path) + 1):
            if len(self.ids.get(path[:size], [])) > 1:
                return f"'{format_path(path[:size])}' 소속이 둘 이상이라 고를 수 없습니다."
        return None

    def known(self, path: Path) -> bool:
        return path in self.ids or path in self.planned

    def plan(self, path: Path) -> list[Path]:
        """없는 상위 소속까지 만들 목록에 넣고, 새로 넣은 경로를 돌려준다."""
        added = []
        for size in range(1, len(path) + 1):
            prefix = path[:size]
            if not self.known(prefix):
                self.planned.append(prefix)
                added.append(prefix)
        return added

    def id(self, path: Path) -> int:
        return self.ids[path][0]


@dataclass
class UserPlan:
    row: Row
    email: str
    name: str
    existing: User | None
    status: str | None
    departments: list[Path] | None
    primary: Path | None
    password: str


def _check_user_row(
    row: Row,
    index: DepartmentIndex,
    users: dict[str, User],
    memberships: dict[int, list[UserDepartment]],
    seen: set[str],
) -> tuple[UserPlan | None, list[str]]:
    problems: list[str] = []
    email = row.get("이메일").lower()
    name = row.get("이름")
    if not EMAIL.fullmatch(email) or len(email) > 255:
        problems.append("이메일 형식이 올바르지 않습니다.")
    elif email in seen:
        problems.append("파일 안에 같은 이메일이 또 있습니다.")
    elif is_admin_email(email):
        problems.append("시스템 관리자 계정은 올릴 수 없습니다.")
    seen.add(email)
    if not name or len(name) > NAME_MAX:
        problems.append(f"이름은 1~{NAME_MAX}자로 적어 주세요.")

    label = row.get("상태")
    if label and label not in USER_STATUS:
        problems.append("상태는 '사용' 또는 '비활성'으로 적어 주세요.")

    existing = users.get(email)
    departments: list[Path] | None = None
    if row.get("소속"):
        departments = []
        for text in split_list(row.get("소속")):
            path = parse_path(text)
            if path is None:
                problems.append(
                    f"소속 '{text}'를 읽을 수 없습니다. '본사 > 개발팀'처럼 적어 주세요."
                )
            elif (problem := index.problem(path)) is not None:
                problems.append(problem)
            elif not index.known(path):
                problems.append(
                    f"없는 소속입니다: {format_path(path)} ('소속' 시트에 추가해 주세요)"
                )
            elif path not in departments:
                departments.append(path)

    primary: Path | None = None
    if row.get("주 소속"):
        primary = parse_path(row.get("주 소속"))
        if departments is not None:
            choices = departments
        elif existing is not None:
            choices = [
                index.path_of[link.department_id] for link in memberships.get(existing.id, [])
            ]
        else:
            choices = []
        if primary not in choices:
            problems.append("주 소속은 그 사용자의 소속 중 하나여야 합니다.")

    password = row.get("초기 비밀번호")
    if existing is None and len(password) < MIN_PASSWORD:
        problems.append(f"새 사용자는 {MIN_PASSWORD}자 이상의 초기 비밀번호가 필요합니다.")

    if problems:
        return None, problems
    status = USER_STATUS[label] if label else None
    return UserPlan(row, email, name, existing, status, departments, primary, password), []


def _current(
    user: User | None, index: DepartmentIndex, memberships: dict[int, list[UserDepartment]]
) -> tuple[list[Path], Path | None]:
    links = [] if user is None else memberships.get(user.id, [])
    primary = _primary_id(links)
    paths = [index.path_of[link.department_id] for link in links]
    return paths, index.path_of[primary] if primary is not None else None


def _user_target(
    plan: UserPlan, index: DepartmentIndex, memberships: dict[int, list[UserDepartment]]
) -> tuple[list[Path], Path | None]:
    """반영 뒤의 소속 목록과 주 소속."""
    current_paths, current_primary = _current(plan.existing, index, memberships)
    if plan.departments is None and plan.primary is None:
        return current_paths, current_primary
    paths = current_paths if plan.departments is None else plan.departments
    if plan.primary is not None:
        primary = plan.primary
    elif current_primary in paths:
        primary = current_primary
    else:
        primary = paths[0] if paths else None
    return paths, primary


async def import_org(
    session: AsyncSession, admin: User, sheets: dict[str, list[Row] | None], apply: bool
) -> ImportReport:
    report = ImportReport()
    department_rows = sheets.get(DEPARTMENT_SHEET)
    user_rows = sheets.get(USER_SHEET)
    if department_rows is None and user_rows is None:
        report.errors.append("'소속' 또는 '사용자' 시트가 필요합니다.")
        return report

    index = DepartmentIndex(await session.scalars(select(Department)))
    for row in department_rows or []:
        path = parse_path(row.get("소속"))
        if path is None:
            report.error(DEPARTMENT_SHEET, row, "소속을 '본사 > 개발팀'처럼 적어 주세요.")
        elif (problem := index.problem(path)) is not None:
            report.error(DEPARTMENT_SHEET, row, problem)
        else:
            for added in index.plan(path):
                report.change("소속 추가", format_path(added))

    users = {item.email: item for item in await session.scalars(select(User))}
    memberships: dict[int, list[UserDepartment]] = {}
    for link in await session.scalars(select(UserDepartment)):
        memberships.setdefault(link.user_id, []).append(link)

    plans: list[UserPlan] = []
    seen: set[str] = set()
    for row in user_rows or []:
        plan, problems = _check_user_row(row, index, users, memberships, seen)
        for problem in problems:
            report.error(USER_SHEET, row, problem)
        if plan is None:
            continue
        plans.append(plan)
        paths, primary = _user_target(plan, index, memberships)
        if plan.existing is None:
            report.change("사용자 추가", f"{plan.name} ({plan.email})")
            continue
        current, current_primary = _current(plan.existing, index, memberships)
        changed = [
            label
            for label, differs in (
                ("이름", plan.name != plan.existing.name),
                ("상태", plan.status is not None and plan.status != plan.existing.status),
                ("소속", set(paths) != set(current)),
                ("주 소속", primary != current_primary),
            )
            if differs
        ]
        if changed:
            report.change("사용자 변경", f"{plan.name} ({plan.email}) · {', '.join(changed)}")
        else:
            report.unchanged()

    if report.errors or not apply:
        return report

    for path in index.planned:
        parent_id = index.id(path[:-1]) if len(path) > 1 else None
        department = Department(name=path[-1], parent_id=parent_id)
        session.add(department)
        await session.flush()
        index.ids[path] = [department.id]
        index.path_of[department.id] = path
        record_audit(
            session,
            actor_id=admin.id,
            action="department.create",
            target_type="department",
            target_id=department.id,
            name=department.name,
            parent_id=parent_id,
            via=VIA,
        )

    for plan in plans:
        await _apply_user(session, admin, plan, index, memberships)
    await session.flush()
    report.applied = True
    return report


async def _apply_user(
    session: AsyncSession,
    admin: User,
    plan: UserPlan,
    index: DepartmentIndex,
    memberships: dict[int, list[UserDepartment]],
) -> None:
    paths, primary = _user_target(plan, index, memberships)
    user = plan.existing
    if user is None:
        password_hash = await to_thread.run_sync(hash_password, plan.password)
        user = User(
            name=plan.name,
            email=plan.email,
            password_hash=password_hash,
            status=plan.status or "active",
        )
        session.add(user)
        await session.flush()
        record_audit(
            session,
            actor_id=admin.id,
            action="user.create",
            target_type="user",
            target_id=user.id,
            email=plan.email,
            via=VIA,
        )
        before = None
    else:
        before = {
            "name": user.name,
            "status": user.status,
            "departments": sorted(link.department_id for link in memberships.get(user.id, [])),
        }
        user.name = plan.name
        if plan.status is not None:
            user.status = plan.status

    selected = [index.id(path) for path in paths]
    primary_id = index.id(primary) if primary is not None else None
    if plan.departments is not None or plan.primary is not None:
        await session.execute(delete(UserDepartment).where(UserDepartment.user_id == user.id))
        session.add_all(
            UserDepartment(
                user_id=user.id,
                department_id=department_id,
                is_primary=department_id == primary_id,
            )
            for department_id in selected
        )
    if before is not None:
        after = {"name": user.name, "status": user.status, "departments": sorted(selected)}
        if after != before or primary_id != _primary_id(memberships.get(user.id, [])):
            record_audit(
                session,
                actor_id=admin.id,
                action="user.update",
                target_type="user",
                target_id=user.id,
                before=before,
                after=after,
                via=VIA,
            )


def _primary_id(links: list[UserDepartment]) -> int | None:
    return next((link.department_id for link in links if link.is_primary), None)


@dataclass
class GroupPlan:
    row: Row
    name: str
    existing: WorkspaceGroup | None
    managers: list[User] | None
    archived: bool | None


async def import_groups(
    session: AsyncSession, admin: User, sheets: dict[str, list[Row] | None], apply: bool
) -> ImportReport:
    report = ImportReport()
    rows = sheets.get(GROUP_SHEET)
    if rows is None:
        report.errors.append("'그룹' 시트가 필요합니다.")
        return report

    groups: dict[str, list[WorkspaceGroup]] = {}
    for group in await session.scalars(select(WorkspaceGroup)):
        groups.setdefault(group.name, []).append(group)
    active = {
        item.email: item
        for item in await session.scalars(select(User).where(User.status == "active"))
    }
    current: dict[int, set[int]] = {}
    for link in await session.scalars(select(GroupManager)):
        current.setdefault(link.group_id, set()).add(link.user_id)

    plans: list[GroupPlan] = []
    seen: set[str] = set()
    for row in rows:
        problems: list[str] = []
        name = row.get("그룹")
        if len(name) > NAME_MAX:
            problems.append(f"그룹 이름은 {NAME_MAX}자까지 적을 수 있습니다.")
        if name in seen:
            problems.append("파일 안에 같은 그룹이 또 있습니다.")
        seen.add(name)
        matches = groups.get(name, [])
        if len(matches) > 1:
            problems.append("같은 이름의 그룹이 둘 이상이라 고를 수 없습니다.")

        managers: list[User] | None = None
        if row.get("관리자"):
            managers = []
            for email in EMAIL_SPLIT.split(row.get("관리자").lower()):
                if not email:
                    continue
                if email not in active:
                    problems.append(
                        f"관리자로 지정할 수 없는 이메일입니다: {email} (없거나 비활성)"
                    )
                elif active[email] not in managers:
                    managers.append(active[email])

        label = row.get("상태")
        if label and label not in GROUP_ARCHIVED:
            problems.append("상태는 '사용' 또는 '보관'으로 적어 주세요.")
        if not name:
            problems.append("그룹 이름을 적어 주세요.")
        for problem in problems:
            report.error(GROUP_SHEET, row, problem)
        if problems:
            continue

        existing = matches[0] if matches else None
        plan = GroupPlan(row, name, existing, managers, GROUP_ARCHIVED[label] if label else None)
        plans.append(plan)
        manager_text = ", ".join(item.name for item in managers or [])
        if existing is None:
            suffix = f" · 관리자 {manager_text}" if manager_text else ""
            report.change("그룹 추가", f"{name}{suffix}")
            continue
        changed = []
        if managers is not None and {item.id for item in managers} != current.get(
            existing.id, set()
        ):
            changed.append(f"관리자 {manager_text}")
        if plan.archived is not None and plan.archived != (existing.archived_at is not None):
            changed.append("보관" if plan.archived else "보관 해제")
        if changed:
            report.change("그룹 변경", f"{name} · {', '.join(changed)}")
        else:
            report.unchanged()

    if report.errors or not apply:
        return report

    now = datetime.now(UTC)
    for plan in plans:
        group = plan.existing
        if group is None:
            group = WorkspaceGroup(name=plan.name, created_by=admin.id)
            session.add(group)
            await session.flush()
            record_audit(
                session,
                actor_id=admin.id,
                action="group.create",
                target_type="group",
                target_id=group.id,
                name=group.name,
                via=VIA,
            )
        if plan.archived is not None and plan.archived != (group.archived_at is not None):
            group.archived_at = now if plan.archived else None
            record_audit(
                session,
                actor_id=admin.id,
                action="group.archive" if plan.archived else "group.unarchive",
                target_type="group",
                target_id=group.id,
                via=VIA,
            )
        if plan.managers is None:
            continue
        wanted = {item.id for item in plan.managers}
        before = current.get(group.id, set())
        for user_id in sorted(before - wanted):
            await session.execute(
                delete(GroupManager).where(
                    GroupManager.group_id == group.id, GroupManager.user_id == user_id
                )
            )
            record_audit(
                session,
                actor_id=admin.id,
                action="group.manager_remove",
                target_type="group",
                target_id=group.id,
                user_id=user_id,
                via=VIA,
            )
        for user_id in sorted(wanted - before):
            session.add(GroupManager(group_id=group.id, user_id=user_id))
            record_audit(
                session,
                actor_id=admin.id,
                action="group.manager_add",
                target_type="group",
                target_id=group.id,
                user_id=user_id,
                via=VIA,
            )
    await session.flush()
    report.applied = True
    return report


ORG_GUIDE = [
    "조직 일괄 등록 안내",
    "· '소속' 시트: 한 행에 소속 하나를 '본사 > 개발팀 > 플랫폼파트'처럼 위에서부터 적습니다. "
    "없는 상위 소속은 함께 만들어집니다.",
    "· '사용자' 시트: 이메일로 사용자를 찾습니다. 없으면 새로 등록하고, 있으면 적은 칸만 바꿉니다.",
    "· 소속이 여러 개이면 세미콜론(;)으로 나눕니다. 주 소속은 그중 하나를 적습니다. "
    "비우면 첫 번째 소속이 주 소속이 됩니다.",
    "· 상태는 '사용' 또는 '비활성'입니다. "
    "기존 사용자의 빈 칸(소속, 주 소속, 상태)은 바꾸지 않습니다.",
    "· 초기 비밀번호는 새 사용자에게만 필요하며 8자 이상입니다. "
    "기존 사용자의 비밀번호는 바꾸지 않습니다.",
    "· 파일에 없는 소속과 사용자는 지우지 않습니다. 소속 이름 변경과 이동은 화면에서 합니다.",
    "· 상태, 소속, 주 소속 칸은 드롭다운에서 고를 수 있습니다. "
    "소속 드롭다운은 '소속' 시트의 목록이고, "
    "여러 개를 적으면 경고가 나오지만 그대로 쓸 수 있습니다.",
    "· 한 행이라도 문제가 있으면 아무것도 반영하지 않습니다. 먼저 '미리 확인'으로 점검하세요.",
]

GROUP_GUIDE = [
    "그룹 일괄 등록 안내",
    "· 그룹 이름으로 그룹을 찾습니다. 없으면 새로 만들고, 있으면 적은 칸만 바꿉니다.",
    "· 관리자에는 사용 중인 사용자의 이메일을 적고, 여러 명이면 세미콜론(;)으로 나눕니다. "
    "적으면 그 목록으로 바뀌고, 비우면 기존 관리자를 그대로 둡니다.",
    "· 상태는 '사용' 또는 '보관'입니다. 보관하면 그룹의 채널이 모두 닫힙니다.",
    "· 파일에 없는 그룹은 지우지 않습니다. 그룹 이름 변경은 화면에서 합니다.",
    "· 상태와 관리자 칸은 드롭다운에서 고를 수 있습니다. 관리자를 여러 명 적으면 경고가 나오지만 "
    "그대로 쓸 수 있습니다.",
    "· 한 행이라도 문제가 있으면 아무것도 반영하지 않습니다. 먼저 '미리 확인'으로 점검하세요.",
]


async def export_org(session: AsyncSession) -> bytes:
    departments = list(await session.scalars(select(Department)))
    paths = department_paths(departments)
    links: dict[int, list[UserDepartment]] = {}
    for link in await session.scalars(select(UserDepartment)):
        links.setdefault(link.user_id, []).append(link)
    user_rows = []
    for user in await session.scalars(select(User).order_by(User.name, User.id)):
        if is_admin_email(user.email):
            continue
        own = sorted(links.get(user.id, []), key=lambda link: paths[link.department_id])
        primary = _primary_id(own)
        user_rows.append(
            [
                user.name,
                user.email,
                "; ".join(format_path(paths[link.department_id]) for link in own),
                format_path(paths[primary]) if primary is not None else "",
                USER_STATUS_LABEL.get(user.status, user.status),
                "",
            ]
        )
    department_rows = [[format_path(path)] for path in sorted(paths.values())]
    return await to_thread.run_sync(
        build_workbook,
        [
            (DEPARTMENT_SHEET, ["소속"], department_rows),
            (USER_SHEET, list(USER_COLUMNS), user_rows),
        ],
        ORG_GUIDE,
        {
            (USER_SHEET, "소속"): Choice(source=(DEPARTMENT_SHEET, "소속"), strict=False),
            (USER_SHEET, "주 소속"): Choice(source=(DEPARTMENT_SHEET, "소속"), strict=False),
            (USER_SHEET, "상태"): Choice(tuple(USER_STATUS)),
        },
    )


async def export_groups(session: AsyncSession) -> bytes:
    managers: dict[int, list[str]] = {}
    rows = await session.execute(
        select(GroupManager.group_id, User.email)
        .join(User, User.id == GroupManager.user_id)
        .order_by(User.email)
    )
    for group_id, email in rows:
        managers.setdefault(group_id, []).append(email)
    group_rows = [
        [
            group.name,
            "; ".join(managers.get(group.id, [])),
            "보관" if group.archived_at else "사용",
        ]
        for group in await session.scalars(select(WorkspaceGroup).order_by(WorkspaceGroup.name))
    ]
    emails = await session.scalars(
        select(User.email).where(User.status == "active").order_by(User.email)
    )
    return await to_thread.run_sync(
        build_workbook,
        [(GROUP_SHEET, list(GROUP_COLUMNS), group_rows)],
        GROUP_GUIDE,
        {
            (GROUP_SHEET, "관리자"): Choice(
                tuple(email for email in emails if not is_admin_email(email)), strict=False
            ),
            (GROUP_SHEET, "상태"): Choice(tuple(GROUP_ARCHIVED)),
        },
    )


Importer = Callable[
    [AsyncSession, User, dict[str, list[Row] | None], bool], Awaitable[ImportReport]
]


async def run_import(
    session: AsyncSession,
    admin: User,
    data: bytes,
    specs: dict[str, SheetSpec],
    importer: Importer,
    apply: bool,
) -> ImportReport:
    try:
        sheets = await to_thread.run_sync(read_workbook, data, specs)
    except SheetError as error:
        return ImportReport(errors=[str(error)])
    try:
        report = await importer(session, admin, sheets, apply)
        if report.applied:
            await session.commit()
    except IntegrityError:
        await session.rollback()
        return ImportReport(errors=["다른 변경과 겹쳐 반영하지 못했습니다. 다시 시도해 주세요."])
    if not report.applied:
        await session.rollback()
    return report


def download(content: bytes, prefix: str) -> Response:
    filename = f"{prefix}-{datetime.now(UTC):%Y%m%d}.xlsx"
    return Response(
        content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

"""엑셀로 조직과 그룹을 한꺼번에 등록한다. 한 행이라도 문제가 있으면 아무것도 바꾸지 않는다."""

from html import unescape
from io import BytesIO
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import verify_password
from app.models import AuditLog, Department, GroupManager, User, UserDepartment, WorkspaceGroup
from app.services.spreadsheet import MAX_FILE_BYTES
from tests.conftest import (
    PASSWORD,
    _add,
    add_department,
    add_group,
    add_user,
    admin_browser,
    browser,
    join,
    run,
)

USER_HEADER = ["이름", "이메일", "소속", "주 소속", "상태", "초기 비밀번호"]


def xlsx(**sheets: list[list[object]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def org_file(departments: list[str] | None = None, users: list[list[object]] | None = None):
    sheets = {}
    if departments is not None:
        sheets["소속"] = [["소속"], *([path] for path in departments)]
    if users is not None:
        sheets["사용자"] = [USER_HEADER, *users]
    return xlsx(**sheets)


def upload(signed_in, url: str, content: bytes, mode: str = "apply"):
    files = {"file": ("data.xlsx", content, "application/octet-stream")}
    response = signed_in.post(url, files=files, data={"mode": mode})
    return SimpleNamespace(status_code=response.status_code, page=unescape(response.text))


def load(model, *conditions):
    async def query():
        async with SessionLocal() as session:
            return list(await session.scalars(select(model).where(*conditions)))

    return run(query())


def department_names() -> dict[str, int | None]:
    rows = load(Department)
    by_id = {item.id: item for item in rows}
    return {item.name: by_id[item.parent_id].name if item.parent_id else None for item in rows}


def memberships(email: str) -> dict[str, bool]:
    user = load(User, User.email == email)[0]
    names = {item.id: item.name for item in load(Department)}
    return {
        names[link.department_id]: link.is_primary
        for link in load(UserDepartment, UserDepartment.user_id == user.id)
    }


def admin():
    return admin_browser(add_user("admin"))


def test_org_import_creates_tree_and_users() -> None:
    signed_in = admin()
    content = org_file(
        ["본사 > 개발팀 > 플랫폼파트", "본사 > 영업팀"],
        [
            [
                "홍길동",
                "Hong@Test.local",
                "본사 > 개발팀; 본사 > 영업팀",
                "본사 > 영업팀",
                "",
                "pass-1234",
            ],
            ["김철수", "kim@test.local", "본사 > 개발팀 > 플랫폼파트", "", "비활성", 12345678],
        ],
    )
    response = upload(signed_in, "/admin/org/import", content)
    assert "반영했습니다" in response.page, response.page
    assert "소속 추가 4건" in response.page
    assert "사용자 추가 2건" in response.page

    assert department_names() == {
        "본사": None,
        "개발팀": "본사",
        "플랫폼파트": "개발팀",
        "영업팀": "본사",
    }
    assert memberships("hong@test.local") == {"개발팀": False, "영업팀": True}
    # 주 소속을 비우면 첫 번째 소속이 주 소속이다.
    assert memberships("kim@test.local") == {"플랫폼파트": True}
    kim = load(User, User.email == "kim@test.local")[0]
    assert kim.status == "inactive"
    # 숫자 칸으로 적은 비밀번호도 글자 그대로 쓴다.
    assert verify_password(kim.password_hash, "12345678")
    actions = {
        (log.action, log.detail.get("via")) for log in load(AuditLog, AuditLog.action != "x")
    }
    assert ("department.create", "excel") in actions
    assert ("user.create", "excel") in actions


def test_org_check_mode_writes_nothing() -> None:
    signed_in = admin()
    content = org_file(["본사"], [["홍길동", "hong@test.local", "본사", "", "", "pass-1234"]])
    response = upload(signed_in, "/admin/org/import", content, mode="check")
    assert "문제가 없습니다" in response.page
    assert "소속 추가: 본사" in response.page
    assert "사용자 추가: 홍길동 (hong@test.local)" in response.page
    assert load(Department) == []
    assert load(User, User.email == "hong@test.local") == []


def test_org_import_applies_nothing_when_any_row_is_wrong() -> None:
    signed_in = admin()
    add_user("old")
    content = org_file(
        ["본사"],
        [
            ["홍길동", "hong@test.local", "본사", "", "", "pass-1234"],
            ["새사람", "new@test.local", "", "", "", "short"],
            ["또홍", "HONG@test.local", "", "", "", "pass-1234"],
            ["관리", "admin@test.local", "", "", "", "pass-1234"],
            ["없음", "none@test.local", "본사 > 없는팀", "", "", "pass-1234"],
            ["주소속", "old@test.local", "본사", "다른팀", "", ""],
            ["상태", "state@test.local", "", "", "휴직", "pass-1234"],
            ["", "bad-email", "", "", "", ""],
        ],
    )
    response = upload(signed_in, "/admin/org/import", content)
    text = response.page
    assert "아무것도 반영하지 않았습니다" in text
    assert "사용자 시트 3행: 새 사용자는 8자 이상의 초기 비밀번호가 필요합니다." in text
    assert "사용자 시트 4행: 파일 안에 같은 이메일이 또 있습니다." in text
    assert "사용자 시트 5행: 시스템 관리자 계정은 올릴 수 없습니다." in text
    assert "사용자 시트 6행: 없는 소속입니다: 본사 > 없는팀" in text
    assert "사용자 시트 7행: 주 소속은 그 사용자의 소속 중 하나여야 합니다." in text
    assert "사용자 시트 8행: 상태는" in text
    assert "사용자 시트 9행: 이메일 형식이 올바르지 않습니다." in text
    assert "사용자 시트 9행: 이름은" in text
    assert load(Department) == []
    assert load(User, User.email == "hong@test.local") == []


def test_org_import_keeps_blank_cells_of_existing_user() -> None:
    signed_in = admin()
    hq = add_department("본사")
    team = add_department("개발팀", hq)
    user = add_user("hong")
    join(user, team)
    content = org_file(users=[["홍길순", "hong@test.local", "", "", "", "new-password"]])
    response = upload(signed_in, "/admin/org/import", content)
    assert "사용자 변경: 홍길순 (hong@test.local) · 이름<" in response.page
    stored = load(User, User.email == "hong@test.local")[0]
    assert stored.name == "홍길순"
    assert stored.status == "active"
    assert memberships("hong@test.local") == {"개발팀": False}
    # 기존 사용자의 비밀번호는 파일로 바꾸지 않는다.
    assert verify_password(stored.password_hash, PASSWORD)


def test_org_import_replaces_departments_and_primary() -> None:
    signed_in = admin()
    hq = add_department("본사")
    add_department("개발팀", hq)
    add_department("영업팀", hq)
    user = add_user("hong")
    join(user, load(Department, Department.name == "개발팀")[0])
    content = org_file(
        users=[["hong", "hong@test.local", "본사 > 영업팀 ; 본사", "본사", "비활성", ""]]
    )
    response = upload(signed_in, "/admin/org/import", content)
    assert "사용자 변경: hong (hong@test.local) · 상태, 소속, 주 소속<" in response.page
    assert memberships("hong@test.local") == {"영업팀": False, "본사": True}
    assert load(User, User.email == "hong@test.local")[0].status == "inactive"


def test_org_import_rejects_ambiguous_department_path() -> None:
    signed_in = admin()
    hq = add_department("본사")
    add_department("개발팀", hq)
    add_department("개발팀", hq)
    content = org_file(["본사 > 개발팀 > 파트"])
    response = upload(signed_in, "/admin/org/import", content)
    assert "'본사 > 개발팀' 소속이 둘 이상이라 고를 수 없습니다." in response.page
    assert len(load(Department)) == 3


def test_org_export_round_trips_without_changes() -> None:
    signed_in = admin()
    hq = add_department("본사")
    team = add_department("개발팀", hq)
    user = add_user("=1+1")
    run(_add(UserDepartment(user_id=user.id, department_id=team.id, is_primary=True)))
    add_user("other", status="inactive")
    response = signed_in.get("/admin/org/export.xlsx")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    workbook = load_workbook(BytesIO(response.content))
    assert workbook.sheetnames == ["소속", "사용자", "안내", "선택 목록"]
    assert workbook["선택 목록"].sheet_state == "hidden"
    # 고르는 칸은 드롭다운이다. 소속은 '소속' 시트를 가리켜 새로 적은 소속도 고를 수 있다.
    lists = {
        str(rule.sqref).split(":")[0]: rule
        for rule in workbook["사용자"].data_validations.dataValidation
    }
    assert lists["C2"].formula1 == "'소속'!$A$2:$A$5001" and lists["C2"].errorStyle == "warning"
    assert lists["D2"].formula1 == "'소속'!$A$2:$A$5001"
    assert lists["E2"].errorStyle == "stop"
    status = workbook["선택 목록"]
    assert [cell.value for cell in status["A"]] == ["사용자 · 상태", "사용", "비활성"]
    rows = list(workbook["사용자"].iter_rows(values_only=True))
    assert rows[0] == tuple(USER_HEADER)
    emails = [row[1] for row in rows[1:]]
    assert "admin@test.local" not in emails
    formula = next(cell for cell in workbook["사용자"]["A"] if cell.value == "=1+1")
    # 이름이 수식으로 저장되지 않는다.
    assert formula.data_type == "s"
    assert ("other", "other@test.local", None, None, "비활성", None) in rows
    assert ("=1+1", "=1+1@test.local", "본사 > 개발팀", "본사 > 개발팀", "사용", None) in rows

    again = upload(signed_in, "/admin/org/import", response.content, mode="check")
    assert "변경 없음 2건" in again.page, again.page
    assert "소속 추가" not in again.page


def test_import_rejects_bad_files() -> None:
    signed_in = admin()
    response = upload(signed_in, "/admin/org/import", b"not a spreadsheet")
    assert "엑셀(.xlsx) 파일이 아닙니다." in response.page
    response = upload(signed_in, "/admin/org/import", b"x" * (MAX_FILE_BYTES + 10))
    assert "2MB까지" in response.page
    response = upload(signed_in, "/admin/org/import", xlsx(기타=[["a"]]))
    assert "'소속' 또는 '사용자' 시트가 필요합니다." in response.page
    response = upload(signed_in, "/admin/org/import", xlsx(사용자=[["이름"], ["홍"]]))
    assert "'사용자' 시트 첫 행에 이메일 열이 없습니다." in response.page
    response = upload(signed_in, "/admin/groups/import", xlsx(소속=[["소속"]]))
    assert "'그룹' 시트가 필요합니다." in response.page


def test_import_and_export_need_system_admin() -> None:
    manager = add_user("manager")
    add_group(manager)
    signed_in = browser(manager)
    content = org_file(["본사"])
    assert upload(signed_in, "/admin/org/import", content).status_code == 403
    assert upload(signed_in, "/admin/groups/import", content).status_code == 403
    assert signed_in.get("/admin/org/export.xlsx").status_code == 403
    assert signed_in.get("/admin/groups/export.xlsx").status_code == 403
    assert load(Department) == []


def group_file(*rows: list[object]) -> bytes:
    return xlsx(그룹=[["그룹", "관리자", "상태"], *rows])


def managers_of(name: str) -> set[str]:
    group = load(WorkspaceGroup, WorkspaceGroup.name == name)[0]
    ids = {link.user_id for link in load(GroupManager, GroupManager.group_id == group.id)}
    return {user.email for user in load(User, User.id.in_(ids))}


def test_group_import_creates_and_updates_groups() -> None:
    signed_in = admin()
    lee = add_user("lee")
    add_user("park")
    add_user("kim")
    add_group(lee, "영업")
    content = group_file(
        ["개발", "park@test.local; KIM@test.local", ""],
        ["영업", "park@test.local", "보관"],
    )
    response = upload(signed_in, "/admin/groups/import", content)
    assert "그룹 추가 1건" in response.page, response.page
    assert "그룹 변경: 영업 · 관리자 park, 보관" in response.page
    assert managers_of("개발") == {"park@test.local", "kim@test.local"}
    assert managers_of("영업") == {"park@test.local"}
    assert load(WorkspaceGroup, WorkspaceGroup.name == "영업")[0].archived_at is not None
    assert load(WorkspaceGroup, WorkspaceGroup.name == "개발")[0].archived_at is None
    actions = {log.action for log in load(AuditLog) if log.detail.get("via") == "excel"}
    assert actions == {
        "group.create",
        "group.manager_add",
        "group.manager_remove",
        "group.archive",
    }

    # 관리자 칸을 비우면 기존 관리자를 그대로 둔다.
    response = upload(signed_in, "/admin/groups/import", group_file(["영업", "", "사용"]))
    assert "그룹 변경: 영업 · 보관 해제" in response.page
    assert managers_of("영업") == {"park@test.local"}


def test_group_import_applies_nothing_when_any_row_is_wrong() -> None:
    signed_in = admin()
    add_user("park")
    add_user("off", status="inactive")
    content = group_file(
        ["개발", "park@test.local", ""],
        ["개발", "", ""],
        ["운영", "off@test.local; ghost@test.local", "삭제"],
    )
    response = upload(signed_in, "/admin/groups/import", content)
    text = response.page
    assert "아무것도 반영하지 않았습니다" in text
    assert "그룹 시트 3행: 파일 안에 같은 그룹이 또 있습니다." in text
    assert "그룹 시트 4행: 관리자로 지정할 수 없는 이메일입니다: off@test.local" in text
    assert "ghost@test.local" in text
    assert "그룹 시트 4행: 상태는 '사용' 또는 '보관'으로 적어 주세요." in text
    assert load(WorkspaceGroup) == []


def test_group_export_round_trips() -> None:
    signed_in = admin()
    park = add_user("park")
    add_group(park, "개발")
    response = signed_in.get("/admin/groups/export.xlsx")
    rows = list(load_workbook(BytesIO(response.content))["그룹"].iter_rows(values_only=True))
    assert rows == [("그룹", "관리자", "상태"), ("개발", "park@test.local", "사용")]
    again = upload(signed_in, "/admin/groups/import", response.content, mode="check")
    assert "변경 없음 1건" in again.page


def test_admin_pages_show_upload_form() -> None:
    signed_in = admin()
    for url, action in (
        ("/admin/org", "/admin/org/import"),
        ("/admin/groups", "/admin/groups/import"),
    ):
        page = signed_in.get(url).text
        assert f'hx-post="{action}"' in page
        assert 'name="mode" value="check"' in page
        assert 'name="mode" value="apply"' in page

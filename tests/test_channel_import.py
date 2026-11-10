"""그룹 관리자는 관리하는 그룹의 채널과 권한을 엑셀로 한꺼번에 등록한다."""

from datetime import UTC, datetime
from html import unescape
from io import BytesIO
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog, Channel, PermissionGrant, WorkspaceGroup
from tests.conftest import (
    _add,
    add_channel,
    add_department,
    add_group,
    add_user,
    browser,
    grant,
    run,
)

CHANNEL_HEADER = ["그룹", "채널", "유형", "순서", "고정 글 수"]
GRANT_HEADER = ["그룹", "채널", "대상", "읽기", "쓰기", "답글 읽기", "답글 쓰기"]


def xlsx(channels=None, grants=None) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, header, rows in (("채널", CHANNEL_HEADER, channels), ("권한", GRANT_HEADER, grants)):
        if rows is None:
            continue
        sheet = workbook.create_sheet(title)
        for row in [header, *rows]:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def upload(signed_in, content: bytes, mode: str = "apply"):
    files = {"file": ("data.xlsx", content, "application/octet-stream")}
    response = signed_in.post("/groups/import", files=files, data={"mode": mode})
    return SimpleNamespace(status_code=response.status_code, page=unescape(response.text))


def load(model, *conditions):
    async def query():
        async with SessionLocal() as session:
            return list(await session.scalars(select(model).where(*conditions)))

    return run(query())


def channel(name: str) -> Channel:
    return load(Channel, Channel.name == name)[0]


def grants_of(scope_type: str, scope_id: int) -> dict[tuple[str, int], tuple]:
    return {
        (item.subject_type, item.subject_id): (
            item.can_read,
            item.can_write,
            item.can_reply_read,
            item.can_reply_write,
        )
        for item in load(
            PermissionGrant,
            PermissionGrant.scope_type == scope_type,
            PermissionGrant.scope_id == scope_id,
        )
    }


def setup():
    manager = add_user("manager")
    group = add_group(manager, "영업")
    return manager, group, browser(manager)


def test_manager_creates_channels_and_permissions() -> None:
    manager, group, signed_in = setup()
    add_channel(group, "기존", sort_order=1)
    hq = add_department("본사")
    team = add_department("영업팀", hq)
    member = add_user("member")
    content = xlsx(
        [["영업", "고객 미팅", "소통", "", 3], ["영업", "지출", "결재", 7, ""]],
        [
            ["영업", "", "본사", "허용", "", "허용", "허용"],
            ["영업", "고객 미팅", "본사 > 영업팀", "", "허용", "", ""],
            ["영업", "고객 미팅", "MEMBER@test.local", "거부", "", "", "허용"],
        ],
    )
    response = upload(signed_in, content)
    assert "반영했습니다" in response.page, response.page
    assert "채널 추가 2건" in response.page and "권한 추가 3건" in response.page

    meeting, expense = channel("고객 미팅"), channel("지출")
    assert (meeting.type, meeting.sort_order, meeting.pin_limit) == ("talk", 2, 3)
    assert (expense.type, expense.sort_order, expense.pin_limit) == ("approval", 7, 5)
    assert grants_of("group", group.id) == {
        ("department", hq.id): ("allow", None, "allow", "allow")
    }
    # 답글 쓰기를 허용하면 답글 읽기도 허용한다. 화면과 같은 규칙이다.
    assert grants_of("channel", meeting.id) == {
        ("department", team.id): (None, "allow", None, None),
        ("user", member.id): ("deny", None, "allow", "allow"),
    }
    logs = [log for log in load(AuditLog) if log.detail.get("via") == "excel"]
    assert {log.action for log in logs} == {"channel.create", "permission.change"}
    assert all(log.actor_id == manager.id for log in logs)


def test_only_managed_active_groups_can_be_changed() -> None:
    _, _, signed_in = setup()
    other = add_group(add_user("other"), "개발")
    archived = WorkspaceGroup(
        name="보관", created_by=other.created_by, archived_at=datetime.now(UTC)
    )
    run(_add(archived))
    content = xlsx([["개발", "새 채널", "소통", "", ""], ["보관", "새 채널", "소통", "", ""]])
    response = upload(signed_in, content)
    assert "채널 시트 2행: 관리하는 그룹이 아닙니다: 개발" in response.page
    assert "채널 시트 3행: 관리하는 그룹이 아닙니다: 보관" in response.page
    assert load(Channel) == []

    outsider = browser(add_user("outsider"))
    assert upload(outsider, content).status_code == 403
    assert outsider.get("/groups/export.xlsx").status_code == 403
    assert "/groups/import" not in outsider.get("/groups").text


def test_existing_channel_updates_only_filled_cells() -> None:
    _, group, signed_in = setup()
    add_channel(group, "일반", sort_order=1)
    add_channel(group, "결재", "approval", sort_order=2)
    content = xlsx([["영업", "일반", "", 5, ""], ["영업", "결재", "결재", "", 10]])
    response = upload(signed_in, content)
    assert "채널 변경: 영업 > 일반 · 순서" in response.page
    assert "채널 변경: 영업 > 결재 · 고정 글 수" in response.page
    assert (channel("일반").sort_order, channel("일반").pin_limit) == (5, 5)
    assert (channel("결재").sort_order, channel("결재").pin_limit) == (2, 10)


def test_grant_row_replaces_values_and_empty_row_deletes() -> None:
    _, group, signed_in = setup()
    talk = add_channel(group, "일반")
    team = add_department("영업팀")
    keep = add_user("keep")
    gone = add_user("gone")
    grant(talk, team, read="allow", write="allow")
    grant(talk, keep, read="allow")
    grant(talk, gone, write="deny")
    content = xlsx(
        grants=[
            ["영업", "일반", "영업팀", "허용", "", "", ""],
            ["영업", "일반", "keep@test.local", "허용", "", "", ""],
            ["영업", "일반", "gone@test.local", "", "", "", ""],
        ]
    )
    check = upload(signed_in, content, mode="check")
    assert "권한 변경: 영업 > 일반 · 영업팀 · 읽기 허용" in check.page
    assert "권한 삭제: 영업 > 일반 · gone@test.local" in check.page
    assert "변경 없음 1건" in check.page
    upload(signed_in, content)
    assert grants_of("channel", talk.id) == {
        ("department", team.id): ("allow", None, None, None),
        ("user", keep.id): ("allow", None, None, None),
    }


def test_any_wrong_row_applies_nothing() -> None:
    _, group, signed_in = setup()
    add_channel(group, "일반")
    content = xlsx(
        [
            ["영업", "새 채널", "소통", "", ""],
            ["영업", "새 채널", "소통", "", ""],
            ["영업", "유형없음", "", "", ""],
            ["영업", "일반", "투표", "", ""],
            ["영업", "숫자", "소통", "둘", 99],
            ["영업", "이상한", "회의", "", ""],
        ],
        [
            ["영업", "없는 채널", "영업팀", "허용", "", "", ""],
            ["영업", "", "ghost@test.local", "허용", "", "", ""],
            ["영업", "", "본사 > 없음", "허용", "", "", ""],
            ["영업", "", "manager@test.local", "가능", "", "", ""],
            ["영업", "새 채널", "manager@test.local", "허용", "", "", ""],
            ["영업", "새 채널", "manager@test.local", "거부", "", "", ""],
        ],
    )
    page = upload(signed_in, content).page
    assert "아무것도 반영하지 않았습니다" in page
    for message in (
        "채널 시트 3행: 파일 안에 같은 채널이 또 있습니다.",
        "채널 시트 4행: 새 채널은 유형을 적어 주세요.",
        "채널 시트 5행: 채널 유형은 바꿀 수 없습니다.",
        "채널 시트 6행: 순서는 0~10000 사이의 숫자로 적어 주세요.",
        "채널 시트 6행: 고정 글 수는 0~50 사이의 숫자로 적어 주세요.",
        "채널 시트 7행: 유형은 소통, 투표, 문서, 결재, 업무 중 하나로 적어 주세요.",
        "권한 시트 2행: 없는 채널입니다: 없는 채널",
        "권한 시트 2행: 없는 소속입니다: 영업팀",
        "권한 시트 3행: 없는 사용자입니다: ghost@test.local",
        "권한 시트 4행: 없는 소속입니다: 본사 > 없음",
        "권한 시트 5행: 읽기 칸은 '허용', '거부' 또는 빈 칸으로 적어 주세요.",
        "권한 시트 7행: 파일 안에 같은 대상의 권한이 또 있습니다.",
    ):
        assert message in page, message
    assert [item.name for item in load(Channel)] == ["일반"]
    assert load(PermissionGrant) == []


def test_export_covers_managed_groups_and_round_trips() -> None:
    manager, group, signed_in = setup()
    talk = add_channel(group, "일반", sort_order=1)
    hq = add_department("본사")
    grant(group, hq, read="allow")
    grant(talk, manager, write="deny")
    other = add_group(add_user("other"), "개발")
    grant(add_channel(other, "비밀"), hq, read="allow")

    response = signed_in.get("/groups/export.xlsx")
    workbook = load_workbook(BytesIO(response.content))
    assert workbook.sheetnames == ["채널", "권한", "안내"]
    assert list(workbook["채널"].iter_rows(values_only=True)) == [
        tuple(CHANNEL_HEADER),
        ("영업", "일반", "소통", 1, 5),
    ]
    assert list(workbook["권한"].iter_rows(values_only=True)) == [
        tuple(GRANT_HEADER),
        ("영업", None, "본사", "허용", None, None, None),
        ("영업", "일반", "manager@test.local", None, "거부", None, None),
    ]
    again = upload(signed_in, response.content, mode="check")
    assert "변경 없음 3건" in again.page, again.page

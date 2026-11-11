"""감사 로그 화면. 시스템 관리자만 보고, 결재는 글 번호로도 찾는다."""

import re
from html import unescape
from pathlib import Path

from app.services.audit_view import ACTION_LABELS
from tests.conftest import (
    FULL,
    add_channel,
    add_department,
    add_group,
    add_user,
    admin_browser,
    browser,
    grant,
)

SUBMIT = '<span class="audit-action">상신'
PERMISSION = '<span class="audit-action">권한 변경'


def test_only_system_admin_sees_audit() -> None:
    assert browser(add_user("someone")).get("/admin/audit").status_code == 403
    assert admin_browser(add_user("admin")).get("/admin/audit").status_code == 200


def test_filters_by_kind_actor_and_target() -> None:
    admin = add_user("admin")
    owner = add_user("owner")
    channel = add_channel(add_group(owner), "결재", "approval")
    grant(channel, owner, **FULL)
    signed_in = browser(owner)
    created = signed_in.post(
        f"/channels/{channel.id}/approvals", data={"title": "x", "body": "본문"}
    )
    post = int(created.headers["HX-Redirect"].rsplit("/", 1)[1])
    signed_in.post(f"/approvals/{post}/submit")
    signed_in.post(
        f"/permissions/channel/{channel.id}", data={"subject": f"user:{admin.id}", "read": "allow"}
    )

    viewer = admin_browser(admin)
    page = viewer.get("/admin/audit?kind=approval.").text
    assert SUBMIT in page and PERMISSION not in page
    page = viewer.get("/admin/audit?kind=permission.").text
    assert PERMISSION in page and SUBMIT not in page
    assert SUBMIT in viewer.get(f"/admin/audit?target={post}").text
    assert SUBMIT not in viewer.get("/admin/audit?actor=nobody").text
    assert SUBMIT in viewer.get("/admin/audit?actor=owner@test").text


def test_audit_page_is_read_only() -> None:
    admin = admin_browser(add_user("admin"))
    assert admin.post("/admin/audit").status_code == 405


def audit_ids(viewer, url: str = "/admin/audit") -> list[int]:
    return [
        int(number) for number in re.findall(r'hx-get="/admin/audit/(\d+)"', viewer.get(url).text)
    ]


def test_list_shows_labels_and_target_names_and_detail_resolves_ids() -> None:
    admin = add_user("admin")
    owner = add_user("owner")
    channel = add_channel(add_group(owner), "공지")
    browser(owner).post(
        f"/permissions/channel/{channel.id}", data={"subject": f"user:{admin.id}", "read": "allow"}
    )
    viewer = admin_browser(admin)
    page = unescape(viewer.get("/admin/audit").text)
    assert '<span class="audit-action">권한 변경' in page
    assert f"채널 · 공지 (#{channel.id})" in page
    # 목록에는 상세를 싣지 않는다.
    assert "audit-changes" not in page

    newest = audit_ids(viewer, "/admin/audit?kind=permission.")[0]
    detail = unescape(viewer.get(f"/admin/audit/{newest}").text)
    assert "permission.change" in detail
    assert f"사용자 admin (#{admin.id})" in detail
    assert '<tr class="changed"><th scope="row">읽기</th><td>없음</td><td>허용</td></tr>' in detail
    assert "원본 기록" in detail


def test_org_kind_and_department_names_in_user_update() -> None:
    admin = add_user("admin")
    team = add_department("개발팀")
    target = add_user("target")
    viewer = admin_browser(admin)
    viewer.post(
        f"/admin/users/{target.id}",
        data={"name": "target", "status": "inactive", "department_ids": [team.id]},
    )
    page = viewer.get("/admin/audit?kind=org.").text
    assert '<span class="audit-action">사용자 변경' in page
    assert '<span class="audit-action">관리자 모드 시작' not in page
    detail = unescape(
        viewer.get(f"/admin/audit/{audit_ids(viewer, '/admin/audit?kind=org.')[0]}").text
    )
    assert '<th scope="row">상태</th><td>사용</td><td>비활성</td>' in detail
    assert f"<td>개발팀 (#{team.id})</td>" in detail


def test_detail_is_admin_only_and_404_for_unknown() -> None:
    admin = add_user("admin")
    assert browser(add_user("someone")).get("/admin/audit/1").status_code == 403
    assert admin_browser(admin).get("/admin/audit/999999").status_code == 404


def test_every_recorded_action_has_a_label() -> None:
    source = "\n".join(path.read_text() for path in Path("src/app").rglob("*.py"))
    actions = {
        action
        for line in source.splitlines()
        if "action=" in line
        for action in re.findall(r'"([a-z]+\.[a-z_]+)"', line)
    }
    approval_steps = re.findall(r'audit\(session, doc, [\w.]+, "([a-z_]+)"', source)
    actions |= {f"approval.{step}" for step in approval_steps}
    assert approval_steps
    missing = sorted(action for action in actions if action not in ACTION_LABELS)
    assert missing == [], missing

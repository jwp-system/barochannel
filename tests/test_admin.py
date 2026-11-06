"""시스템 관리자는 환경 변수로만 지정하고, 관리 기능은 PIN을 확인한 동안에만 열린다."""

import time

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models import AuditLog, User
from app.services.admin import INITIAL_PIN, MAX_PIN_FAILURES, PIN_CHANGE_PATH, safe_next
from tests.conftest import (
    ADMIN_PIN,
    FULL,
    PASSWORD,
    Browser,
    add_channel,
    add_group,
    add_user,
    admin_browser,
    browser,
    grant,
    post_id,
    run,
)

WRONG_PIN = "000000"


def fail_pin(signed_in, times: int):
    response = None
    for _ in range(times):
        response = signed_in.post("/admin/pin", data={"pin": WRONG_PIN})
    return response


def audit_actions() -> list[str]:
    async def load() -> list[str]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(AuditLog.action).order_by(AuditLog.id)))

    return run(load())


def test_user_model_has_no_admin_column() -> None:
    assert "is_admin" not in User.__table__.columns


def test_group_manager_is_not_system_admin() -> None:
    manager = add_user("manager")
    add_group(manager)
    signed_in = browser(manager)
    assert signed_in.get("/admin/org").status_code == 403
    assert signed_in.get("/admin/groups").status_code == 403
    assert signed_in.get("/admin/pin").status_code == 403
    assert signed_in.post("/admin/pin", data={"pin": ADMIN_PIN}).status_code == 403
    assert signed_in.post("/admin/groups", data={"name": "x"}).status_code == 403


def test_correct_pin_from_non_admin_does_not_open_admin_mode() -> None:
    signed_in = browser(add_user("plain"))
    signed_in.post("/admin/pin", data={"pin": ADMIN_PIN})
    assert signed_in.get("/admin/org").status_code == 403


def test_admin_without_pin_is_sent_to_pin_page() -> None:
    signed_in = browser(add_user("admin"))
    response = signed_in.get("/admin/org")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/pin?next=%2Fadmin%2Forg"
    change = signed_in.post("/admin/departments", data={"name": "x"})
    assert change.headers["HX-Redirect"].startswith("/admin/pin")


def test_correct_pin_opens_admin_pages_and_is_audited() -> None:
    signed_in = admin_browser(add_user("admin"))
    assert signed_in.get("/admin/org").status_code == 200
    assert audit_actions() == ["admin.pin_success"]


def test_admin_mode_expires(monkeypatch) -> None:
    signed_in = admin_browser(add_user("admin"))
    later = time.time() + get_settings().admin_mode_minutes * 60 + 1
    monkeypatch.setattr("app.services.admin.time.time", lambda: later)
    assert signed_in.get("/admin/org").status_code == 303


def test_exit_and_logout_close_admin_mode() -> None:
    admin = add_user("admin")
    signed_in = admin_browser(admin)
    signed_in.post("/admin/exit")
    assert signed_in.get("/admin/org").status_code == 303

    signed_in = admin_browser(admin)
    signed_in.post("/logout")
    signed_in.login(admin.email)
    assert signed_in.get("/admin/org").status_code == 303


def test_failures_show_remaining_and_lock_after_limit() -> None:
    signed_in = browser(add_user("admin"))
    assert "4회 남았습니다" in fail_pin(signed_in, 1).text
    response = fail_pin(signed_in, MAX_PIN_FAILURES - 1)
    assert "잠겼습니다" in response.text
    locked = signed_in.post("/admin/pin", data={"pin": ADMIN_PIN})
    assert "HX-Redirect" not in locked.headers and "잠겼습니다" in locked.text
    assert signed_in.get("/admin/org").status_code == 303
    assert audit_actions().count("admin.pin_failure") == MAX_PIN_FAILURES - 1
    assert audit_actions().count("admin.pin_locked") == 1


def test_new_login_does_not_reset_lock_or_failure_count() -> None:
    admin = add_user("admin")
    fail_pin(browser(admin), MAX_PIN_FAILURES - 1)
    fresh = browser(admin)
    assert "잠겼습니다" in fail_pin(fresh, 1).text
    other_session = browser(admin)
    response = other_session.post("/admin/pin", data={"pin": ADMIN_PIN})
    assert "HX-Redirect" not in response.headers


def test_success_resets_failure_count() -> None:
    signed_in = browser(add_user("admin"))
    fail_pin(signed_in, MAX_PIN_FAILURES - 1)
    assert signed_in.post("/admin/pin", data={"pin": ADMIN_PIN}).headers["HX-Redirect"]
    assert "4회 남았습니다" in fail_pin(signed_in, 1).text


def test_removing_email_from_env_closes_open_admin_mode(monkeypatch) -> None:
    signed_in = admin_browser(add_user("admin"))
    monkeypatch.setattr(get_settings(), "admin_email", "someone@test.local")
    assert signed_in.get("/admin/org").status_code == 403


def test_admin_email_is_normalized_and_short_password_rejected() -> None:
    assert Settings(admin_email=" Admin@X.com ").admin_email == "admin@x.com"
    with pytest.raises(ValidationError):
        Settings(admin_password="short")


@pytest.mark.parametrize("target", ["https://evil.test", "//evil.test", "/\\evil.test", "x"])
def test_next_cannot_leave_site(target: str) -> None:
    assert safe_next(target) == "/admin/org"


def test_pin_redirect_ignores_external_next() -> None:
    signed_in = browser(add_user("admin"))
    response = signed_in.post("/admin/pin", data={"pin": ADMIN_PIN, "next": "//evil.test"})
    assert response.headers["HX-Redirect"] == "/admin/org"


def test_only_system_admin_creates_groups_and_needs_pin() -> None:
    assert (
        "HX-Redirect"
        in browser(add_user("admin")).post("/admin/groups", data={"name": "x"}).headers
    )
    assert browser(add_user("plain")).post("/admin/groups", data={"name": "x"}).status_code == 403
    assert browser(add_user("plain2")).post("/groups", data={"name": "x"}).status_code == 405


def test_system_admin_creates_group_and_assigns_manager() -> None:
    admin = admin_browser(add_user("admin"))
    manager = add_user("manager")
    created = admin.post("/admin/groups", data={"name": "영업"})
    url = created.headers["HX-Redirect"]
    assert admin.post(f"{url}/managers", data={"user_id": manager.id}).status_code == 200
    assert "manager" in admin.get(url).text

    signed_in = browser(manager)
    group_id = url.rsplit("/", 1)[1]
    response = signed_in.post(f"/groups/{group_id}/channels", data={"name": "일반", "type": "talk"})
    assert response.status_code == 200
    assert signed_in.get(f"/groups/{group_id}/settings").status_code == 200
    assert "group.manager_add" in audit_actions()


def test_removed_manager_loses_access_immediately() -> None:
    admin = add_user("admin")
    manager = add_user("manager")
    group = add_group(manager)
    channel = add_channel(group)
    signed_in = browser(manager)
    assert signed_in.get(f"/channels/{channel.id}/settings").status_code == 200
    url = f"/admin/groups/{group.id}/managers/{manager.id}/delete"
    assert admin_browser(admin).post(url).status_code == 200
    assert signed_in.get(f"/channels/{channel.id}/settings").status_code == 403


def test_inactive_user_cannot_become_manager() -> None:
    admin = admin_browser(add_user("admin"))
    group = add_group(add_user("owner"))
    gone = add_user("gone", status="inactive")
    url = f"/admin/groups/{group.id}/managers"
    assert admin.post(url, data={"user_id": gone.id}).status_code == 404


def test_manager_is_limited_to_own_group() -> None:
    manager = add_user("manager")
    own = add_group(manager)
    others = add_group(add_user("other"))
    channel = add_channel(others)
    signed_in = browser(manager)
    assert signed_in.get(f"/groups/{own.id}/settings").status_code == 200
    assert signed_in.get(f"/groups/{others.id}/settings").status_code == 403
    assert signed_in.get(f"/channels/{channel.id}/settings").status_code == 403
    data = {"name": "x", "type": "talk"}
    assert signed_in.post(f"/groups/{others.id}/channels", data=data).status_code == 403


def test_group_creator_without_manager_role_cannot_manage() -> None:
    """그룹을 만든 시스템 관리자도 관리자로 지정되기 전에는 채널을 다루지 않는다."""
    admin = add_user("admin")
    signed_in = admin_browser(admin)
    url = signed_in.post("/admin/groups", data={"name": "영업"}).headers["HX-Redirect"]
    group_id = url.rsplit("/", 1)[1]
    data = {"name": "일반", "type": "talk"}
    assert signed_in.post(f"/groups/{group_id}/channels", data=data).status_code == 403


def test_system_admin_stops_at_group_level_even_in_admin_mode() -> None:
    group = add_group(add_user("owner"))
    channel = add_channel(group)
    signed_in = admin_browser(add_user("admin"))
    assert signed_in.get(f"/admin/groups/{group.id}").status_code == 200
    assert signed_in.get(f"/groups/{group.id}/settings").status_code == 403
    assert signed_in.get(f"/channels/{channel.id}/settings").status_code == 403
    data = {"subject": "user:1", "read": "allow"}
    assert signed_in.post(f"/permissions/channel/{channel.id}", data=data).status_code == 403


def test_admin_can_assign_self_as_manager_to_step_in() -> None:
    admin = add_user("admin")
    signed_in = admin_browser(admin)
    group = add_group(add_user("owner"))
    signed_in.post(f"/admin/groups/{group.id}/managers", data={"user_id": admin.id})
    assert signed_in.get(f"/groups/{group.id}/settings").status_code == 200


def test_admin_mode_does_not_grant_reading() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    created = post_id(browser(owner).post(f"/channels/{channel.id}/posts", data={"body": "비밀"}))
    signed_in = admin_browser(add_user("admin"))
    assert signed_in.get(f"/channels/{channel.id}").status_code == 404
    assert signed_in.get(f"/posts/{created}/card").status_code == 404


def test_archive_by_admin_closes_group_for_manager() -> None:
    manager = add_user("manager")
    group = add_group(manager)
    admin = admin_browser(add_user("admin"))
    admin.post(f"/admin/groups/{group.id}/archive")
    assert "보관됨" in admin.get("/admin/groups").text
    assert browser(manager).get(f"/groups/{group.id}/settings").status_code == 404
    admin.post(f"/admin/groups/{group.id}/archive")
    assert browser(manager).get(f"/groups/{group.id}/settings").status_code == 200


def test_pin_toggle_is_for_group_manager_only() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    admin = add_user("admin")
    grant(channel, admin, **FULL)
    created = post_id(browser(owner).post(f"/channels/{channel.id}/posts", data={"body": "글"}))
    assert admin_browser(admin).post(f"/posts/{created}/pin").status_code == 403
    assert browser(owner).post(f"/posts/{created}/pin").status_code == 200


def test_menu_shows_only_what_the_role_can_use() -> None:
    plain = browser(add_user("plain")).get("/more").text
    assert "조직 관리" not in plain and "내가 관리하는 그룹" not in plain
    assert "비밀번호 변경" in plain

    manager = add_user("manager")
    add_group(manager)
    page = browser(manager).get("/more").text
    assert "내가 관리하는 그룹" in page and "조직 관리" not in page

    page = browser(add_user("admin")).get("/more").text
    assert "조직 관리" in page and "그룹 생성과" in page and "비밀번호 변경" not in page


def test_home_lists_visible_channels_and_empty_managed_group() -> None:
    manager = add_user("manager")
    group = add_group(manager, "빈그룹")
    page = browser(manager).get("/").text
    assert "빈그룹" in page and f"/groups/{group.id}/settings" in page


def admin_row() -> User | None:
    async def load() -> User | None:
        async with SessionLocal() as session:
            return await session.scalar(select(User).where(User.email == "admin@test.local"))

    return run(load())


def login_attempt(email: str, password: str):
    anonymous = Browser()
    anonymous.refresh_token()
    return anonymous, anonymous.post("/login", data={"email": email, "password": password})


def first_login() -> Browser:
    signed_in, response = login_attempt("admin@test.local", PASSWORD)
    assert response.headers["HX-Redirect"] == PIN_CHANGE_PATH
    signed_in.refresh_token()
    return signed_in


def change(signed_in, current: str, new: str, confirm: str | None = None):
    data = {"current_pin": current, "new_pin": new, "confirm_pin": confirm or new}
    return signed_in.post(PIN_CHANGE_PATH, data=data)


def test_first_login_creates_row_from_env_without_password_hash() -> None:
    assert admin_row() is None
    first_login()
    row = admin_row()
    assert row.password_hash is None and row.pin_hash is None and row.is_active


def test_env_password_is_the_only_admin_password() -> None:
    _, response = login_attempt("admin@test.local", "wrong-password")
    assert "HX-Redirect" not in response.headers
    assert admin_row() is None


def test_stored_password_hash_cannot_log_in_as_admin() -> None:
    add_user("admin")
    _, response = login_attempt("admin@test.local", "other-password")
    assert "HX-Redirect" not in response.headers


def test_empty_env_password_disables_admin_login(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "admin_password", "")
    _, response = login_attempt("admin@test.local", "")
    assert "HX-Redirect" not in response.headers


def test_login_email_is_case_insensitive_and_allows_underscore_domain(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "admin_email", "admin@baro_system.com")
    _, response = login_attempt("ADMIN@baro_system.com", PASSWORD)
    assert response.headers["HX-Redirect"] == PIN_CHANGE_PATH


def test_login_input_does_not_use_browser_email_validation() -> None:
    anonymous = Browser()
    page = anonymous.get("/login").text
    assert 'type="email"' not in page and 'inputmode="email"' in page


def test_initial_pin_does_not_open_admin_mode() -> None:
    signed_in = first_login()
    response = signed_in.post("/admin/pin", data={"pin": INITIAL_PIN})
    assert response.headers["HX-Redirect"] == PIN_CHANGE_PATH
    assert signed_in.get("/admin/org").headers["location"] == PIN_CHANGE_PATH


@pytest.mark.parametrize("path", ["/", "/groups", "/admin/org", "/admin/pin"])
def test_every_page_waits_for_pin_change(path: str) -> None:
    response = first_login().get(path)
    assert response.status_code == 303 and response.headers["location"] == PIN_CHANGE_PATH


def test_first_change_stores_only_a_hash_and_opens_admin_mode() -> None:
    signed_in = first_login()
    assert change(signed_in, INITIAL_PIN, "739251").headers["HX-Redirect"] == "/admin/org"
    assert signed_in.get("/admin/org").status_code == 200
    row = admin_row()
    assert row.pin_hash and "739251" not in row.pin_hash
    assert audit_actions() == ["admin.pin_change"]


def test_new_pin_works_and_initial_pin_stops_working() -> None:
    change(first_login(), INITIAL_PIN, "739251")
    signed_in = browser_for_admin()
    assert "HX-Redirect" not in signed_in.post("/admin/pin", data={"pin": INITIAL_PIN}).headers
    assert signed_in.post("/admin/pin", data={"pin": "739251"}).headers["HX-Redirect"]


def browser_for_admin() -> Browser:
    return Browser().login("admin@test.local")


@pytest.mark.parametrize(
    ("new", "confirm", "text"),
    [
        ("123456", "123456", "연속"),
        ("654321", "654321", "연속"),
        ("777777", "777777", "같은 숫자"),
        ("12345", "12345", "6자리"),
        ("73925a", "73925a", "6자리"),
        ("739251", "739252", "서로 다릅니다"),
    ],
)
def test_weak_or_mismatched_new_pin_is_rejected(new: str, confirm: str, text: str) -> None:
    signed_in = first_login()
    response = change(signed_in, INITIAL_PIN, new, confirm)
    assert "HX-Redirect" not in response.headers and text in response.text
    assert admin_row().pin_hash is None


def test_new_pin_must_differ_from_current() -> None:
    signed_in = admin_browser(add_user("admin"))
    response = change(signed_in, ADMIN_PIN, ADMIN_PIN)
    assert "다르게" in response.text


def test_change_with_wrong_current_counts_toward_lock() -> None:
    signed_in = first_login()
    for _ in range(MAX_PIN_FAILURES):
        response = change(signed_in, "999999", "739251")
    assert "잠겼습니다" in response.text
    locked = change(signed_in, INITIAL_PIN, "739251")
    assert "HX-Redirect" not in locked.headers and admin_row().pin_hash is None


def test_voluntary_change_keeps_working_afterwards() -> None:
    signed_in = admin_browser(add_user("admin"))
    assert signed_in.get(PIN_CHANGE_PATH).status_code == 200
    assert change(signed_in, ADMIN_PIN, "739251").headers["HX-Redirect"]
    assert audit_actions()[-1] == "admin.pin_change"


def test_pin_hash_depends_on_server_secret(monkeypatch) -> None:
    add_user("admin")
    signed_in = browser_for_admin()
    monkeypatch.setattr(get_settings(), "secret_key", "another-secret-value")
    assert "HX-Redirect" not in signed_in.post("/admin/pin", data={"pin": ADMIN_PIN}).headers


def test_non_admin_cannot_open_pin_change() -> None:
    signed_in = browser(add_user("plain"))
    assert signed_in.get(PIN_CHANGE_PATH).status_code == 403
    assert change(signed_in, INITIAL_PIN, "739251").status_code == 403


def test_normal_user_never_sees_pin_requirement() -> None:
    assert browser(add_user("plain")).get("/").status_code == 200


def test_reset_pin_returns_to_initial_state_and_clears_lock() -> None:
    from app.cli import run_reset_pin

    add_user("admin")
    fail_pin(browser_for_admin(), MAX_PIN_FAILURES)
    assert run(run_reset_pin()) is True
    row = admin_row()
    assert row.pin_hash is None and row.pin_locked_until is None
    assert audit_actions()[-1] == "admin.pin_reset"
    assert first_login().get("/").headers["location"] == PIN_CHANGE_PATH


def test_reset_pin_without_admin_row() -> None:
    from app.cli import run_reset_pin

    assert run(run_reset_pin()) is False


def test_admin_password_page_is_closed() -> None:
    signed_in = browser(add_user("admin"))
    data = {"current_password": PASSWORD, "new_password": "new-password-1"}
    assert signed_in.post("/account/password", data=data).status_code == 403


def test_org_cannot_register_reserved_admin_email() -> None:
    signed_in = admin_browser(add_user("admin"))
    signed_in.post(
        "/admin/users", data={"name": "x", "email": "ADMIN@test.local", "password": "12345678"}
    )
    assert admin_row().name == "admin"
    assert (
        "이미 등록된"
        in signed_in.post(
            "/admin/users", data={"name": "x", "email": "admin@test.local", "password": "12345678"}
        ).text
    )

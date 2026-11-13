"""시스템 관리자는 환경 변수로만 지정하고, 로그인하면 바로 관리 기능이 열린다."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.templating import templates
from app.models import AuditLog, User
from tests.conftest import (
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
    assert signed_in.post("/admin/groups", data={"name": "x"}).status_code == 403


def test_removing_email_from_env_closes_open_admin_mode(monkeypatch) -> None:
    signed_in = admin_browser(add_user("admin"))
    monkeypatch.setattr(get_settings(), "admin_email", "someone@test.local")
    assert signed_in.get("/admin/org").status_code == 403


def test_admin_email_is_normalized_and_short_password_rejected() -> None:
    assert Settings(admin_email=" Admin@X.com ").admin_email == "admin@x.com"
    with pytest.raises(ValidationError):
        Settings(admin_password="short")


def test_only_system_admin_creates_groups() -> None:
    created = browser(add_user("admin")).post("/admin/groups", data={"name": "x"})
    assert created.headers["HX-Redirect"].startswith("/admin/groups/")
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
    assert response.headers["HX-Redirect"] == "/"
    signed_in.refresh_token()
    return signed_in


def test_first_login_creates_row_from_env_without_password_hash() -> None:
    assert admin_row() is None
    first_login()
    row = admin_row()
    assert row.password_hash is None and row.is_active


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
    assert response.headers["HX-Redirect"] == "/"


def test_login_input_does_not_use_browser_email_validation() -> None:
    anonymous = Browser()
    page = anonymous.get("/login").text
    assert 'type="email"' not in page and 'inputmode="email"' in page


def test_first_login_opens_admin_pages_without_pin() -> None:
    signed_in = first_login()
    assert signed_in.get("/").status_code == 200
    assert signed_in.get("/admin/org").status_code == 200


def test_admin_bar_sits_below_brand_with_company_name(monkeypatch) -> None:
    monkeypatch.setitem(templates.env.globals, "company_name", "가나상사")
    page = browser(add_user("admin")).get("/").text
    assert '"brand-name">가나상사</span>' in page and '"brand-name">바로채널</span>' not in page
    assert '<small class="brand-by">Powered by 바로Channel</small>' in page
    assert page.index('class="user-bar"') < page.index('class="admin-bar"')
    assert 'class="admin-bar"' not in browser(add_user("plain")).get("/").text


def test_default_brand_hides_system_mark(monkeypatch) -> None:
    monkeypatch.setitem(templates.env.globals, "company_name", "바로채널")
    page = browser(add_user("plain")).get("/").text
    assert '"brand-name">바로채널</span>' in page and "brand-by" not in page


def test_logout_closes_admin_mode() -> None:
    signed_in = browser(add_user("admin"))
    signed_in.post("/logout")
    assert signed_in.get("/admin/org").status_code == 303


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

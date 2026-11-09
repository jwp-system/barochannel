"""관문 1: 요청 보안. 모든 경로에 인증과 CSRF가 걸렸는지 라우트 목록으로 확인한다."""

import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import app
from app.models import AuditLog
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

PUBLIC = {
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("GET", "/healthz"),
    ("GET", "/favicon.ico"),
}
DOC_PATHS = {"/docs", "/docs/oauth2-redirect", "/openapi.json"}


def all_routes() -> list[tuple[str, str]]:
    found = []

    def walk(routes) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)
            if methods and path and path not in DOC_PATHS:
                found.extend((method, path) for method in methods - {"HEAD", "OPTIONS"})

    walk(app.routes)
    return sorted(set(found))


def concrete(path: str) -> str:
    return re.sub(r"\{scope_type\}", "channel", re.sub(r"\{(?!scope_type)[a-z_]+\}", "1", path))


def test_route_list_is_complete() -> None:
    assert len(all_routes()) >= 30


@pytest.mark.parametrize(("method", "path"), [r for r in all_routes() if r not in PUBLIC])
def test_every_route_requires_login(method: str, path: str) -> None:
    anonymous = Browser()
    anonymous.refresh_token()
    if method == "GET":
        response = anonymous.get(concrete(path))
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"
    else:
        response = anonymous.post(concrete(path), data={})
        assert response.headers.get("HX-Redirect") == "/login" or response.status_code == 422, path


@pytest.mark.parametrize(
    ("method", "path"), [r for r in all_routes() if r[0] != "GET" and r != ("POST", "/login")]
)
def test_every_change_requires_csrf(method: str, path: str) -> None:
    user = add_user("user")
    signed_in = browser(user)
    response = signed_in.http.request(method, concrete(path), data={})
    assert response.status_code == 403, path
    assert "보안 토큰" in response.json()["detail"]


def test_login_without_csrf_is_rejected(client: TestClient) -> None:
    add_user("user")
    response = client.post("/login", data={"email": "user@test.local", "password": PASSWORD})
    assert response.status_code == 403


def test_wrong_password_and_inactive_user_cannot_login() -> None:
    add_user("user")
    add_user("gone", status="inactive")
    anonymous = Browser()
    anonymous.refresh_token()
    for email, password in (("user@test.local", "wrong"), ("gone@test.local", PASSWORD)):
        response = anonymous.post("/login", data={"email": email, "password": password})
        assert "HX-Redirect" not in response.headers
        assert "맞지 않습니다" in response.text


def test_deactivated_user_loses_session_immediately() -> None:
    admin = add_user("admin")
    user = add_user("user")
    signed_in = browser(user)
    assert signed_in.get("/").status_code == 200
    response = admin_browser(admin).post(
        f"/admin/users/{user.id}", data={"name": "user", "status": "inactive"}
    )
    assert response.status_code == 200
    assert signed_in.get("/").status_code == 303


def test_admin_pages_reject_normal_user() -> None:
    signed_in = browser(add_user("user"))
    assert signed_in.get("/admin/org").status_code == 403
    assert signed_in.post("/admin/departments", data={"name": "x"}).status_code == 403


def test_admin_cannot_lock_self_out() -> None:
    admin = add_user("admin")
    response = admin_browser(admin).post(
        f"/admin/users/{admin.id}", data={"name": "admin", "status": "inactive"}
    )
    assert response.status_code == 400


def test_department_cycle_is_rejected() -> None:
    admin = admin_browser(add_user("admin"))
    admin.post("/admin/departments", data={"name": "상위"})
    admin.post("/admin/departments", data={"name": "하위", "parent_id": "1"})
    response = admin.post("/admin/departments/1", data={"name": "상위", "parent_id": "2"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "[x](javascript:alert(1))",
        "![x](javascript:alert(1))",
    ],
)
def test_markdown_does_not_emit_script(payload: str) -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    signed_in = browser(owner)
    response = signed_in.post(f"/channels/{channel.id}/posts", data={"body": payload})
    assert response.status_code == 200
    body = re.search(r'<div class="body">(.*?)</div>', response.text, re.S).group(1)
    assert "<script" not in body
    assert "<img src=" not in body or "onerror" not in body
    assert 'href="javascript' not in body and 'src="javascript' not in body


def test_author_name_is_escaped() -> None:
    owner = add_user("<b>x</b>")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    response = browser(owner).post(f"/channels/{channel.id}/posts", data={"body": "hi"})
    assert "<b>x</b>" not in response.text
    assert "&lt;b&gt;" in response.text


def test_audit_log_rejects_update_delete_truncate() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    signed_in = browser(owner)
    created = post_id(signed_in.post(f"/channels/{channel.id}/posts", data={"body": "hi"}))
    signed_in.post(f"/posts/{created}/delete")

    async def attempt(statement: str) -> None:
        async with SessionLocal() as session:
            await session.execute(text(statement))
            await session.commit()

    for statement in (
        "UPDATE audit_log SET action = 'x'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(Exception, match="append-only"):
            run(attempt(statement))


def test_permission_change_is_audited_with_before_and_after() -> None:
    owner = add_user("owner")
    other = add_user("other")
    channel = add_channel(add_group(owner))
    signed_in = browser(owner)
    url = f"/permissions/channel/{channel.id}"
    signed_in.post(url, data={"subject": f"user:{other.id}", "read": "allow"})
    signed_in.post(url, data={"subject": f"user:{other.id}", "read": "deny"})

    async def load() -> list[AuditLog]:
        from sqlalchemy import select

        async with SessionLocal() as session:
            rows = await session.scalars(
                select(AuditLog).where(AuditLog.action == "permission.change").order_by(AuditLog.id)
            )
            return list(rows)

    first, second = run(load())
    assert first.detail["before"] is None
    assert second.detail["before"]["read"] == "allow"
    assert second.detail["after"]["read"] == "deny"
    assert second.actor_id == owner.id


def test_non_manager_cannot_change_permissions_or_settings() -> None:
    owner = add_user("owner")
    outsider = add_user("outsider")
    channel = add_channel(add_group(owner))
    grant(channel, outsider, **FULL)
    signed_in = browser(outsider)
    response = signed_in.post(
        f"/permissions/channel/{channel.id}",
        data={"subject": f"user:{outsider.id}", "read": "allow"},
    )
    assert response.status_code == 403
    assert signed_in.get(f"/channels/{channel.id}/settings").status_code == 403


def test_grant_prerequisites_are_enforced_on_save() -> None:
    from sqlalchemy import select

    from app.models import PermissionGrant

    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    browser(owner).post(
        f"/permissions/channel/{channel.id}",
        data={"subject": f"user:{owner.id}", "read": "allow", "reply_write": "allow"},
    )

    async def load() -> PermissionGrant:
        async with SessionLocal() as session:
            return await session.scalar(select(PermissionGrant))

    assert run(load()).can_reply_read == "allow"


def test_grant_row_submits_on_change() -> None:
    """선택 상자가 폼 밖에 있으면 change가 전달되지 않아 저장되지 않는다."""
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, read="allow")
    page = browser(owner).get(f"/channels/{channel.id}/settings").text
    row = re.search(r"<tr hx-post=.*?</tr>", page, re.S).group(0)
    assert f'hx-post="/permissions/channel/{channel.id}"' in row
    assert 'hx-trigger="change"' in row and 'hx-include="this"' in row
    assert f"user:{owner.id}" in row and 'name="read"' in row


def test_session_survives_clock_stepping_backwards(monkeypatch) -> None:
    """WSL은 시계를 주기적으로 되돌린다. 방금 받은 쿠키가 미래의 것으로 거부되면 안 된다."""
    user = add_user("user")
    signed_in = browser(user)
    behind = time.time() - 3
    monkeypatch.setattr("itsdangerous.timed.time.time", lambda: behind)
    assert signed_in.get("/").status_code == 200


def test_session_still_expires_after_max_age(monkeypatch) -> None:
    signed_in = browser(add_user("user"))
    later = time.time() + get_settings().session_max_age_seconds + 1
    monkeypatch.setattr("itsdangerous.timed.time.time", lambda: later)
    assert signed_in.get("/").status_code == 303


def test_tampered_session_is_rejected() -> None:
    signed_in = browser(add_user("user"))
    cookie = signed_in.http.cookies["baro_session"]
    signed_in.http.cookies.clear()
    signed_in.http.cookies.set("baro_session", "x" + cookie[1:], domain="testserver.local")
    assert signed_in.get("/").status_code == 303


def test_static_assets_are_versioned_and_cached(client: TestClient) -> None:
    page = client.get("/login").text
    url = re.search(r'src="(/static/app\.js\?v=\d+)"', page).group(1)
    response = client.get(url)
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert re.search(r'href="/static/app\.css\?v=\d+"', page)


def test_password_check_does_not_block_event_loop() -> None:
    import asyncio

    from app.core.security import check_password

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.001)
                ticks += 1

        task = asyncio.create_task(ticker())
        await check_password(None, "password")
        task.cancel()
        return ticks

    assert run(scenario()) > 3

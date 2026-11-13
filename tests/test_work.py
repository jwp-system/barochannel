"""업무 채널(v3): 업무 화면, API 중계, 남기고 닫기, 상세 화면, 서버 훅. 외부 시스템은 샘플 할 일."""

import json
import re
import sys
import time
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models import AuditLog, Post, WorkCommand, WorkMessage
from app.services import work
from app.services.work import (
    FrameSize,
    MessageError,
    ScreenError,
    api_parts,
    check_endpoint,
    load_screen,
    parse_message,
    screens,
    verify_webhook,
    webhook_signature,
)
from tests.conftest import (
    FULL,
    Browser,
    add_channel,
    add_group,
    add_user,
    browser,
    grant,
    run,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "samples"))
from todo_api import create_app  # noqa: E402

TOKEN = "todo-test-token"
ENDPOINT = "http://todo.test/todos"


@pytest.fixture
def todo():
    """매 시험마다 비어 있는 샘플 할 일 시스템을 외부 시스템 자리에 끼운다."""
    app = create_app(":memory:", TOKEN)
    work.transport = httpx2.ASGITransport(app=app)
    work.limiter.counts.clear()
    yield app
    work.transport = None


@pytest.fixture
def office(todo):
    owner = add_user("owner")
    channel = add_channel(add_group(owner), "할 일", "work")
    grant(channel, owner, **FULL)
    writer = add_user("writer")
    grant(channel, writer, **FULL)
    reader = add_user("reader")
    grant(channel, reader, read="allow", reply_read="allow")
    return owner, channel, writer, reader


def register(signed_in, channel, **data):
    return signed_in.post(
        f"/channels/{channel.id}/work-commands",
        data={
            "name": "add",
            "label": "할 일 등록",
            "target": "todo/index",
            "endpoint_url": ENDPOINT,
            "api_token": TOKEN,
            **data,
        },
    )


def command_id(name: str = "add") -> int:
    async def load() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(WorkCommand.id).where(WorkCommand.name == name))

    return run(load())


@pytest.fixture
def ready(office):
    owner, channel, *_ = office
    response = register(browser(owner), channel)
    assert response.headers.get("HX-Refresh") == "true", response.text
    return office


def call(signed_in: Browser, method: str, url: str, body=None, **headers):
    """업무 화면의 SDK처럼 JSON과 CSRF 머리글로 부른다."""
    sent = {"X-CSRF-Token": signed_in.token, **headers}
    content = None
    if body is not None:
        sent["Content-Type"] = "application/json"
        content = json.dumps(body, ensure_ascii=False).encode()
    return signed_in.http.request(method, url, headers=sent, content=content)


def api(signed_in: Browser, method: str, path: str = "", body=None, **headers):
    return call(signed_in, method, f"/works/{command_id()}/api/{path}", body, **headers)


def leave(signed_in: Browser, body: dict, key: str = "popup-key-0001"):
    return call(
        signed_in, "POST", f"/works/{command_id()}/messages", body, **{"Idempotency-Key": key}
    )


def count(model) -> int:
    async def load() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(func.count()).select_from(model))

    return run(load())


def audits(action: str) -> list[AuditLog]:
    async def load() -> list[AuditLog]:
        async with SessionLocal() as session:
            return list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
                )
            )

    return run(load())


def context_of(page) -> dict:
    found = re.search(
        r'<script type="application/json" id="baro-context">(.*?)</script>', page.text
    )
    return json.loads(found.group(1))


def created(signed_in: Browser, title: str = "견적서 회신") -> dict:
    response = api(signed_in, "POST", "", {"title": title, "priority": "높음"})
    assert response.status_code == 201, response.text
    return response.json()


# 화면 정의


def write_screen(folder: Path, toml: str, pages=("index",)) -> Path:
    folder.mkdir(parents=True)
    (folder / "screen.toml").write_text(toml, encoding="utf-8")
    for page in pages:
        (folder / f"{page}.html").write_text("", encoding="utf-8")
    return folder


def test_screens_are_loaded_and_checked(tmp_path) -> None:
    todo = screens()["todo"]
    assert todo.pages == {"index": "할 일 등록", "find": "할 일 찾기"}
    assert todo.detail == "detail"
    assert todo.allows("POST", ("17", "actions", "done"))
    assert todo.allows("GET", ())
    assert not todo.allows("DELETE", ("17",))
    assert not todo.allows("GET", ("17", "secret"))

    good = '[pages]\nindex = "등록"\n'
    assert load_screen(write_screen(tmp_path / "ok", good)).api == ()
    with pytest.raises(ScreenError, match="find.html"):
        load_screen(write_screen(tmp_path / "missing", '[pages]\nfind = "찾기"\n', pages=()))
    with pytest.raises(ScreenError, match="메서드"):
        load_screen(write_screen(tmp_path / "method", 'api = ["TRACE x"]\n' + good))
    with pytest.raises(ScreenError, match="경로"):
        load_screen(write_screen(tmp_path / "dots", 'api = ["GET ../admin"]\n' + good))
    with pytest.raises(ScreenError, match="폴더 이름"):
        load_screen(write_screen(tmp_path / "Bad-Name", good))


def test_screen_sizes_come_from_definition(tmp_path) -> None:
    todo = screens()["todo"]
    assert todo.size_for("index") == FrameSize(560, 720)
    assert todo.size_for("find") == FrameSize(720, 760)
    assert todo.size_for("detail") == FrameSize(420, 720)

    pages = '[pages]\nindex = "등록"\n'
    sized = load_screen(write_screen(tmp_path / "px", "size = { width = 900 }\n" + pages))
    assert sized.size_for("index") == FrameSize(900, 720)
    full = load_screen(write_screen(tmp_path / "full", 'size = "full"\n' + pages))
    assert full.size_for("index").as_dict() == {"width": None, "height": None}
    for index, bad in enumerate(
        (
            'size = "huge"',
            "size = { width = 100 }",
            "size = { width = 640, depth = 3 }",
            'size = { width = "640" }',
            '[sizes]\nnope = "small"',
        )
    ):
        toml = pages + bad + "\n" if bad.startswith("[") else bad + "\n" + pages
        with pytest.raises(ScreenError, match="size"):
            load_screen(write_screen(tmp_path / f"bad{index}", toml))


def test_api_paths_are_split_strictly() -> None:
    assert api_parts("") == ()
    assert api_parts("17/actions/done") == ("17", "actions", "done")
    for bad in ("..", "a/../b", "a//b", "a/", "./a", "a b", "a%2Fb", "한글"):
        assert api_parts(bad) is None, bad


def test_message_is_checked() -> None:
    def parse(raw, **options):
        return parse_message(raw, **({"detail_allowed": True, "reply_allowed": True} | options))

    message = parse(
        {
            "text": " 등록했습니다 ",
            "fields": [{"label": "상태", "value": "할 일"}, {"label": "번호", "value": 17}],
            "detail": {"id": 17},
            "link": {"url": "http://todo.test/todos/17"},
            "reply_to": 3,
        }
    )
    assert message.text == "등록했습니다"
    assert message.fields[1] == {"label": "번호", "value": "17"}
    assert message.detail == {"id": "17"} and message.reply_to == 3
    assert message.link_label == "열기"
    bad = [
        ({}, "text"),
        ({"text": "x" * 1001}, "1000자"),
        ({"text": "a", "fields": [{"label": "l", "value": "v"}] * 7}, "6개"),
        ({"text": "a", "fields": [{"value": "v"}]}, "label"),
        ({"text": "a", "detail": {"bad key": "1"}}, "이름"),
        ({"text": "a", "detail": {}}, "detail"),
        ({"text": "a", "link": {"url": "http://evil.test/x"}}, "허용된 호스트"),
        ({"text": "a", "link": {"url": "javascript:alert(1)"}}, "허용된 호스트"),
        ({"text": "a", "reply_to": True}, "글 번호"),
        ({"text": ["a"]}, "글자"),
    ]
    for raw, error in bad:
        with pytest.raises(MessageError, match=error):
            parse(raw)
    with pytest.raises(MessageError, match="상세 화면"):
        parse({"text": "a", "detail": {"id": "1"}}, detail_allowed=False)
    with pytest.raises(MessageError, match="답글"):
        parse({"text": "a", "reply_to": 1}, reply_allowed=False)


def test_endpoint_must_be_allowed_host() -> None:
    assert check_endpoint("http://todo.test/todos/") == ENDPOINT
    for bad in (
        "ftp://todo.test",
        "http://evil.test/x",
        "http://u:p@todo.test",
        "http://todo.test?a",
    ):
        with pytest.raises(ValueError):
            check_endpoint(bad)


# 명령어 관리


def test_manager_registers_command_with_screen(office) -> None:
    owner, channel, writer, _ = office
    signed_in = browser(owner)
    blocked = register(signed_in, channel, endpoint_url="http://evil.test/todos")
    assert blocked.status_code == 400 and "허용되지 않은 호스트" in blocked.json()["detail"]
    assert register(signed_in, channel, target="todo/nothing").status_code == 400
    assert register(signed_in, channel, target="nothing/index").status_code == 400
    assert register(browser(writer), channel).status_code == 403
    assert count(WorkCommand) == 0
    assert register(signed_in, channel).headers.get("HX-Refresh") == "true"
    assert register(signed_in, channel).status_code == 409
    page = signed_in.get(f"/channels/{channel.id}/settings").text
    assert "todo/index" in page and "할 일 찾기 · todo/find" in page


def test_command_list_opens_screen_for_writers(ready) -> None:
    _, channel, writer, reader = ready
    rows = browser(writer).get(f"/channels/{channel.id}/commands").json()
    assert rows == [
        {
            "name": "add",
            "label": "할 일 등록",
            "description": "",
            "url": f"/works/{command_id()}/pages/index",
            "size": {"width": 560, "height": 720},
        }
    ]
    assert browser(reader).get(f"/channels/{channel.id}/commands").json() == []


def test_screen_page_has_context_and_strict_policy(ready) -> None:
    owner, channel, writer, reader = ready
    page = browser(writer).get(f"/works/{command_id()}/pages/index")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert "connect-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "SAMEORIGIN"
    context = context_of(page)
    assert context["user"]["name"] == "writer" and context["channel"]["id"] == channel.id
    assert context["can_leave"] and context["reply_to"] is None
    assert re.fullmatch(r"[0-9a-f]{32}", context["key"])
    assert "남기고 닫기" in page.text and "<script>" not in page.text
    assert browser(writer).get(f"/works/{command_id()}/pages/find").status_code == 200
    assert browser(writer).get(f"/works/{command_id()}/pages/detail").status_code == 404
    assert browser(reader).get(f"/works/{command_id()}/pages/index").status_code == 403
    outsider = browser(add_user("outsider"))
    assert outsider.get(f"/works/{command_id()}/pages/index").status_code == 404
    browser(owner).post(f"/work-commands/{command_id()}/archive")
    assert browser(writer).get(f"/works/{command_id()}/pages/index").status_code == 404


# API 중계


def test_relay_calls_external_api_as_the_user(ready) -> None:
    _, _, writer, _ = ready
    signed_in = browser(writer)
    options = api(signed_in, "GET", "options")
    assert options.status_code == 200 and "높음" in options.json()["priorities"]
    first = api(
        signed_in,
        "POST",
        "",
        {"title": "견적서 회신", "priority": "높음"},
        **{"Idempotency-Key": "popup-key-0001"},
    )
    again = api(
        signed_in,
        "POST",
        "",
        {"title": "견적서 회신", "priority": "높음"},
        **{"Idempotency-Key": "popup-key-0001"},
    )
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    # 요청한 사람은 퍼센트 인코딩된 머리글로 간다.
    assert first.json()["created_by"] == "writer"
    found = api(signed_in, "GET", "?q=견적")
    assert found.json()["total"] == 1
    detail = api(signed_in, "GET", first.json()["id"])
    assert detail.json()["actions"][0]["key"] == "start"
    done = api(signed_in, "POST", f"{first.json()['id']}/actions/start")
    assert done.json()["status"] == "doing" and done.json()["updated_by"] == "writer"
    logged = audits("work.api")
    assert len(logged) == 3  # GET은 남기지 않는다
    assert logged[-1].detail["path"] == f"/{first.json()['id']}/actions/start"


def test_relay_passes_validation_errors_through(ready) -> None:
    _, _, writer, _ = ready
    response = api(browser(writer), "POST", "", {"title": "", "priority": "급함"})
    assert response.status_code == 422
    assert set(response.json()["errors"]) == {"title", "priority"}
    missing = api(browser(writer), "GET", "999")
    assert missing.status_code == 404 and "찾을 수 없습니다" in missing.json()["detail"]


def test_relay_only_allows_listed_paths_and_permissions(ready) -> None:
    _, _, writer, reader = ready
    signed_in = browser(writer)
    item = created(signed_in)
    assert api(signed_in, "PATCH", item["id"], {"status": "done"}).status_code == 404
    assert api(signed_in, "GET", f"{item['id']}/secret").status_code == 404
    assert api(signed_in, "GET", "options/%2E%2E/1").status_code == 404
    assert api(signed_in, "GET", "1%2Factions").status_code == 404
    assert api(signed_in, "POST", "", "not an object").status_code == 422
    raw = signed_in.http.request(
        "POST",
        f"/works/{command_id()}/api/",
        headers={"X-CSRF-Token": signed_in.token},
        content=b"{broken",
    )
    assert raw.status_code == 400
    # 읽기만 되는 사람은 조회만 한다.
    viewer = browser(reader)
    assert api(viewer, "GET", item["id"]).status_code == 200
    assert api(viewer, "POST", f"{item['id']}/actions/done").status_code == 403
    # CSRF 없는 변경은 막힌다.
    plain = signed_in.http.post(f"/works/{command_id()}/api/{item['id']}/actions/done")
    assert plain.status_code == 403
    assert api(browser(add_user("outsider")), "GET", "options").status_code == 404


def test_relay_hides_external_failures(ready, monkeypatch) -> None:
    _, _, writer, _ = ready
    signed_in = browser(writer)

    def answer(response):
        work.transport = httpx2.MockTransport(lambda request: response)

    answer(httpx2.Response(200, text="<html>"))
    assert api(signed_in, "GET", "options").status_code == 502
    answer(httpx2.Response(302, headers={"Location": "http://evil.test/"}))
    assert api(signed_in, "GET", "options").status_code == 502
    answer(httpx2.Response(500, json={"trace": "secret"}))
    failed = api(signed_in, "GET", "options")
    assert failed.status_code == 502 and "secret" not in failed.text
    answer(httpx2.Response(200, json={"ok": 1}, headers={"Set-Cookie": "a=b"}))
    passed = api(signed_in, "GET", "options")
    assert passed.json() == {"ok": 1} and "set-cookie" not in passed.headers

    def broken(request):
        raise httpx2.ConnectError("down", request=request)

    work.transport = httpx2.MockTransport(broken)
    assert api(signed_in, "GET", "options").status_code == 502

    sent = {}

    def capture(request):
        sent.update(request.headers)
        return httpx2.Response(200, json={})

    work.transport = httpx2.MockTransport(capture)
    api(signed_in, "GET", "options")
    assert sent["authorization"] == f"Bearer {TOKEN}"
    assert sent["x-baro-user-email"] == "writer@test.local"
    assert sent["x-baro-command"] == "add"
    monkeypatch.setattr(work.limiter, "limit", 1)
    work.limiter.counts.clear()
    assert api(signed_in, "GET", "options").status_code == 200
    assert api(signed_in, "GET", "options").status_code == 429


def test_people_lookup_for_assignee(ready) -> None:
    _, _, writer, _ = ready
    add_user("이영업")
    found = browser(writer).get(f"/works/{command_id()}/people", params={"q": "영업"}).json()
    assert [row["name"] for row in found] == ["이영업"]
    assert browser(writer).get(f"/works/{command_id()}/people").json() == []


# 남기고 닫기


def test_leave_message_adds_card_once(ready) -> None:
    _, channel, writer, _ = ready
    signed_in = browser(writer)
    item = created(signed_in)
    body = {
        "text": f"할 일을 등록했습니다: {item['title']}",
        "fields": [{"label": "상태", "value": "할 일"}, {"label": "우선순위", "value": "높음"}],
        "detail": {"id": item["id"]},
        "link": {"label": "할 일 시스템에서 열기", "url": f"http://todo.test/todos/{item['id']}"},
    }
    first = leave(signed_in, body)
    assert first.status_code == 201
    again = leave(signed_in, body)
    assert again.status_code == 200 and again.json() == first.json()
    assert count(WorkMessage) == 1 and audits("work.message")
    post = first.json()["post_id"]
    page = signed_in.get(f"/channels/{channel.id}").text
    assert "할 일을 등록했습니다: 견적서 회신" in page and "우선순위" in page
    assert f'data-work-frame="/works/messages/{post}/detail"' in page
    assert "할 일 시스템에서 열기" in page
    # 메시지는 고칠 수 없고, 작성자는 지울 수 있다.
    assert signed_in.get(f"/posts/{post}/edit").status_code == 403
    assert signed_in.post(f"/posts/{post}/delete").status_code == 200
    assert leave(signed_in, {**body}, key="popup-key-0002").status_code == 201


def test_leave_message_is_checked(ready) -> None:
    _, _, writer, reader = ready
    signed_in = browser(writer)
    assert leave(signed_in, {"text": ""}).status_code == 422
    assert leave(signed_in, {"text": "a"}, key="short").status_code == 400
    assert leave(browser(reader), {"text": "a"}).status_code == 403
    no_csrf = signed_in.http.post(
        f"/works/{command_id()}/messages",
        headers={"Idempotency-Key": "popup-key-0001"},
        json={"text": "a"},
    )
    assert no_csrf.status_code == 403
    assert count(Post) == 0


def test_text_is_escaped_in_card(ready) -> None:
    _, channel, writer, _ = ready
    signed_in = browser(writer)
    leave(signed_in, {"text": "<img src=x onerror=alert(1)> **굵게**"})
    page = signed_in.get(f"/channels/{channel.id}").text
    assert "<img src=x" not in page and "&lt;img src=x" in page
    assert "<strong>굵게</strong>" not in page


# 상세 화면


def test_detail_screen_replies_in_thread(ready) -> None:
    _, channel, writer, reader = ready
    signed_in = browser(writer)
    item = created(signed_in)
    post = leave(signed_in, {"text": "등록", "detail": {"id": item["id"]}}).json()["post_id"]

    page = signed_in.get(f"/works/messages/{post}/detail")
    assert page.status_code == 200 and "할 일 상세" in page.text
    context = context_of(page)
    assert context["params"] == {"id": item["id"]} and context["reply_to"] == post
    assert context["can_write"] and context["can_leave"]
    # 읽기만 되는 사람은 볼 수 있지만 처리하거나 남길 수 없다.
    viewed = browser(reader).get(f"/works/messages/{post}/detail")
    assert viewed.status_code == 200
    assert not context_of(viewed)["can_write"] and not context_of(viewed)["can_leave"]
    assert "남기고 닫기" not in viewed.text

    api(signed_in, "POST", f"{item['id']}/actions/done")
    reply = leave(
        signed_in,
        {
            "text": "완료 처리했습니다.",
            "fields": [{"label": "상태", "value": "할 일 → 완료"}],
            "reply_to": post,
        },
        key=context["key"],
    )
    assert reply.status_code == 201
    thread = signed_in.get(f"/posts/{post}/thread").text
    assert "완료 처리했습니다." in thread and "할 일 → 완료" in thread
    # 상세 값 없이 남긴 답글에는 상세 화면이 없다.
    assert (
        browser(reader).get(f"/works/messages/{reply.json()['post_id']}/detail").status_code == 404
    )


def test_reply_needs_same_channel_and_permission(ready) -> None:
    owner, channel, writer, _ = ready
    other = add_channel(add_group(owner), "다른 채널", "talk")
    grant(other, writer, **FULL)
    signed_in = browser(writer)
    talk = signed_in.post(f"/channels/{other.id}/posts", data={"body": "안녕"})
    talk_id = int(re.search(r'id="post-(\d+)"', talk.text).group(1))
    assert leave(signed_in, {"text": "a", "reply_to": talk_id}).status_code == 400
    assert (
        leave(signed_in, {"text": "a", "reply_to": 99999}, key="popup-key-0003").status_code == 404
    )


def test_detail_is_closed_when_command_is_archived(ready) -> None:
    owner, channel, writer, _ = ready
    signed_in = browser(writer)
    post = leave(signed_in, {"text": "등록", "detail": {"id": "1"}}).json()["post_id"]
    no_detail = leave(signed_in, {"text": "그냥 남김"}, key="popup-key-0002").json()["post_id"]
    assert signed_in.get(f"/works/messages/{no_detail}/detail").status_code == 404
    browser(owner).post(f"/work-commands/{command_id()}/archive")
    assert signed_in.get(f"/works/messages/{post}/detail").status_code == 404
    page = signed_in.get(f"/channels/{channel.id}").text
    assert "등록" in page and "data-work-frame" not in page


# 서버 훅


def hook(command: int, secret: str, body: dict, event: str = "evt-1", **headers):
    content = json.dumps(body, ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    sent = {
        "Content-Type": "application/json",
        "X-Baro-Timestamp": timestamp,
        "X-Baro-Signature": webhook_signature(secret, timestamp, content),
        "X-Baro-Event-Id": event,
        **headers,
    }
    return Browser().http.post(f"/hooks/work/{command}", content=content, headers=sent)


def issue_key(owner) -> str:
    response = browser(owner).post(f"/work-commands/{command_id()}/webhook")
    return re.search(r'서명 키 <input readonly value="([0-9a-f]{64})"', response.text).group(1)


def test_server_hook_adds_message(ready) -> None:
    owner, channel, writer, _ = ready
    secret = issue_key(owner)
    assert secret not in browser(owner).get(f"/channels/{channel.id}/settings").text
    body = {"text": "할 일 시스템에서 상태를 바꿨습니다.", "detail": {"id": "1"}, "actor": "정영업"}
    accepted = hook(command_id(), secret, body)
    assert accepted.status_code == 202
    replay = hook(command_id(), secret, body)
    assert replay.json()["duplicate"] and count(WorkMessage) == 1
    page = browser(writer).get(f"/channels/{channel.id}").text
    assert "외부 시스템 · 정영업" in page and "상태를 바꿨습니다" in page

    assert hook(command_id(), "wrong", body, "evt-2").status_code == 401
    assert hook(999, secret, body, "evt-2").status_code == 401
    assert hook(command_id(), secret, body, "bad id!").status_code == 400
    assert hook(command_id(), secret, {"text": ""}, "evt-3").status_code == 422
    assert hook(command_id(), secret, {**body, "reply_to": 1}, "evt-4").status_code == 422
    browser(owner).post(f"/work-commands/{command_id()}/webhook/delete")
    assert hook(command_id(), secret, body, "evt-5").status_code == 401


def test_sample_todo_status_change_reaches_channel(ready) -> None:
    """할 일 시스템의 자체 상태 변경이 서명된 훅으로 채널에 메시지를 남긴다."""
    owner, channel, writer, _ = ready
    secret = issue_key(owner)
    sent = []
    app = create_app(":memory:", TOKEN, secret, lambda body, headers: sent.append((body, headers)))
    work.transport = httpx2.ASGITransport(app=app)
    item = created(browser(writer))

    async def patch():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://todo.test"
        ) as client:
            return await client.patch(
                f"/todos/{item['id']}",
                json={"status": "done", "actor": "정영업"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )

    assert run(patch()).status_code == 200
    ((body, headers),) = sent
    response = Browser().http.post(f"/hooks/work/{command_id()}", content=body, headers=headers)
    assert response.status_code == 202, response.text
    page = browser(writer).get(f"/channels/{channel.id}").text
    assert "할 일 → 완료" in page and "외부 시스템 · 정영업" in page


def test_webhook_signature_window() -> None:
    body = b'{"text": "a"}'
    now = 1_800_000_000
    good = webhook_signature("k", str(now), body)
    assert verify_webhook("k", str(now), good, body, now + 299)
    assert not verify_webhook("k", str(now), good, body, now + 301)
    assert not verify_webhook("", str(now), good, body, now)
    assert not verify_webhook("k", "abc", good, body, now)
    assert not verify_webhook("k", str(now), good, b"{}", now)

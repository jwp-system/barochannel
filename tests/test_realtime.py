"""관문 1: 실시간 전송에도 일반 요청과 같은 권한 판정이 적용되는지 확인한다."""

import asyncio

import asyncpg
import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import PermissionGrant
from app.routers.events import format_event, stream
from app.services.delivery import messages_for
from app.services.realtime import NOTIFY_CHANNEL, Event, Hub, hub, publish
from tests.conftest import (
    FULL,
    add_channel,
    add_group,
    add_user,
    browser,
    grant,
    post_id,
    run,
)


@pytest.fixture
def space():
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    return owner, channel


def created(signed_in, channel, body="비밀내용") -> int:
    return post_id(signed_in.post(f"/channels/{channel.id}/posts", data={"body": body}))


def deliver(kind: str, channel, post: int, actor, user):
    return run(messages_for(Event(kind, channel.id, post, actor.id), user.id))


def test_reader_receives_rendered_card_on_channel_event(space) -> None:
    owner, channel = space
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    post = created(browser(owner), channel)
    ((name, html),) = deliver("created", channel, post, owner, reader)
    assert name == f"ch-{channel.id}" and "비밀내용" in html


def test_stranger_gets_disconnected_without_content(space) -> None:
    owner, channel = space
    post = created(browser(owner), channel)
    assert deliver("created", channel, post, owner, add_user("stranger")) is None


def test_write_only_user_receives_nothing_about_others_posts(space) -> None:
    owner, channel = space
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    post = created(browser(owner), channel)
    assert deliver("created", channel, post, owner, writer) == []
    assert deliver("updated", channel, post, owner, writer) == []


def test_pins_event_filters_for_write_only_user(space) -> None:
    owner, channel = space
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    author = browser(owner)
    post = created(author, channel)
    author.post(f"/posts/{post}/pin")
    ((_, html),) = deliver("pins", channel, post, owner, writer)
    assert "비밀내용" not in html


def test_reply_event_requires_reply_read(space) -> None:
    owner, channel = space
    author = browser(owner)
    parent = created(author, channel)
    child = post_id(author.post(f"/posts/{parent}/replies", data={"body": "답글비밀"}))
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    assert deliver("created", channel, child, owner, reader) == []

    full = add_user("full")
    grant(channel, full, **FULL)
    messages = dict(deliver("created", channel, child, owner, full))
    assert "답글비밀" in messages[f"thread-{parent}"]
    assert "답글 1개" in messages[f"post-{parent}"]


def test_update_refreshes_open_thread_root_only_for_reply_readers(space) -> None:
    owner, channel = space
    post = created(browser(owner), channel)
    full = add_user("full")
    grant(channel, full, **FULL)
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    assert f"root-{post}" in dict(deliver("updated", channel, post, owner, full))
    assert f"root-{post}" not in dict(deliver("updated", channel, post, owner, reader))


def test_author_receives_own_post_for_other_tabs(space) -> None:
    owner, channel = space
    post = created(browser(owner), channel)
    ((name, _),) = deliver("created", channel, post, owner, owner)
    assert name == f"ch-{channel.id}"


def test_permission_revoked_between_events_stops_delivery(space) -> None:
    owner, channel = space
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    post = created(browser(owner), channel)
    assert deliver("updated", channel, post, owner, reader)

    async def revoke() -> None:
        from sqlalchemy import update

        async with SessionLocal() as session:
            await session.execute(
                update(PermissionGrant)
                .where(PermissionGrant.subject_id == reader.id)
                .values(can_read="deny")
            )
            await session.commit()

    run(revoke())
    assert deliver("updated", channel, post, owner, reader) is None


def test_inactive_user_is_disconnected(space) -> None:
    owner, channel = space
    post = created(browser(owner), channel)
    gone = add_user("gone", status="inactive")
    grant(channel, gone, read="allow")
    assert deliver("updated", channel, post, owner, gone) is None


def test_events_route_rejects_user_without_channel_access(space) -> None:
    _, channel = space
    assert browser(add_user("stranger")).get(f"/events/{channel.id}").status_code == 404


def test_stream_ends_when_reader_loses_access(space) -> None:
    owner, channel = space
    post = created(browser(owner), channel)
    stranger = add_user("stranger")

    async def scenario() -> list[str]:
        chunks = []
        generator = stream(stranger.id, channel.id)
        chunks.append(await anext(generator))
        hub.dispatch(Event("updated", channel.id, post, owner.id))
        chunks.extend([chunk async for chunk in generator])
        return chunks

    assert run(scenario()) == [": connected\n\n"]
    assert hub._subscribers == {}


def test_sse_connection_wraps_both_timeline_and_thread(space) -> None:
    """쓰레드 패널이 연결 밖에 있으면 답글이 실시간으로 오지 않는다."""
    owner, channel = space
    page = browser(owner).get(f"/channels/{channel.id}").text
    connect = page.index(f'sse-connect="/events/{channel.id}"')
    assert page.count("sse-connect") == 1
    assert connect < page.index('class="timeline-pane"') < page.index('id="thread"')
    assert page[:connect].rfind('<div class="app') > page[:connect].rfind(">")


def test_format_event_prefixes_every_line() -> None:
    assert format_event("x", "a\nb") == "event: x\ndata: a\ndata: b\n\n"


def test_notify_is_sent_only_after_commit_and_reaches_listener() -> None:
    async def scenario() -> tuple[list[Event], list[Event]]:
        local = Hub()
        queue = local.subscribe(7)
        local.start()
        await asyncio.sleep(0.5)
        async with SessionLocal() as session:
            await publish(session, Event("updated", 7, 1, 1))
            await session.rollback()
        async with SessionLocal() as session:
            await publish(session, Event("updated", 7, 2, 1))
            await publish(session, Event("updated", 8, 3, 1))
            await session.commit()
        received = [await asyncio.wait_for(queue.get(), 3)]
        await asyncio.sleep(0.2)
        remaining = []
        while not queue.empty():
            remaining.append(queue.get_nowait())
        await local.stop()
        return received, remaining

    received, remaining = run(scenario())
    assert [event.post_id for event in received] == [2]
    assert remaining == []


def test_listener_reconnects_after_connection_is_killed(monkeypatch) -> None:
    monkeypatch.setattr("app.services.realtime.RECONNECT_SECONDS", 0.1)
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def scenario() -> int:
        local = Hub()
        queue = local.subscribe(9)
        local.start()
        await asyncio.sleep(0.5)
        admin = await asyncpg.connect(dsn)
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE query LIKE 'LISTEN%' AND datname = current_database()"
        )
        await asyncio.sleep(1)
        await admin.execute(
            f"""SELECT pg_notify('{NOTIFY_CHANNEL}',
            '{{"kind":"updated","channel_id":9,"post_id":5,"actor_id":1}}')"""
        )
        await admin.close()
        event = await asyncio.wait_for(queue.get(), 3)
        await local.stop()
        return event.post_id

    assert run(scenario()) == 5


def test_slow_consumer_is_dropped_without_blocking_others() -> None:
    from app.services.realtime import QUEUE_SIZE

    local = Hub()
    slow = local.subscribe(1)
    for number in range(QUEUE_SIZE + 1):
        local.dispatch(Event("updated", 1, number, 1))
    assert local._subscribers == {}
    items = []
    while not slow.empty():
        items.append(slow.get_nowait())
    assert items[-1] is None

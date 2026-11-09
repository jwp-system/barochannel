"""채널별 새 글 수. 채널의 글만 세고, 소통·문서·투표 채널만, 읽기 권한이 있는 채널만 센다."""

import asyncio
import re

from app.services.delivery import count_state
from app.services.realtime import Event, Hub
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


def badge(page: str, channel_id: int) -> str | None:
    """보이는 새 글 수. 숨겨져 있으면 None."""
    found = re.search(
        rf'<b class="unread-badge" data-unread="{channel_id}"( hidden)?>.*?'
        r'<span class="unread-number">([^<]*)</span>',
        page,
        re.S,
    )
    assert found, f"{channel_id} 채널의 표시 자리가 없습니다."
    return None if found.group(1) else found.group(2)


def setup():
    owner = add_user("owner")
    group = add_group(owner, "영업")
    talk = add_channel(group, "일반")
    other = add_channel(group, "잡담")
    reader = add_user("reader")
    for channel in (talk, other):
        grant(channel, owner, **FULL)
        grant(channel, reader, **FULL)
    return owner, reader, group, talk, other


def write(signed_in, channel, body="안녕하세요") -> int:
    response = signed_in.post(f"/channels/{channel.id}/posts", data={"body": body})
    assert response.status_code == 200, response.text
    return post_id(response)


def test_existing_history_is_not_new_for_first_visit() -> None:
    owner, reader, _, talk, _ = setup()
    author = browser(owner)
    write(author, talk)
    write(author, talk)
    assert badge(browser(reader).get("/").text, talk.id) is None


def test_counts_others_channel_posts_only() -> None:
    owner, reader, group, talk, other = setup()
    viewer = browser(reader)
    viewer.get("/")  # 처음 보는 시점을 기록한다.
    author = browser(owner)
    first = write(author, talk)
    write(author, talk)
    author.post(f"/posts/{first}/replies", data={"body": "답글은 세지 않는다"})
    write(viewer, talk, "내 글은 세지 않는다")
    gone = write(author, talk)
    author.post(f"/posts/{gone}/delete")
    page = viewer.get(f"/channels/{other.id}").text
    assert badge(page, talk.id) == "2"
    # 그룹 버튼에도 점이 보인다.
    assert f'class="rail-dot" data-unread-group="{group.id}"><span' in page


def test_opening_channel_marks_read() -> None:
    owner, reader, _, talk, other = setup()
    viewer = browser(reader)
    viewer.get("/")
    write(browser(owner), talk)
    assert badge(viewer.get("/").text, talk.id) == "1"
    viewer.get(f"/channels/{talk.id}")
    assert badge(viewer.get(f"/channels/{other.id}").text, talk.id) is None


def test_read_endpoint_marks_current_channel() -> None:
    owner, reader, _, talk, _ = setup()
    viewer = browser(reader)
    viewer.get("/")
    write(browser(owner), talk)
    assert viewer.post(f"/channels/{talk.id}/read").status_code == 204
    assert badge(viewer.get("/").text, talk.id) is None
    stranger = browser(add_user("stranger"))
    assert stranger.post(f"/channels/{talk.id}/read").status_code == 404


def test_write_only_channel_never_shows_count() -> None:
    owner = add_user("owner")
    box = add_channel(add_group(owner), "건의함")
    grant(box, owner, **FULL)
    writer = add_user("writer")
    grant(box, writer, read="deny", write="allow")
    viewer = browser(writer)
    viewer.get("/")
    write(browser(owner), box)
    # 남의 글이 있다는 사실이 새지 않는다.
    assert badge(viewer.get("/").text, box.id) is None


def test_approval_channel_is_not_counted() -> None:
    owner = add_user("owner")
    group = add_group(owner)
    approval = add_channel(group, "지출결의", "approval")
    talk = add_channel(group, "일반")
    reader = add_user("reader")
    for channel in (approval, talk):
        grant(channel, owner, **FULL)
        grant(channel, reader, **FULL)
    viewer = browser(reader)
    viewer.get("/")
    owner_browser = browser(owner)
    created = owner_browser.post(
        f"/channels/{approval.id}/approvals", data={"title": "x", "body": "본문"}
    )
    post = int(created.headers["HX-Redirect"].rsplit("/", 1)[1])
    owner_browser.post(f"/approvals/{post}/submit")
    assert badge(viewer.get("/").text, approval.id) is None


def test_documents_and_polls_count() -> None:
    owner = add_user("owner")
    group = add_group(owner)
    doc = add_channel(group, "규정집", "doc")
    reader = add_user("reader")
    for channel in (doc,):
        grant(channel, owner, **FULL)
        grant(channel, reader, **FULL)
    viewer = browser(reader)
    viewer.get("/")
    browser(owner).post(f"/channels/{doc.id}/docs", data={"title": "휴가", "body": "본문"})
    assert badge(viewer.get("/").text, doc.id) == "1"


def test_count_state_lists_channels_and_groups() -> None:
    owner, reader, group, talk, other = setup()
    browser(reader).get("/")
    write(browser(owner), talk)
    state = run(count_state(reader.id))
    assert state.unread["channels"] == {str(talk.id): 1, str(other.id): 0}
    assert state.unread["groups"] == {str(group.id): 1}
    assert state.watched == {talk.id, other.id}


def test_hub_wakes_only_connections_watching_the_channel() -> None:
    async def scenario() -> tuple[bool, bool]:
        hub = Hub()
        watching, elsewhere = hub.subscribe_user(1), hub.subscribe_user(2)
        hub.watch(watching, {10})
        hub.watch(elsewhere, {20})
        hub.dispatch(Event("created", 10, 5, 3))
        return not watching.empty(), not elsewhere.empty()

    assert asyncio.run(scenario()) == (True, False)


def test_unread_event_carries_only_numbers() -> None:
    from app.routers.events import count_stream

    owner, reader, _, talk, _ = setup()
    browser(reader).get("/")
    write(browser(owner), talk, "비밀내용")

    async def messages() -> list[str]:
        stream = count_stream(reader.id)
        found = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return found

    counts, unread = run(messages())
    assert counts.startswith("event: counts\n")
    assert unread.startswith("event: unread\n") and "비밀내용" not in unread
    data = unread.split("data: ", 1)[1].strip()
    assert re.fullmatch(r'[{}":, 0-9a-z]+', data), data

"""F-30~33: 투표 채널."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.db import SessionLocal
from app.core.templating import LOCAL_TZ
from app.models import Poll, PollOption, PollVote, PollVoteHistory
from tests.conftest import FULL, add_channel, add_group, add_user, browser, grant, run


@pytest.fixture
def board():
    owner = add_user("owner")
    channel = add_channel(add_group(owner), "일정 조율", "vote")
    grant(channel, owner, **FULL)
    voter = add_user("voter")
    grant(channel, voter, read="allow")
    return owner, channel, voter


def later(hours: int = 2) -> str:
    return (datetime.now(LOCAL_TZ) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def create(signed_in, channel, *, options=("월", "화", "수"), **data):
    return signed_in.post(
        f"/channels/{channel.id}/polls",
        data={"body": "회식 요일", "closes_at": later(), "options": list(options), **data},
    )


def poll_id(channel) -> int:
    async def load() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(Poll.post_id).order_by(Poll.post_id.desc()))

    return run(load())


def options(post: int) -> list[int]:
    async def load() -> list[int]:
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(PollOption.id)
                .where(PollOption.post_id == post)
                .order_by(PollOption.position)
            )
            return list(rows)

    return run(load())


def close_now(post: int) -> None:
    async def change() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(Poll)
                .where(Poll.post_id == post)
                .values(closes_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

    run(change())


def card(signed_in, post: int) -> str:
    return signed_in.get(f"/posts/{post}/card").text


def test_writer_creates_poll_and_reader_votes(board) -> None:
    owner, channel, voter = board
    response = create(browser(owner), channel)
    assert response.headers["HX-Redirect"] == f"/channels/{channel.id}"
    post = poll_id(channel)
    first, *_ = options(post)
    signed_in = browser(voter)
    result = signed_in.post(f"/posts/{post}/vote", data={"option_ids": [first]})
    assert result.status_code == 200
    assert "1명 · 100%" in result.text and "voter" in result.text
    assert "선택 바꾸기" in result.text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"options": ["하나"]}, "선택지"),
        ({"options": ["같음", "같음"]}, "겹치"),
        ({"closes_at": "2000-01-01T00:00"}, "마감"),
        ({"closes_at": "아무때나"}, "마감"),
        ({"body": "  "}, "안건"),
    ],
)
def test_invalid_poll_is_rejected(board, data: dict, message: str) -> None:
    owner, channel, _ = board
    payload = {"body": "안건", "closes_at": later(), "options": ["가", "나"], **data}
    response = browser(owner).post(f"/channels/{channel.id}/polls", data=payload)
    assert response.status_code == 400 and message in response.json()["detail"]


def test_only_writers_create_polls_and_only_in_vote_channels(board) -> None:
    owner, channel, voter = board
    assert create(browser(voter), channel).status_code == 403
    talk = add_channel(add_group(owner, "소통"), "일반")
    grant(talk, owner, **FULL)
    assert create(browser(owner), talk).status_code == 403


def test_write_only_user_cannot_vote_even_on_own_poll(board) -> None:
    owner, channel, _ = board
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    signed_in = browser(writer)
    create(signed_in, channel)
    post = poll_id(channel)
    response = signed_in.post(f"/posts/{post}/vote", data={"option_ids": options(post)[:1]})
    assert response.status_code == 403


def test_single_choice_rejects_many_and_foreign_options(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel)
    create(browser(owner), channel, options=("다른", "투표"))
    first_poll = poll_id(channel) - 1
    ids = options(first_poll)
    other = options(poll_id(channel))
    signed_in = browser(voter)
    assert (
        signed_in.post(f"/posts/{first_poll}/vote", data={"option_ids": ids[:2]}).status_code == 400
    )
    assert (
        signed_in.post(f"/posts/{first_poll}/vote", data={"option_ids": other[:1]}).status_code
        == 400
    )
    assert signed_in.post(f"/posts/{first_poll}/vote").status_code == 400


def test_multi_select_accepts_many(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel, multi_select="true")
    post = poll_id(channel)
    response = browser(voter).post(f"/posts/{post}/vote", data={"option_ids": options(post)})
    assert response.status_code == 200 and response.text.count("1명 · 100%") == 3


def test_change_is_recorded_and_counted_as_changer(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel)
    post = poll_id(channel)
    first, second, _ = options(post)
    signed_in = browser(voter)
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [first]})
    assert "변심 0명" in card(signed_in, post)
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [first]})
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [second]})
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [first]})
    assert "변심 1명" in card(signed_in, post)

    async def history() -> list[tuple[list[int], list[int]]]:
        async with SessionLocal() as session:
            rows = await session.execute(
                select(
                    PollVoteHistory.before_option_ids, PollVoteHistory.after_option_ids
                ).order_by(PollVoteHistory.id)
            )
            return [tuple(row) for row in rows]

    assert run(history()) == [([], [first]), ([first], [second]), ([second], [first])]


def test_changer_names_are_never_shown(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel)
    post = poll_id(channel)
    first, second, _ = options(post)
    signed_in = browser(voter)
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [first]})
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [second]})
    html = card(browser(owner), post)
    assert "변심 1명" in html
    assert html.count("voter") == 1


def test_anonymous_poll_shows_counts_only(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel, anonymous="true")
    post = poll_id(channel)
    first, second, _ = options(post)
    signed_in = browser(voter)
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [first]})
    signed_in.post(f"/posts/{post}/vote", data={"option_ids": [second]})
    html = card(browser(owner), post)
    assert "1명 · 100%" in html and "익명" in html
    assert "voter" not in html and "변심" not in html


def test_results_after_close_are_hidden_until_deadline(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel, result_open="after_close")
    post = poll_id(channel)
    signed_in = browser(voter)
    response = signed_in.post(f"/posts/{post}/vote", data={"option_ids": options(post)[:1]})
    assert "결과는 마감 후 공개" in response.text
    assert "명 ·" not in response.text and "참여" not in response.text
    assert "voter" not in card(browser(owner), post)

    close_now(post)
    html = card(browser(owner), post)
    assert "1명 · 100%" in html and "voter" in html and "마감됨" in html


def test_closed_poll_refuses_votes_and_hides_controls(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel)
    post = poll_id(channel)
    close_now(post)
    signed_in = browser(voter)
    response = signed_in.post(f"/posts/{post}/vote", data={"option_ids": options(post)[:1]})
    assert response.status_code == 409
    assert 'name="option_ids"' not in card(signed_in, post)


def test_deleted_poll_refuses_votes(board) -> None:
    owner, channel, voter = board
    author = browser(owner)
    create(author, channel)
    post = poll_id(channel)
    author.post(f"/posts/{post}/delete")
    response = browser(voter).post(f"/posts/{post}/vote", data={"option_ids": options(post)[:1]})
    assert response.status_code == 409


def test_stranger_cannot_vote(board) -> None:
    owner, channel, _ = board
    create(browser(owner), channel)
    post = poll_id(channel)
    stranger = browser(add_user("stranger"))
    assert (
        stranger.post(f"/posts/{post}/vote", data={"option_ids": options(post)[:1]}).status_code
        == 404
    )


def test_vote_on_normal_post_is_not_found(board) -> None:
    owner, channel, voter = board
    talk = add_channel(add_group(owner, "소통"), "일반")
    grant(talk, owner, **FULL)
    signed_in = browser(owner)
    created = signed_in.post(f"/channels/{talk.id}/posts", data={"body": "글"})
    post = int(created.text.split('id="post-')[1].split('"')[0])
    assert signed_in.post(f"/posts/{post}/vote", data={"option_ids": [1]}).status_code == 404


def test_deadline_is_read_as_seoul_time(board) -> None:
    owner, channel, _ = board
    local = datetime.now(LOCAL_TZ).replace(second=0, microsecond=0) + timedelta(hours=3)
    create(browser(owner), channel, closes_at=local.strftime("%Y-%m-%dT%H:%M"))

    async def load() -> datetime:
        async with SessionLocal() as session:
            return await session.scalar(select(Poll.closes_at))

    assert run(load()) == local


def test_double_vote_requests_keep_one_set(board) -> None:
    owner, channel, voter = board
    create(browser(owner), channel)
    post = poll_id(channel)
    first, second, _ = options(post)
    signed_in = browser(voter)
    with ThreadPoolExecutor(6) as pool:
        futures = [
            pool.submit(signed_in.post, f"/posts/{post}/vote", data={"option_ids": [choice]})
            for choice in (first, second) * 3
        ]
        assert {future.result().status_code for future in futures} == {200}

    async def count() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(func.count()).select_from(PollVote))

    assert run(count()) == 1


def test_post_page_shows_single_post_and_redirects_replies(board) -> None:
    owner, channel, voter = board
    talk = add_channel(add_group(owner, "소통"), "일반")
    grant(talk, owner, **FULL)
    signed_in = browser(owner)
    created = signed_in.post(f"/channels/{talk.id}/posts", data={"body": "단독 글"})
    post = int(created.text.split('id="post-')[1].split('"')[0])
    reply = signed_in.post(f"/posts/{post}/replies", data={"body": "답글"})
    reply_id = int(reply.text.split('id="post-')[1].split('"')[0])
    assert "단독 글" in signed_in.get(f"/posts/{post}").text
    moved = signed_in.get(f"/posts/{reply_id}")
    assert (
        moved.status_code == 303 and moved.headers["location"] == f"/posts/{post}#post-{reply_id}"
    )
    assert browser(voter).get(f"/posts/{post}").status_code == 404

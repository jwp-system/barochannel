"""동시에 들어온 요청이 규칙을 깨지 않는지 실제 PostgreSQL 잠금으로 확인한다."""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models import Post, Reaction
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


def together(*calls):
    with ThreadPoolExecutor(len(calls)) as pool:
        return [future.result() for future in [pool.submit(call) for call in calls]]


def test_delete_and_reply_never_leave_live_reply_on_deleted_post() -> None:
    owner = add_user("owner")
    replier = add_user("replier")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    grant(channel, replier, **FULL)
    author = browser(owner)
    other = browser(replier)

    for _ in range(15):
        parent = post_id(author.post(f"/channels/{channel.id}/posts", data={"body": "글"}))
        deleted, replied = together(
            lambda parent=parent: author.post(f"/posts/{parent}/delete"),
            lambda parent=parent: other.post(f"/posts/{parent}/replies", data={"body": "답글"}),
        )
        assert sorted((deleted.status_code, replied.status_code)) in ([200, 200], [200, 409])

        async def state(parent=parent) -> tuple[bool, int]:
            async with SessionLocal() as session:
                post = await session.get(Post, parent)
                live = await session.scalar(
                    select(func.count())
                    .select_from(Post)
                    .where(Post.parent_post_id == parent, Post.deleted_at.is_(None))
                )
                return post.deleted_at is not None, live

        is_deleted, live = run(state())
        assert not (is_deleted and live), "삭제된 글에 살아 있는 답글이 남았습니다"


def test_concurrent_pins_do_not_exceed_limit() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner), pin_limit=1)
    grant(channel, owner, **FULL)
    manager = browser(owner)
    ids = [
        post_id(manager.post(f"/channels/{channel.id}/posts", data={"body": "글"}))
        for _ in range(4)
    ]
    results = together(*[lambda post=post: manager.post(f"/posts/{post}/pin") for post in ids])
    assert sorted(result.status_code for result in results) == [200, 409, 409, 409]


def test_double_click_reaction_never_fails() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    signed_in = browser(owner)
    post = post_id(signed_in.post(f"/channels/{channel.id}/posts", data={"body": "글"}))
    results = together(
        *[
            lambda: signed_in.post(f"/posts/{post}/reactions", data={"emoji": "👍"})
            for _ in range(6)
        ]
    )
    assert {result.status_code for result in results} == {200}

    async def count() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(func.count()).select_from(Reaction))

    assert run(count()) in (0, 1)

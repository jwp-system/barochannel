from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import AuditLog, Post, PostRevision
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


def write(signed_in, channel, body="본문") -> int:
    response = signed_in.post(f"/channels/{channel.id}/posts", data={"body": body})
    assert response.status_code == 200, response.text
    return post_id(response)


def reply(signed_in, parent: int, body="답글") -> int:
    response = signed_in.post(f"/posts/{parent}/replies", data={"body": body})
    assert response.status_code == 200, response.text
    return post_id(response)


def load_post(identifier: int) -> Post:
    async def load() -> Post:
        async with SessionLocal() as session:
            return await session.get(Post, identifier)

    return run(load())


def test_reader_cannot_write_and_stranger_cannot_enter(space) -> None:
    _, channel = space
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    response = browser(reader).post(f"/channels/{channel.id}/posts", data={"body": "x"})
    assert response.status_code == 403
    assert browser(add_user("stranger")).get(f"/channels/{channel.id}").status_code == 404


def test_write_only_user_sees_only_own_posts(space) -> None:
    owner, channel = space
    writer = add_user("writer")
    grant(channel, writer, write="allow", reply_read="allow", reply_write="allow")
    author = browser(owner)
    others = write(author, channel, "남의글내용")
    signed_in = browser(writer)
    mine = write(signed_in, channel, "내글내용")

    page = signed_in.get(f"/channels/{channel.id}").text
    assert "내글내용" in page and "남의글내용" not in page
    assert signed_in.get(f"/posts/{others}/card").status_code == 404
    assert signed_in.get(f"/posts/{others}/thread").status_code == 404
    assert signed_in.post(f"/posts/{others}/replies", data={"body": "x"}).status_code == 404
    assert signed_in.get(f"/posts/{mine}/thread").status_code == 200
    reply(signed_in, mine)


def test_older_page_filters_for_write_only_user(space) -> None:
    owner, channel = space
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    author = browser(owner)
    write(author, channel, "남의글내용")
    last = write(browser(writer), channel, "내글")
    page = browser(writer).get(f"/channels/{channel.id}/posts?before={last + 1}")
    assert "내글" in page.text and "남의글내용" not in page.text


def test_reply_permissions(space) -> None:
    owner, channel = space
    parent = write(browser(owner), channel)
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    signed_in = browser(reader)
    assert signed_in.get(f"/posts/{parent}/thread").status_code == 404
    assert signed_in.post(f"/posts/{parent}/replies", data={"body": "x"}).status_code == 403


def test_reply_reader_without_reply_write(space) -> None:
    owner, channel = space
    parent = write(browser(owner), channel)
    reader = add_user("reader")
    grant(channel, reader, read="allow", reply_read="allow")
    signed_in = browser(reader)
    assert signed_in.get(f"/posts/{parent}/thread").status_code == 200
    assert signed_in.post(f"/posts/{parent}/replies", data={"body": "x"}).status_code == 403


def test_threads_have_one_level(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    child = reply(signed_in, write(signed_in, channel))
    assert signed_in.post(f"/posts/{child}/replies", data={"body": "x"}).status_code == 400


def test_edit_keeps_history_and_marks_edited(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    created = write(signed_in, channel, "처음")
    response = signed_in.post(f"/posts/{created}/edit", data={"body": "나중"})
    assert "나중" in response.text and "수정됨" in response.text

    async def revisions() -> list[PostRevision]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(PostRevision)))

    (revision,) = run(revisions())
    assert (revision.version, revision.body_md) == (1, "처음")


def test_only_author_can_edit_or_delete(space) -> None:
    owner, channel = space
    created = write(browser(owner), channel)
    other = add_user("other")
    grant(channel, other, **FULL)
    signed_in = browser(other)
    assert signed_in.post(f"/posts/{created}/edit", data={"body": "x"}).status_code == 403
    assert signed_in.post(f"/posts/{created}/delete").status_code == 403


def test_admin_cannot_delete_others_post_without_being_author(space) -> None:
    owner, channel = space
    created = write(browser(owner), channel)
    admin = add_user("admin")
    grant(channel, admin, **FULL)
    assert browser(admin).post(f"/posts/{created}/delete").status_code == 403


def test_deleted_post_leaves_placeholder_keeps_body_and_audit(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    created = write(signed_in, channel, "지울내용")
    response = signed_in.post(f"/posts/{created}/delete")
    assert "삭제된 글" in response.text and "지울내용" not in response.text
    page = signed_in.get(f"/channels/{channel.id}").text
    assert "삭제된 글" in page and "지울내용" not in page

    stored = load_post(created)
    assert stored.body_md == "지울내용" and stored.deleted_by == owner.id

    async def audit() -> AuditLog:
        async with SessionLocal() as session:
            return await session.scalar(select(AuditLog).where(AuditLog.action == "post.delete"))

    assert run(audit()).target_id == created


def test_post_with_live_reply_cannot_be_deleted_until_reply_author_deletes(space) -> None:
    owner, channel = space
    replier = add_user("replier")
    grant(channel, replier, **FULL)
    author = browser(owner)
    other = browser(replier)
    created = write(author, channel)
    child = reply(other, created)
    assert author.post(f"/posts/{created}/delete").status_code == 409
    assert other.post(f"/posts/{child}/delete").status_code == 200
    assert author.post(f"/posts/{created}/delete").status_code == 200


def test_cannot_reply_to_deleted_post(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    created = write(signed_in, channel)
    signed_in.post(f"/posts/{created}/delete")
    assert signed_in.post(f"/posts/{created}/replies", data={"body": "x"}).status_code == 409


def test_reaction_toggle_counts_and_names(space) -> None:
    owner, channel = space
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    created = write(browser(owner), channel)
    signed_in = browser(reader)
    response = signed_in.post(f"/posts/{created}/reactions", data={"emoji": "👍"})
    assert "👍 1" in response.text and 'title="reader"' in response.text
    response = browser(owner).post(f"/posts/{created}/reactions", data={"emoji": "👍"})
    assert "👍 2" in response.text
    response = signed_in.post(f"/posts/{created}/reactions", data={"emoji": "👍"})
    assert "👍 1" in response.text


def test_reaction_rejects_unknown_emoji_and_write_only_user(space) -> None:
    owner, channel = space
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    signed_in = browser(writer)
    created = write(signed_in, channel)
    assert signed_in.post(f"/posts/{created}/reactions", data={"emoji": "👍"}).status_code == 403
    author = browser(owner)
    target = write(author, channel)
    assert author.post(f"/posts/{target}/reactions", data={"emoji": "x"}).status_code == 400


def test_pin_only_manager_and_limit() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner), pin_limit=1)
    member = add_user("member")
    grant(channel, owner, **FULL)
    grant(channel, member, **FULL)
    manager = browser(owner)
    first = write(manager, channel)
    second = write(manager, channel)
    assert browser(member).post(f"/posts/{first}/pin").status_code == 403
    assert manager.post(f"/posts/{first}/pin").status_code == 200
    response = manager.post(f"/posts/{second}/pin")
    assert response.status_code == 409 and "1개" in response.json()["detail"]
    assert manager.post(f"/posts/{first}/pin").status_code == 200
    assert manager.post(f"/posts/{second}/pin").status_code == 200


def test_replies_cannot_be_pinned_and_deleted_post_leaves_pin_list(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    created = write(signed_in, channel, "고정글")
    child = reply(signed_in, created)
    assert signed_in.post(f"/posts/{child}/pin").status_code == 400
    signed_in.post(f"/posts/{created}/pin")
    assert "고정글" in signed_in.get(f"/channels/{channel.id}/pins").text
    signed_in.post(f"/posts/{child}/delete")
    signed_in.post(f"/posts/{created}/delete")
    assert "고정글" not in signed_in.get(f"/channels/{channel.id}/pins").text


def test_pinned_list_hides_others_posts_from_write_only_user(space) -> None:
    owner, channel = space
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    author = browser(owner)
    created = write(author, channel, "비밀고정글")
    author.post(f"/posts/{created}/pin")
    assert "비밀고정글" not in browser(writer).get(f"/channels/{channel.id}/pins").text


def test_preview_is_sanitized_and_empty_post_rejected(space) -> None:
    _, channel = space
    signed_in = browser(space[0])
    response = signed_in.post("/preview", data={"body": "**굵게**<script>x</script>"})
    assert "<strong>굵게</strong>" in response.text and "<script" not in response.text
    assert signed_in.post(f"/channels/{channel.id}/posts", data={"body": "  "}).status_code == 400


def test_non_talk_channels_do_not_accept_posts_yet() -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner), type="vote")
    grant(channel, owner, **FULL)
    response = browser(owner).post(f"/channels/{channel.id}/posts", data={"body": "x"})
    assert response.status_code == 409


def test_timeline_pagination(space) -> None:
    owner, channel = space

    async def seed() -> None:
        async with SessionLocal() as session:
            session.add_all(
                Post(
                    channel_id=channel.id,
                    author_id=owner.id,
                    body_md=f"글{number}끝",
                    created_at=datetime.now(UTC),
                )
                for number in range(55)
            )
            await session.commit()

    run(seed())
    signed_in = browser(owner)
    page = signed_in.get(f"/channels/{channel.id}").text
    assert "글54끝" in page and "글4끝" not in page and "이전 글 더 보기" in page
    older = signed_in.get(f"/channels/{channel.id}/posts?before=6").text
    assert "글4끝" in older and "이전 글 더 보기" not in older


def test_group_manager_adds_and_orders_channels() -> None:
    manager = add_user("manager")
    group = add_group(manager)
    signed_in = browser(manager)
    for name, kind in (("공지", "talk"), ("지출", "approval")):
        response = signed_in.post(f"/groups/{group.id}/channels", data={"name": name, "type": kind})
        assert response.status_code == 200
    bad = signed_in.post(f"/groups/{group.id}/channels", data={"name": "x", "type": "bad"})
    assert bad.status_code == 400
    assert signed_in.post("/channels/2/move", data={"direction": "up"}).status_code == 200
    page = signed_in.get(f"/groups/{group.id}/settings").text
    assert page.index("지출") < page.index("공지")


def test_navigation_hides_channels_without_access() -> None:
    owner = add_user("owner")
    group = add_group(owner)
    channel = add_channel(group)
    add_channel(group, name="비공개채널")
    grant(channel, owner, **FULL)
    assert "비공개채널" not in browser(owner).get(f"/channels/{channel.id}").text


def test_pinned_summary_hides_markdown_and_stays_escaped(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    created = write(signed_in, channel, "## 제목\n\n**굵게** <b>x</b> & 끝")
    signed_in.post(f"/posts/{created}/pin")
    page = signed_in.get(f"/channels/{channel.id}/pins").text
    assert "제목 굵게" in page and "##" not in page and "**" not in page
    assert "<b>" not in page


def test_latest_page_loads_without_cursor_for_tab_catch_up(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    write(signed_in, channel, "놓친글")
    assert "놓친글" in signed_in.get(f"/channels/{channel.id}/posts").text


def test_markdown_cache_keeps_sanitizing_long_and_repeated_text() -> None:
    from app.core.markdown import CACHE_MAX_LENGTH, render_markdown

    payload = "<script>x</script>"
    assert render_markdown(payload) == render_markdown(payload)
    assert "<script" not in render_markdown(payload + "a" * CACHE_MAX_LENGTH)


@pytest.mark.parametrize(
    ("kind", "path"),
    [("approval", "approvals/new"), ("doc", "docs/new"), ("vote", "polls/new")],
)
def test_authoring_pages_have_write_and_preview_tabs(kind: str, path: str) -> None:
    owner = add_user("owner")
    channel = add_channel(add_group(owner), type=kind)
    grant(channel, owner, **FULL)
    page = browser(owner).get(f"/channels/{channel.id}/{path}").text
    assert page.count('data-editor-tab="write"') == 1
    assert page.count('data-editor-tab="preview"') == 1
    assert 'id="editor-body" name="body"' in page

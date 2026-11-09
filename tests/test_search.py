"""F-15: 검색 결과에는 볼 수 있는 글만 나온다."""

import pytest

from app.core.db import SessionLocal
from app.models import Post
from tests.conftest import FULL, add_channel, add_group, add_user, browser, grant, post_id, run


@pytest.fixture
def town():
    owner = add_user("owner")
    group = add_group(owner, "영업")
    open_channel = add_channel(group, "일반")
    secret = add_channel(group, "비밀")
    grant(open_channel, owner, **FULL)
    grant(secret, owner, **FULL)
    return owner, group, open_channel, secret


def write(signed_in, channel, body):
    return post_id(signed_in.post(f"/channels/{channel.id}/posts", data={"body": body}))


def found(signed_in, q, **scope) -> str:
    params = "&".join(f"{key}={value}" for key, value in scope.items())
    return signed_in.get(f"/search?q={q}&{params}").text


def test_reader_finds_only_channels_they_can_read(town) -> None:
    owner, _, open_channel, secret = town
    author = browser(owner)
    write(author, open_channel, "공개 견적서 이야기")
    write(author, secret, "비밀 견적서 이야기")
    reader = add_user("reader")
    grant(open_channel, reader, read="allow")
    page = found(browser(reader), "견적서")
    assert "공개 견적서" in page and "비밀 견적서" not in page
    assert "비밀" not in found(browser(add_user("stranger")), "견적서")


def test_write_only_user_finds_only_own_posts(town) -> None:
    owner, _, open_channel, _ = town
    write(browser(owner), open_channel, "남의 건의 내용")
    writer = add_user("writer")
    grant(open_channel, writer, write="allow")
    mine = browser(writer)
    write(mine, open_channel, "나의 건의 내용")
    page = found(mine, "건의")
    assert "나의 건의" in page and "남의 건의" not in page


def test_replies_need_reply_read(town) -> None:
    owner, _, open_channel, _ = town
    author = browser(owner)
    parent = write(author, open_channel, "원글")
    author.post(f"/posts/{parent}/replies", data={"body": "답글 속 키워드"})
    reader = add_user("reader")
    grant(open_channel, reader, read="allow")
    assert "답글 속 키워드" not in found(browser(reader), "키워드")
    full = add_user("full")
    grant(open_channel, full, read="allow", reply_read="allow")
    page = found(browser(full), "키워드")
    assert "답글 속 키워드" in page and f"/posts/{parent}#post-" in page


def test_deleted_posts_and_others_drafts_are_hidden(town) -> None:
    owner, group, open_channel, _ = town
    author = browser(owner)
    gone = write(author, open_channel, "지운 키워드")
    author.post(f"/posts/{gone}/delete")
    approval = add_channel(group, "결재", "approval")
    grant(approval, owner, **FULL)
    reader = add_user("reader")
    grant(approval, reader, read="allow")
    author.post(f"/channels/{approval.id}/approvals", data={"title": "임시 키워드", "body": "x"})
    assert "지운 키워드" not in found(author, "키워드")
    assert "임시 키워드" in found(author, "키워드")
    assert "임시 키워드" not in found(browser(reader), "키워드")


def test_scope_limits_group_and_channel(town) -> None:
    owner, group, open_channel, secret = town
    other = add_channel(add_group(owner, "다른"), "다른 채널")
    grant(other, owner, **FULL)
    author = browser(owner)
    write(author, open_channel, "범위 일반")
    write(author, secret, "범위 비밀")
    write(author, other, "범위 다른")
    assert "범위 다른" not in found(author, "범위", group_id=group.id)
    page = found(author, "범위", channel_id=open_channel.id)
    assert "범위 일반" in page and "범위 비밀" not in page
    assert "범위 다른" in found(author, "범위")


def test_like_wildcards_are_literal_and_titles_match(town) -> None:
    owner, group, open_channel, _ = town
    author = browser(owner)
    write(author, open_channel, "100% 달성")
    write(author, open_channel, "평범한 글")
    assert "평범한 글" not in found(author, "%25")
    doc = add_channel(group, "규정", "doc")
    grant(doc, owner, **FULL)
    author.post(f"/channels/{doc.id}/docs", data={"title": "출장 규정", "body": "본문"})
    assert "출장 규정" in found(author, "출장")


def test_archived_group_is_not_searched(town) -> None:
    owner, group, open_channel, _ = town
    write(browser(owner), open_channel, "보관된 키워드")

    async def archive() -> None:
        from datetime import UTC, datetime

        from app.models import WorkspaceGroup

        async with SessionLocal() as session:
            (await session.get(WorkspaceGroup, group.id)).archived_at = datetime.now(UTC)
            await session.commit()

    run(archive())
    assert "보관된 키워드" not in found(browser(owner), "키워드")


def test_search_uses_trigram_index(town) -> None:
    async def plan() -> str:
        from sqlalchemy import text

        async with SessionLocal() as session:
            await session.execute(text("SET enable_seqscan = off"))
            rows = await session.execute(
                text("EXPLAIN SELECT id FROM post WHERE body_md ILIKE '%견적서%'")
            )
            return "\n".join(row[0] for row in rows)

    assert "ix_post_body_trgm" in run(plan())
    assert Post.__table__.indexes

"""F-40~43: 문서 채널."""

import io

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import PostRevision
from tests.conftest import FULL, add_channel, add_group, add_user, browser, grant, run


@pytest.fixture
def shelf():
    owner = add_user("owner")
    channel = add_channel(add_group(owner), "규정집", "doc")
    grant(channel, owner, **FULL)
    reader = add_user("reader")
    grant(channel, reader, read="allow", reply_read="allow", reply_write="allow")
    return owner, channel, reader


def create(signed_in, channel, title="휴가 규정", body="연차는 15일", files=None):
    response = signed_in.post(
        f"/channels/{channel.id}/docs", data={"title": title, "body": body}, files=files
    )
    assert response.status_code == 200, response.text
    return int(response.headers["HX-Redirect"].rsplit("/", 1)[1])


def test_document_list_replaces_timeline(shelf) -> None:
    owner, channel, reader = shelf
    post = create(browser(owner), channel)
    page = browser(reader).get(f"/channels/{channel.id}").text
    assert 'class="list doc-list"' in page and f'href="/posts/{post}"' in page
    assert "휴가 규정" in page and "연차는 15일" not in page
    assert "연차는 15일" in browser(reader).get(f"/posts/{post}").text


def test_document_needs_title_and_write(shelf) -> None:
    owner, channel, reader = shelf
    url = f"/channels/{channel.id}/docs"
    assert browser(owner).post(url, data={"title": " ", "body": "x"}).status_code == 400
    assert browser(reader).post(url, data={"title": "x", "body": "x"}).status_code == 403
    assert (
        browser(owner).post(f"/channels/{channel.id}/posts", data={"body": "x"}).status_code == 409
    )


def test_edit_keeps_every_previous_version_with_title(shelf) -> None:
    owner, channel, reader = shelf
    author = browser(owner)
    post = create(author, channel)
    author.post(f"/posts/{post}/edit", data={"title": "휴가 규정", "body": "연차는 16일"})
    author.post(f"/posts/{post}/edit", data={"title": "휴가 규정 개정", "body": "연차는 16일"})

    async def versions() -> list[tuple[int, str, str]]:
        async with SessionLocal() as session:
            rows = await session.scalars(select(PostRevision).order_by(PostRevision.version))
            return [(row.version, row.title, row.body_md) for row in rows]

    assert run(versions()) == [(1, "휴가 규정", "연차는 15일"), (2, "휴가 규정", "연차는 16일")]
    page = browser(reader).get(f"/posts/{post}/revisions").text
    assert "현재 버전은 3" in page and "연차는 15일" in page and "버전 2" in page
    assert "휴가 규정 개정" in browser(reader).get(f"/channels/{channel.id}").text


def test_revisions_are_read_only_and_document_only(shelf) -> None:
    owner, channel, _ = shelf
    talk = add_channel(add_group(owner, "소통"), "일반")
    grant(talk, owner, **FULL)
    signed_in = browser(owner)
    created = signed_in.post(f"/channels/{talk.id}/posts", data={"body": "글"})
    post = int(created.text.split('id="post-')[1].split('"')[0])
    assert signed_in.get(f"/posts/{post}/revisions").status_code == 404
    document = create(signed_in, channel)
    assert signed_in.post(f"/posts/{document}/revisions").status_code == 405


def test_documents_cannot_be_deleted_by_anyone(shelf) -> None:
    owner, channel, _ = shelf
    author = browser(owner)
    post = create(author, channel, files=[("files", ("a.txt", io.BytesIO(b"x")))])
    assert author.post(f"/posts/{post}/delete").status_code == 403
    assert author.post("/attachments/1/delete").status_code == 403
    page = author.get(f"/posts/{post}").text
    assert f'hx-post="/posts/{post}/delete"' not in page
    assert 'hx-post="/attachments/1/delete"' not in page


def test_document_replies_follow_normal_rules(shelf) -> None:
    owner, channel, reader = shelf
    post = create(browser(owner), channel)
    signed_in = browser(reader)
    reply = signed_in.post(f"/posts/{post}/replies", data={"body": "질문"})
    reply_id = int(reply.text.split('id="post-')[1].split('"')[0])
    assert signed_in.post(f"/posts/{reply_id}/delete").status_code == 200


def test_reader_without_read_sees_only_own_documents(shelf) -> None:
    owner, channel, _ = shelf
    writer = add_user("writer")
    grant(channel, writer, write="allow")
    create(browser(owner), channel, title="남의 문서")
    mine = browser(writer)
    create(mine, channel, title="내 문서")
    page = mine.get(f"/channels/{channel.id}").text
    assert "내 문서" in page and "남의 문서" not in page


def test_document_mentions_create_acks(shelf) -> None:
    owner, channel, reader = shelf
    from app.services.delivery import todo_count

    create(browser(owner), channel, body="@reader 확인")
    assert run(todo_count(reader.id)) == 1

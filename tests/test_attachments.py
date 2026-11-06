import io
import urllib.request

import pytest

from app.core.db import SessionLocal
from app.models import Attachment
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

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def space():
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    return owner, channel


def upload(signed_in, url: str, *files: tuple[str, bytes], body="첨부"):
    return signed_in.post(
        url,
        data={"body": body},
        files=[("files", (name, io.BytesIO(content))) for name, content in files],
    )


def test_upload_and_signed_download_round_trip(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    response = upload(signed_in, f"/channels/{channel.id}/posts", ("견적서.txt", "내용".encode()))
    assert response.status_code == 200 and "견적서.txt" in response.text
    redirect = signed_in.get("/attachments/1?download=true")
    assert redirect.status_code == 303
    assert redirect.headers["cache-control"] == "no-store"
    with urllib.request.urlopen(redirect.headers["location"]) as file:
        assert file.read() == "내용".encode()
        assert "attachment" in file.headers["Content-Disposition"]


def test_image_preview_is_inline_and_mime_comes_from_extension(space) -> None:
    owner, channel = space
    signed_in = browser(owner)
    response = upload(signed_in, f"/channels/{channel.id}/posts", ("a.png", PNG))
    assert '<img src="/attachments/1"' in response.text
    with urllib.request.urlopen(signed_in.get("/attachments/1").headers["location"]) as file:
        assert file.headers["Content-Type"] == "image/png"
        assert "inline" in file.headers["Content-Disposition"]


@pytest.mark.parametrize("name", ["run.exe", "page.html", "image.svg", "noextension"])
def test_disallowed_types_are_rejected(space, name: str) -> None:
    owner, channel = space
    signed_in = browser(owner)
    response = upload(signed_in, f"/channels/{channel.id}/posts", (name, b"x"))
    assert response.status_code == 400

    async def count() -> int:
        from sqlalchemy import func, select

        async with SessionLocal() as session:
            return await session.scalar(select(func.count()).select_from(Attachment))

    assert run(count()) == 0


def test_file_count_and_size_limits(space, monkeypatch) -> None:
    from app.core.config import get_settings

    owner, channel = space
    signed_in = browser(owner)
    url = f"/channels/{channel.id}/posts"
    assert upload(signed_in, url, *[(f"{i}.txt", b"x") for i in range(11)]).status_code == 400
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    response = upload(signed_in, url, ("big.txt", b"x" * (1024 * 1024 + 1)))
    assert response.status_code == 400 and "1MB" in response.json()["detail"]


def test_download_requires_view_permission(space) -> None:
    owner, channel = space
    author = browser(owner)
    upload(author, f"/channels/{channel.id}/posts", ("a.txt", b"x"))
    assert browser(add_user("stranger")).get("/attachments/1").status_code == 404

    writer = add_user("writer")
    grant(channel, writer, write="allow")
    assert browser(writer).get("/attachments/1").status_code == 404

    reader = add_user("reader")
    grant(channel, reader, read="allow")
    assert browser(reader).get("/attachments/1").status_code == 303


def test_reply_attachment_requires_reply_read(space) -> None:
    owner, channel = space
    author = browser(owner)
    parent = post_id(author.post(f"/channels/{channel.id}/posts", data={"body": "글"}))
    upload(author, f"/posts/{parent}/replies", ("a.txt", b"x"))
    reader = add_user("reader")
    grant(channel, reader, read="allow")
    assert browser(reader).get("/attachments/1").status_code == 404


def test_deleted_post_attachment_is_not_downloadable(space) -> None:
    owner, channel = space
    author = browser(owner)
    created = post_id(upload(author, f"/channels/{channel.id}/posts", ("a.txt", b"x")))
    author.post(f"/posts/{created}/delete")
    assert author.get("/attachments/1").status_code == 404

"""F-20~26: 멘션과 필수 읽음 확인, 내 할 일 개수."""

import asyncio
import json

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.models import AuditLog, Channel, MentionAck, PermissionGrant, Post, User
from app.routers.events import count_stream
from app.services.delivery import todo_count
from app.services.mentions import find_mentions
from app.services.permissions import Authorizer, access_for_users
from app.services.realtime import Hub, hub
from tests.conftest import (
    FULL,
    add_channel,
    add_department,
    add_group,
    add_user,
    browser,
    grant,
    join,
    post_id,
    run,
)


@pytest.fixture
def space():
    owner = add_user("owner")
    channel = add_channel(add_group(owner))
    grant(channel, owner, **FULL)
    return owner, channel


def reader_of(channel, name: str, **values: str) -> User:
    user = add_user(name)
    grant(channel, user, **(values or {"read": "allow"}))
    return user


def write(signed_in, channel, body: str):
    return signed_in.post(f"/channels/{channel.id}/posts", data={"body": body})


def acks(post: int) -> dict[int, MentionAck]:
    async def load() -> dict[int, MentionAck]:
        async with SessionLocal() as session:
            rows = await session.scalars(select(MentionAck).where(MentionAck.post_id == post))
            return {row.user_id: row for row in rows}

    return run(load())


def audit_actions() -> list[str]:
    async def load() -> list[str]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(AuditLog.action).order_by(AuditLog.id)))

    return run(load())


USERS = [(1, "김민재"), (2, "김민"), (3, "남궁 민수")]
DEPARTMENTS = [(1, "영업 1팀")]


@pytest.mark.parametrize(
    ("body", "users", "departments"),
    [
        ("@김민재 확인 바랍니다", {1}, set()),
        ("@김민재님 확인", {1}, set()),
        ("@김민 확인", {2}, set()),
        ("@남궁 민수 확인", {3}, set()),
        ("(@김민재) 그리고 @영업 1팀", {1}, {1}),
        ("메일은 a@김민재 입니다", set(), set()),
        ("없는 사람 @이순신", set(), set()),
        ("`@김민재` 코드", set(), set()),
        ("```\n@김민재\n```", set(), set()),
    ],
)
def test_find_mentions(body: str, users: set[int], departments: set[int]) -> None:
    found = find_mentions(body, USERS, DEPARTMENTS)
    assert (found.users, found.departments, found.ambiguous) == (users, departments, set())


def test_same_name_needs_explicit_id() -> None:
    same = [(1, "김민재"), (2, "김민재")]
    assert find_mentions("@김민재", same, []).ambiguous == {"김민재"}
    found = find_mentions("@김민재#2", same, [])
    assert found.users == {2} and not found.ambiguous


def test_same_name_is_rejected_when_writing(space) -> None:
    owner, channel = space
    reader_of(channel, "dup-1")
    run(_rename("dup-1", "동명"))
    reader_of(channel, "dup-2")
    run(_rename("dup-2", "동명"))
    response = write(browser(owner), channel, "@동명 확인")
    assert response.status_code == 400 and "동명" in response.json()["detail"]


async def _rename(old: str, new: str) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.name == old))
        user.name = new
        await session.commit()


def test_mention_shows_button_only_to_target_and_counts(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    other = reader_of(channel, "other")
    response = write(browser(owner), channel, "@target 공지 확인")
    assert "0/1 확인" in response.text and "미확인: target" in response.text
    assert "읽음 확인</button>" not in response.text

    assert "읽음 확인</button>" in browser(target).get(f"/channels/{channel.id}").text
    assert "읽음 확인</button>" not in browser(other).get(f"/channels/{channel.id}").text


def test_ack_records_time_once_and_is_audited(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    post = post_id(write(browser(owner), channel, "@target 공지"))
    signed_in = browser(target)

    response = signed_in.post(f"/posts/{post}/ack")
    assert response.status_code == 200
    assert "1/1 확인" in response.text and "읽음 확인</button>" not in response.text
    first = acks(post)[target.id].acked_at
    assert first is not None

    assert signed_in.post(f"/posts/{post}/ack").status_code == 200
    assert acks(post)[target.id].acked_at == first
    assert audit_actions().count("mention.ack") == 1


def test_only_target_can_ack(space) -> None:
    owner, channel = space
    reader_of(channel, "target")
    other = reader_of(channel, "other")
    post = post_id(write(browser(owner), channel, "@target 공지"))
    assert browser(other).post(f"/posts/{post}/ack").status_code == 403
    assert browser(add_user("stranger")).post(f"/posts/{post}/ack").status_code == 404


def test_self_mention_needs_no_ack(space) -> None:
    owner, channel = space
    post = post_id(write(browser(owner), channel, "@owner 메모"))
    assert acks(post) == {}


def test_department_mention_expands_children_and_skips_people_without_read(space) -> None:
    owner, channel = space
    parent = add_department("본부")
    child = add_department("개발팀", parent)
    lead, member, outsider = add_user("lead"), add_user("member"), add_user("outsider")
    join(lead, parent)
    join(member, child)
    join(outsider, child)
    grant(channel, lead, read="allow")
    grant(channel, member, read="allow")

    post = post_id(write(browser(owner), channel, "@본부 전달"))
    rows = acks(post)
    assert set(rows) == {lead.id, member.id}
    assert rows[member.id].source_department_id == parent.id


def test_direct_mention_beats_department_source(space) -> None:
    owner, channel = space
    team = add_department("개발팀")
    member = reader_of(channel, "member")
    join(member, team)
    post = post_id(write(browser(owner), channel, "@개발팀 @member 확인"))
    assert acks(post)[member.id].source_department_id is None


def test_mentioning_user_without_read_is_blocked(space) -> None:
    owner, channel = space
    add_user("outsider")
    response = write(browser(owner), channel, "@outsider 확인")
    assert response.status_code == 400 and "outsider" in response.json()["detail"]

    async def count() -> int:
        async with SessionLocal() as session:
            return len(list(await session.scalars(select(Post))))

    assert run(count()) == 0


def test_mentioning_department_nobody_can_read_is_blocked(space) -> None:
    owner, channel = space
    team = add_department("외부팀")
    join(add_user("outsider"), team)
    assert write(browser(owner), channel, "@외부팀 확인").status_code == 400


def test_reply_mention_requires_reply_read(space) -> None:
    owner, channel = space
    reader_of(channel, "reader")
    full = reader_of(channel, "full", **FULL)
    author = browser(owner)
    parent = post_id(write(author, channel, "글"))
    url = f"/posts/{parent}/replies"
    assert author.post(url, data={"body": "@reader 확인"}).status_code == 400
    reply = post_id(author.post(url, data={"body": "@full 확인"}))
    assert set(acks(reply)) == {full.id}


def test_write_only_user_cannot_be_mentioned_on_others_post(space) -> None:
    owner, channel = space
    reader_of(channel, "writer", write="allow")
    assert write(browser(owner), channel, "@writer 확인").status_code == 400


def test_edit_updates_targets_but_keeps_acked_rows(space) -> None:
    owner, channel = space
    acked = reader_of(channel, "acked")
    waiting = reader_of(channel, "waiting")
    added = reader_of(channel, "added")
    author = browser(owner)
    post = post_id(write(author, channel, "@acked @waiting 확인"))
    browser(acked).post(f"/posts/{post}/ack")

    author.post(f"/posts/{post}/edit", data={"body": "@added 확인"})
    rows = acks(post)
    assert set(rows) == {acked.id, added.id}
    assert waiting.id not in rows and rows[acked.id].acked_at is not None


def test_edit_keeps_ack_unless_reconfirm_requested(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    author = browser(owner)
    post = post_id(write(author, channel, "@target 공지"))
    browser(target).post(f"/posts/{post}/ack")

    author.post(f"/posts/{post}/edit", data={"body": "@target 공지 수정"})
    assert acks(post)[target.id].acked_at is not None
    assert "mention.reconfirm" not in audit_actions()

    author.post(f"/posts/{post}/edit", data={"body": "@target 공지 재수정", "reconfirm": "true"})
    assert acks(post)[target.id].acked_at is None
    assert "mention.reconfirm" in audit_actions()


def test_reconfirm_option_is_offered_only_when_post_has_targets(space) -> None:
    owner, channel = space
    reader_of(channel, "target")
    author = browser(owner)
    plain = post_id(write(author, channel, "일반 글"))
    mentioned = post_id(write(author, channel, "@target 공지"))
    assert "reconfirm" not in author.get(f"/posts/{plain}/edit").text
    assert "reconfirm" in author.get(f"/posts/{mentioned}/edit").text


def test_todo_lists_until_acked(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    post = post_id(write(browser(owner), channel, "@target 꼭 읽기"))
    signed_in = browser(target)
    assert "꼭 읽기" in signed_in.get("/todo").text
    assert run(todo_count(target.id)) == 1

    signed_in.post(f"/posts/{post}/ack")
    assert "꼭 읽기" not in signed_in.get("/todo").text
    assert run(todo_count(target.id)) == 0


def test_deleted_post_leaves_todo(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    author = browser(owner)
    post = post_id(write(author, channel, "@target 공지"))
    author.post(f"/posts/{post}/delete")
    assert run(todo_count(target.id)) == 0
    assert browser(target).post(f"/posts/{post}/ack").status_code == 409


def test_losing_read_hides_post_from_todo(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    write(browser(owner), channel, "@target 비밀 공지")

    async def revoke() -> None:
        async with SessionLocal() as session:
            await session.execute(
                delete(PermissionGrant).where(PermissionGrant.subject_id == target.id)
            )
            await session.commit()

    run(revoke())
    assert run(todo_count(target.id)) == 0
    assert "비밀 공지" not in browser(target).get("/todo").text


def test_candidates_mark_people_without_read(space) -> None:
    owner, channel = space
    reader_of(channel, "reader-a")
    add_user("reader-b")
    add_department("reader-team")
    rows = browser(owner).get(f"/channels/{channel.id}/mention-candidates?q=reader").json()
    assert {row["label"]: row["disabled"] for row in rows} == {
        "reader-a": False,
        "reader-b": True,
        "reader-team": False,
    }


def test_candidates_insert_id_for_same_names(space) -> None:
    owner, channel = space
    first = reader_of(channel, "dup-1")
    run(_rename("dup-1", "동명"))
    second = reader_of(channel, "dup-2")
    run(_rename("dup-2", "동명"))
    rows = browser(owner).get(f"/channels/{channel.id}/mention-candidates?q=동명").json()
    assert {row["insert"] for row in rows} == {f"동명#{first.id}", f"동명#{second.id}"}


def test_candidates_treat_like_wildcards_as_text(space) -> None:
    owner, channel = space
    reader_of(channel, "reader")
    assert browser(owner).get(f"/channels/{channel.id}/mention-candidates?q=%25").json() == []


def test_candidates_require_channel_access(space) -> None:
    _, channel = space
    stranger = browser(add_user("stranger"))
    assert stranger.get(f"/channels/{channel.id}/mention-candidates").status_code == 404


def test_batch_access_matches_single_decision(space) -> None:
    owner, channel = space
    team = add_department("팀")
    child = add_department("하위", team)
    grant(channel, team, read="allow", write="allow")
    denied, member, stranger = add_user("denied"), add_user("member"), add_user("stranger")
    join(denied, child)
    join(member, child)
    grant(channel, denied, write="deny")

    async def compare() -> None:
        async with SessionLocal() as session:
            loaded = await session.get(Channel, channel.id)
            users = [owner, denied, member, stranger]
            batch = await access_for_users(session, loaded, [user.id for user in users])
            for user in users:
                single = await Authorizer(session, await session.get(User, user.id)).access(loaded)
                assert batch[user.id] == single

    run(compare())


def test_count_stream_sends_on_connect_and_on_change(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    write(browser(owner), channel, "@target 첫 글")

    async def scenario() -> None:
        stream = count_stream(target.id)
        assert await anext(stream) == "event: counts\ndata: 1\n\n"
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        async with SessionLocal() as session:
            await session.execute(delete(MentionAck))
            await session.commit()
        hub.dispatch_counts([target.id])
        assert await asyncio.wait_for(waiting, 5) == "event: counts\ndata: 0\n\n"
        await stream.aclose()

    run(scenario())
    assert not hub._user_subscribers


def test_count_stream_carries_only_a_number(space) -> None:
    owner, channel = space
    target = reader_of(channel, "target")
    write(browser(owner), channel, "@target 비밀내용")

    async def first() -> str:
        stream = count_stream(target.id)
        message = await anext(stream)
        await stream.aclose()
        return message

    assert "비밀내용" not in run(first())


def test_count_stream_ends_for_deactivated_user(space) -> None:
    _, channel = space
    target = reader_of(channel, "target")

    async def scenario() -> None:
        async with SessionLocal() as session:
            (await session.get(User, target.id)).status = "inactive"
            await session.commit()
        with pytest.raises(StopAsyncIteration):
            await anext(count_stream(target.id))

    run(scenario())


def test_notify_payload_with_user_ids_reaches_only_those_users() -> None:
    async def scenario() -> None:
        local = Hub()
        mine, other = local.subscribe_user(1), local.subscribe_user(2)
        local._on_notify(None, 0, "", json.dumps({"user_ids": [1, 1]}))
        assert mine.qsize() == 1 and other.empty()

    run(scenario())

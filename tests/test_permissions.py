"""관문 1: 권한 판정. 기획서 판정 예시와 규칙을 그대로 옮겼다."""

import pytest

from app.core.db import SessionLocal
from app.models import Channel, User
from app.services.permissions import Authorizer, ChannelAccess, decide_access, normalize_grant
from tests.conftest import (
    add_channel,
    add_department,
    add_group,
    add_user,
    grant,
    join,
    run,
)

ALLOW_WRITE = {"write": "allow"}
DENY_WRITE = {"write": "deny"}


def decide(user=(), chains=(), approver=False) -> ChannelAccess:
    return decide_access(is_approver=approver, user_layers=user, department_chains=chains)


@pytest.mark.parametrize(
    ("case", "user", "chains", "expected"),
    [
        ("영업팀원의 일반 쓰기", [{}], [[ALLOW_WRITE]], True),
        ("영업팀 인턴의 쓰기", [DENY_WRITE], [[ALLOW_WRITE]], False),
        ("아무 설정이 없는 사용자", [{}], [[{}]], False),
    ],
)
def test_spec_write_examples(case, user, chains, expected) -> None:
    assert decide(user, chains).write is expected, case


def test_spec_cross_team_reader_is_allowed_by_user_setting() -> None:
    assert decide([{"read": "allow"}], [[{}]]).read is True


def test_spec_write_only_member_cannot_read() -> None:
    access = decide([{}], [[ALLOW_WRITE]])
    assert (access.read, access.write, access.can_enter) == (False, True, True)


def test_spec_denied_approver_gets_all_permissions() -> None:
    assert decide([DENY_WRITE], [[{}]], approver=True) == ChannelAccess(True, True, True, True)


def test_no_setting_anywhere_is_denied() -> None:
    assert decide() == ChannelAccess()


def test_channel_setting_beats_group_for_same_subject() -> None:
    assert decide([{"read": "deny"}, {"read": "allow"}]).read is False
    assert decide([{"read": "allow"}, {"read": "deny"}]).read is True
    assert decide([{}, {"read": "allow"}]).read is True


def test_user_group_setting_beats_department_channel_setting() -> None:
    assert decide([{}, {"read": "deny"}], [[{"read": "allow"}]]).read is False


def test_department_inherits_from_nearest_ancestor_with_value() -> None:
    assert decide([], [[{}, {}, {"read": "allow"}, {}]]).read is True
    assert decide([], [[{"read": "deny"}, {}, {"read": "allow"}]]).read is False


def test_concurrent_departments_any_allow_wins() -> None:
    assert decide([], [[{"read": "deny"}], [{"read": "allow"}]]).read is True
    assert decide([], [[{"read": "deny"}], [{}]]).read is False


def test_user_deny_beats_concurrent_department_allow() -> None:
    assert decide([{"read": "deny"}], [[{"read": "allow"}], [{"read": "allow"}]]).read is False


def test_permissions_are_decided_independently() -> None:
    access = decide([{"read": "deny"}], [[{"read": "allow", "write": "allow"}]])
    assert (access.read, access.write) == (False, True)


def test_reply_read_requires_read_or_write() -> None:
    assert decide([{"reply_read": "allow"}]).reply_read is False
    assert decide([{"reply_read": "allow", "read": "allow"}]).reply_read is True
    assert decide([{"reply_read": "allow", "write": "allow"}]).reply_read is True


def test_reply_write_requires_reply_read() -> None:
    assert decide([{"reply_write": "allow", "read": "allow"}]).reply_write is False


def test_normalize_turns_on_reply_read_with_reply_write() -> None:
    assert normalize_grant({"reply_write": "allow"})["reply_read"] == "allow"


def test_normalize_turns_off_replies_when_read_and_write_are_off() -> None:
    result = normalize_grant({"read": "deny", "write": "deny", "reply_write": "allow"})
    assert (result["reply_read"], result["reply_write"]) == ("deny", "deny")


def test_normalize_denied_reply_read_denies_reply_write() -> None:
    assert normalize_grant({"reply_read": "deny", "reply_write": None})["reply_write"] == "deny"


def access_of(user: User, channel: Channel) -> ChannelAccess:
    async def load() -> ChannelAccess:
        async with SessionLocal() as session:
            loaded = await session.get(User, user.id)
            target = await session.get(Channel, channel.id)
            return await Authorizer(session, loaded).access(target)

    return run(load())


def test_database_resolution_walks_department_tree_and_group_scope() -> None:
    owner = add_user("owner")
    group = add_group(owner)
    channel = add_channel(group)
    head = add_department("본부")
    team = add_department("팀", head)
    member = add_user("member")
    join(member, team)
    assert access_of(member, channel) == ChannelAccess()

    grant(group, head, read="allow")
    assert access_of(member, channel).read is True

    grant(channel, team, read="deny")
    assert access_of(member, channel).read is False

    grant(group, member, read="allow")
    assert access_of(member, channel).read is True


def test_system_admin_gets_no_implicit_read() -> None:
    admin = add_user("admin")
    channel = add_channel(add_group(admin))
    assert access_of(admin, channel) == ChannelAccess()


def test_archived_group_closes_channels() -> None:
    from datetime import UTC, datetime

    from app.models import WorkspaceGroup

    owner = add_user("owner")
    group = add_group(owner)
    channel = add_channel(group)
    grant(channel, owner, read="allow", write="allow")

    async def archive() -> None:
        async with SessionLocal() as session:
            (await session.get(WorkspaceGroup, group.id)).archived_at = datetime.now(UTC)
            await session.commit()

    run(archive())
    assert access_of(owner, channel) == ChannelAccess()


def test_department_cycle_does_not_hang() -> None:
    from app.services.permissions import Authorizer as Auth

    assert Auth._ancestors(1, {1: 2, 2: 1}) == [1, 2]

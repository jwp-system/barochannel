"""@멘션과 필수 읽음 확인. mention_ack의 acked_at이 빈 행이 곧 미확인 목록이다."""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Department, MentionAck, Post, User, UserDepartment
from app.services.permissions import Authorizer, ChannelAccess, access_for_users
from app.services.post_views import can_view_any

MENTION_START = re.compile(r"(?<![\w.@])@")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
EXPLICIT_ID = re.compile(r"#(\d+)")


class MentionError(ValueError):
    """작성자에게 그대로 보여줄 수 있는 문구를 담는다."""


@dataclass
class FoundMentions:
    users: set[int] = field(default_factory=set)
    departments: set[int] = field(default_factory=set)
    ambiguous: set[str] = field(default_factory=set)


def find_mentions(
    body: str, users: Iterable[tuple[int, str]], departments: Iterable[tuple[int, str]]
) -> FoundMentions:
    """이름에 공백이 있고 뒤에 조사가 붙을 수 있어 긴 이름부터 맞춰 본다.

    동명이인은 자동완성이 넣는 @이름#번호로 구분한다. 맞는 이름이 없으면 평문으로 둔다.
    """
    targets: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for user_id, name in users:
        targets[name].append(("user", user_id))
    for department_id, name in departments:
        targets[name].append(("department", department_id))
    names = sorted(targets, key=len, reverse=True)

    found = FoundMentions()
    text = CODE.sub(" ", body)
    for match in MENTION_START.finditer(text):
        name = next((item for item in names if text.startswith(item, match.end())), None)
        if name is None:
            continue
        candidates = targets[name]
        explicit = EXPLICIT_ID.match(text, match.end() + len(name))
        if explicit:
            candidates = [item for item in candidates if item[1] == int(explicit.group(1))]
        if len(candidates) > 1:
            found.ambiguous.add(name)
        for kind, target_id in candidates[:1] if len(candidates) == 1 else []:
            (found.users if kind == "user" else found.departments).add(target_id)
    return found


async def _department_members(
    session: AsyncSession, department_ids: set[int]
) -> dict[int, set[int]]:
    """소속원은 하위 소속까지 펼친다. 본부를 멘션하면 산하 팀이 모두 대상이다."""
    if not department_ids:
        return {}
    parents = dict((await session.execute(select(Department.id, Department.parent_id))).all())
    rows = await session.execute(
        select(UserDepartment.user_id, UserDepartment.department_id)
        .join(User, User.id == UserDepartment.user_id)
        .where(User.status == "active")
    )
    members: dict[int, set[int]] = {department_id: set() for department_id in department_ids}
    for user_id, department_id in rows:
        current: int | None = department_id
        seen: set[int] = set()
        while current is not None and current not in seen:
            seen.add(current)
            if current in members:
                members[current].add(user_id)
            current = parents.get(current)
    return members


async def sync_mentions(
    session: AsyncSession, post: Post, parent: Post | None, channel: Channel
) -> set[int]:
    """본문에 맞춰 대상자를 고치고 미확인 개수가 바뀐 사람을 돌려준다.

    이미 확인한 행은 멘션을 지워도 기록으로 남긴다.
    """
    users = (await session.execute(select(User.id, User.name).where(User.status == "active"))).all()
    departments = (await session.execute(select(Department.id, Department.name))).all()
    found = find_mentions(post.body_md, users, departments)
    if found.ambiguous:
        names = ", ".join(sorted(found.ambiguous))
        raise MentionError(f"같은 이름이 있습니다. 자동완성에서 골라 주세요: {names}")

    members = await _department_members(session, found.departments)
    everyone = found.users | {user_id for group in members.values() for user_id in group}
    access = await access_for_users(session, channel, everyone)

    def can_view(user_id: int) -> bool:
        return can_view_any(access[user_id], post, parent, user_id)

    names_by_id = dict(users)
    blocked = sorted(names_by_id[user_id] for user_id in found.users if not can_view(user_id))
    if blocked:
        raise MentionError(f"글을 볼 권한이 없어 멘션할 수 없습니다: {', '.join(blocked)}")

    targets: dict[int, int | None] = {}
    department_names = dict(departments)
    for department_id, group in members.items():
        allowed = {user_id for user_id in group if can_view(user_id)}
        if not allowed:
            name = department_names[department_id]
            raise MentionError(f"글을 볼 수 있는 소속원이 없어 멘션할 수 없습니다: {name}")
        targets.update(dict.fromkeys(allowed, department_id))
    targets.update(dict.fromkeys(found.users))
    targets.pop(post.author_id, None)

    rows = {
        row.user_id: row
        for row in await session.scalars(select(MentionAck).where(MentionAck.post_id == post.id))
    }
    removed = {
        user_id for user_id, row in rows.items() if user_id not in targets and row.acked_at is None
    }
    if removed:
        await session.execute(
            delete(MentionAck).where(MentionAck.post_id == post.id, MentionAck.user_id.in_(removed))
        )
    added = set(targets) - set(rows)
    session.add_all(
        MentionAck(post_id=post.id, user_id=user_id, source_department_id=targets[user_id])
        for user_id in added
    )
    return removed | added


@dataclass(frozen=True)
class PendingAck:
    post: Post
    parent: Post | None
    channel: Channel
    access: ChannelAccess


async def pending_acks(session: AsyncSession, authz: Authorizer) -> list[PendingAck]:
    """지금 볼 수 있는 글만 센다. 권한을 잃은 글이나 지워진 글은 할 일로 남기지 않는다."""
    posts = await session.scalars(
        select(Post)
        .join(MentionAck, MentionAck.post_id == Post.id)
        .where(
            MentionAck.user_id == authz.user.id,
            MentionAck.acked_at.is_(None),
            Post.deleted_at.is_(None),
        )
        .order_by(Post.id.desc())
    )
    pending = []
    for post in posts:
        channel = await session.get(Channel, post.channel_id)
        parent = await session.get(Post, post.parent_post_id) if post.parent_post_id else None
        access = await authz.access(channel)
        if can_view_any(access, post, parent, authz.user.id):
            pending.append(PendingAck(post, parent, channel, access))
    return pending

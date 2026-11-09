"""SSE로 보낼 내용을 수신자별로 만든다. 이벤트마다 권한을 새로 판정한다."""

from dataclasses import dataclass

from app.core.db import SessionLocal
from app.core.templating import render_text
from app.models import Channel, Post, User
from app.services.approvals import (
    my_references,
    my_reviews,
    my_turn,
    stalled_in_groups,
    unread_notices,
)
from app.services.dispatch import receiving_for
from app.services.mentions import pending_acks
from app.services.navigation import build_navigation, unread_payload
from app.services.permissions import Authorizer, can_view_replies
from app.services.post_views import build_views, can_view_any, pinned_views
from app.services.realtime import Event

Message = tuple[str, str]


async def still_allowed(user_id: int, channel_id: int) -> bool:
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        channel = await session.get(Channel, channel_id)
        if user is None or not user.is_active or channel is None:
            return False
        return (await Authorizer(session, user).access(channel)).can_enter


async def todo_count(user_id: int) -> int | None:
    """None이면 수신 자격을 잃은 것이므로 연결을 끊는다."""
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return None
        authz = Authorizer(session, user)
        acks = await pending_acks(session, authz)
        stalled = await stalled_in_groups(session, await authz.managed_group_ids())
        receiving = await receiving_for(session, authz)
        turns = await my_turn(session, user_id)
        reviews = await my_reviews(session, user_id)
        notices = await unread_notices(session, user_id)
        references = await my_references(session, user_id)
        waiting = len(turns) + len(reviews) + len(notices) + len(stalled) + len(receiving)
        waiting += len(references)
        return len(acks) + waiting


@dataclass(frozen=True)
class CountState:
    """개수 연결이 보내는 값. todo는 내 할 일 개수, unread는 채널·그룹별 새 글 수다."""

    todo: int
    unread: dict[str, dict[str, int]]
    watched: frozenset[int]


async def count_state(user_id: int) -> CountState | None:
    """None이면 수신 자격을 잃은 것이므로 연결을 끊는다."""
    todo = await todo_count(user_id)
    if todo is None:
        return None
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return None
        navigation = await build_navigation(Authorizer(session, user))
    watched = frozenset(channel.id for item in navigation for channel in item.channels)
    return CountState(todo, unread_payload(navigation), watched)


async def messages_for(event: Event, user_id: int) -> list[Message] | None:
    """None이면 수신 자격을 잃은 것이므로 연결을 끊는다."""
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        channel = await session.get(Channel, event.channel_id)
        if user is None or not user.is_active or channel is None:
            return None
        authz = Authorizer(session, user)
        access = await authz.access(channel)
        if not access.can_enter:
            return None

        if event.kind == "pins":
            views = await pinned_views(session, authz, channel, access)
            return [(f"pins-{channel.id}", render_text("partials/pinned.html", views=views))]

        post = await session.get(Post, event.post_id)
        if post is None or post.channel_id != channel.id:
            return []
        parent = await session.get(Post, post.parent_post_id) if post.parent_post_id else None
        if not can_view_any(access, post, parent, user_id):
            return []

        (view,) = await build_views(session, [post], authz, channel, access, parent)
        card = render_text("partials/post_card.html", v=view)
        messages: list[Message] = []
        if event.kind == "updated":
            messages.append((f"post-{post.id}", card))
            if parent is None and can_view_replies(access, post, user_id):
                root = render_text("partials/thread_root.html", root=view)
                messages.append((f"root-{post.id}", root))
        else:
            # 작성자의 다른 탭도 받아야 하므로 보낸다. 같은 글이 겹치면 브라우저가 버린다.
            target = f"thread-{parent.id}" if parent else f"ch-{channel.id}"
            messages.append((target, card))
        if parent is not None:
            (parent_view,) = await build_views(session, [parent], authz, channel, access)
            messages.append(
                (f"post-{parent.id}", render_text("partials/post_card.html", v=parent_view))
            )
        return messages

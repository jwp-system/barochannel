"""글 카드에 필요한 값을 한곳에서 만든다. 일반 응답과 SSE가 같은 판정을 쓴다."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.markdown import plain_summary, render_markdown
from app.models import Attachment, Channel, MentionAck, Post, Reaction, User
from app.services.approvals import ApprovalView, approval_views
from app.services.permissions import (
    Authorizer,
    ChannelAccess,
    can_view_post,
    can_view_replies,
    can_write_reply,
)
from app.services.polls import PollView, poll_views
from app.services.work import WorkView, work_views

EMOJIS = ("👍", "❤️", "😄", "🎉", "👀", "✅")
PAGE_SIZE = 50


@dataclass
class ReactionView:
    emoji: str
    names: list[str] = field(default_factory=list)
    mine: bool = False


@dataclass
class PostView:
    post: Post
    author_name: str
    body_html: str | None
    attachments: list[Attachment]
    reactions: list[ReactionView]
    reply_count: int
    can_edit: bool
    can_delete: bool
    can_react: bool
    can_open_thread: bool
    can_pin: bool
    can_delete_files: bool = False
    ack_total: int = 0
    ack_done: int = 0
    pending_names: list[str] = field(default_factory=list)
    can_ack: bool = False
    approval: ApprovalView | None = None
    poll: PollView | None = None
    work: WorkView | None = None

    @property
    def is_reply(self) -> bool:
        return self.post.parent_post_id is not None

    @property
    def is_deleted(self) -> bool:
        return self.post.deleted_at is not None


@dataclass(frozen=True)
class PinnedItem:
    post_id: int
    author_name: str
    summary: str


def can_view_any(access: ChannelAccess, post: Post, parent: Post | None, user_id: int) -> bool:
    if parent is None:
        return can_view_post(access, post, user_id)
    return can_view_replies(access, parent, user_id)


async def build_views(
    session: AsyncSession,
    posts: Sequence[Post],
    authz: Authorizer,
    channel: Channel,
    access: ChannelAccess,
    parent: Post | None = None,
) -> list[PostView]:
    """parent를 주면 posts는 그 글의 답글이다."""
    if not posts:
        return []
    ids = [post.id for post in posts]
    user = authz.user
    authors = dict(
        (
            await session.execute(
                select(User.id, User.name).where(User.id.in_({post.author_id for post in posts}))
            )
        ).all()
    )
    attachments: dict[int, list[Attachment]] = defaultdict(list)
    for attachment in await session.scalars(
        select(Attachment)
        .where(Attachment.post_id.in_(ids), Attachment.deleted_at.is_(None))
        .order_by(Attachment.id)
    ):
        attachments[attachment.post_id].append(attachment)

    reactions: dict[int, dict[str, ReactionView]] = defaultdict(dict)
    rows = await session.execute(
        select(Reaction.post_id, Reaction.emoji, Reaction.user_id, User.name)
        .join(User, User.id == Reaction.user_id)
        .where(Reaction.post_id.in_(ids))
        .order_by(User.name)
    )
    for post_id, emoji, reactor_id, name in rows:
        view = reactions[post_id].setdefault(emoji, ReactionView(emoji))
        view.names.append(name)
        view.mine = view.mine or reactor_id == user.id

    reply_counts = dict(
        (
            await session.execute(
                select(Post.parent_post_id, func.count())
                .where(Post.parent_post_id.in_(ids), Post.deleted_at.is_(None))
                .group_by(Post.parent_post_id)
            )
        ).all()
    )

    acks: dict[int, list[tuple[int, str, bool]]] = defaultdict(list)
    rows = await session.execute(
        select(MentionAck.post_id, MentionAck.user_id, User.name, MentionAck.acked_at)
        .join(User, User.id == MentionAck.user_id)
        .where(MentionAck.post_id.in_(ids))
        .order_by(User.name)
    )
    for post_id, target_id, name, acked_at in rows:
        acks[post_id].append((target_id, name, acked_at is not None))

    approvals = (
        await approval_views(session, ids, channel.id, user.id, access.read and access.write)
        if channel.type == "approval" and parent is None
        else {}
    )

    polls = (
        await poll_views(session, ids, user.id, access.read)
        if channel.type == "vote" and parent is None
        else {}
    )

    # 업무 메시지는 원글과 쓰레드 답글 어디에나 있다.
    works = await work_views(session, ids) if channel.type == "work" else {}

    is_manager = await authz.is_manager(channel)
    views = []
    for post in posts:
        deleted = post.deleted_at is not None
        replies = reply_counts.get(post.id, 0)
        own = post.author_id == user.id
        can_change = (
            can_write_reply(access, parent, user.id) if parent is not None else access.write
        )
        # F-58: 상신하면 본문과 첨부를 고칠 수 없고 지울 수도 없다.
        if post.id in approvals and not post.is_draft:
            can_change = False
        views.append(
            PostView(
                post=post,
                author_name=(post.id in works and works[post.id].author_name)
                or authors.get(post.author_id, ""),
                body_html=None if deleted else render_markdown(post.body_md),
                attachments=[] if deleted else attachments[post.id],
                reactions=[
                    reactions[post.id][emoji] for emoji in EMOJIS if emoji in reactions[post.id]
                ],
                reply_count=replies,
                # 업무 메시지는 남긴 그대로 둔다. 지우는 것만 일반 글과 같다.
                can_edit=own and can_change and not deleted and post.id not in works,
                can_delete=own
                and can_change
                and not deleted
                and replies == 0
                and not (channel.type == "doc" and parent is None),
                can_delete_files=own
                and can_change
                and not deleted
                and not (channel.type == "doc" and parent is None),
                can_react=access.read and not deleted,
                can_open_thread=parent is None and can_view_replies(access, post, user.id),
                can_pin=(
                    parent is None and not deleted and channel.type != "approval" and is_manager
                ),
                ack_total=0 if deleted else len(acks[post.id]),
                ack_done=sum(done for _, _, done in acks[post.id]),
                pending_names=[name for _, name, done in acks[post.id] if not done],
                can_ack=not deleted
                and any(target == user.id and not done for target, _, done in acks[post.id]),
                approval=approvals.get(post.id),
                poll=None if deleted else polls.get(post.id),
                work=None if deleted else works.get(post.id),
            )
        )
    return views


async def pinned_views(
    session: AsyncSession, authz: Authorizer, channel: Channel, access: ChannelAccess
) -> list[PinnedItem]:
    """고정 목록은 작성자와 요약만 쓰므로 글 카드 전체를 만들지 않는다."""
    rows = await session.execute(
        select(Post, User.name)
        .join(User, User.id == Post.author_id)
        .where(
            Post.channel_id == channel.id,
            Post.pinned_at.is_not(None),
            Post.deleted_at.is_(None),
            Post.parent_post_id.is_(None),
        )
        .order_by(Post.pinned_at.desc())
    )
    return [
        PinnedItem(post.id, name, plain_summary(post.body_md))
        for post, name in rows
        if can_view_post(access, post, authz.user.id)
    ]


async def timeline_posts(
    session: AsyncSession,
    channel: Channel,
    access: ChannelAccess,
    user_id: int,
    before: int | None = None,
) -> tuple[list[Post], bool]:
    """쓰기만 있는 사람은 조회 단계에서 본인 글로 좁힌다. 오래된 순으로 돌려준다."""
    query = select(Post).where(
        Post.channel_id == channel.id,
        Post.parent_post_id.is_(None),
        or_(Post.is_draft.is_(False), Post.author_id == user_id),
    )
    if not access.read:
        query = query.where(Post.author_id == user_id)
    if before is not None:
        query = query.where(Post.id < before)
    rows = list(await session.scalars(query.order_by(Post.id.desc()).limit(PAGE_SIZE + 1)))
    has_more = len(rows) > PAGE_SIZE
    return list(reversed(rows[:PAGE_SIZE])), has_more

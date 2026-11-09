"""F-15: 볼 수 있는 글만 찾는다. 채널 권한 판정 결과를 그대로 SQL 조건으로 옮긴다."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import aliased

from app.core.markdown import plain_summary
from app.models import Approval, Channel, Post, User, WorkspaceGroup
from app.services.permissions import Authorizer

RESULT_LIMIT = 50
SNIPPET = 80


@dataclass(frozen=True)
class SearchResult:
    href: str
    channel_name: str
    author_name: str
    created_at: datetime
    title: str | None
    snippet: str
    is_reply: bool


def snippet(text: str, query: str) -> str:
    plain = plain_summary(text, limit=4000)
    found = plain.lower().find(query.lower())
    start = max(found - SNIPPET // 3, 0) if found >= 0 else 0
    piece = plain[start : start + SNIPPET]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(plain) else "")


async def search_posts(
    authz: Authorizer,
    query: str,
    group_id: int | None = None,
    channel_id: int | None = None,
) -> list[SearchResult]:
    session = authz.session
    channels = select(Channel).join(WorkspaceGroup).where(WorkspaceGroup.archived_at.is_(None))
    if group_id is not None:
        channels = channels.where(Channel.group_id == group_id)
    if channel_id is not None:
        channels = channels.where(Channel.id == channel_id)
    read, write_only, replies = set(), set(), set()
    for channel in await session.scalars(channels):
        access = await authz.access(channel)
        if access.read:
            read.add(channel.id)
        elif access.write:
            write_only.add(channel.id)
        if access.reply_read:
            replies.add(channel.id)
    if not (read or write_only):
        return []

    me = authz.user.id
    parent = aliased(Post)
    approval = aliased(Approval)
    root_visible = or_(
        Post.channel_id.in_(read),
        and_(Post.channel_id.in_(write_only), Post.author_id == me),
    )
    reply_visible = and_(
        Post.channel_id.in_(replies) if replies else false(),
        or_(
            parent.channel_id.in_(read),
            and_(parent.channel_id.in_(write_only), parent.author_id == me),
        ),
    )
    rows = await session.execute(
        select(Post, Channel.name, User.name, approval.title, parent.id)
        .join(Channel, Channel.id == Post.channel_id)
        .join(User, User.id == Post.author_id)
        .outerjoin(parent, parent.id == Post.parent_post_id)
        .outerjoin(approval, approval.post_id == Post.id)
        .where(
            Post.deleted_at.is_(None),
            or_(Post.is_draft.is_(False), Post.author_id == me),
            or_(
                and_(Post.parent_post_id.is_(None), root_visible),
                and_(Post.parent_post_id.is_not(None), reply_visible),
            ),
            or_(
                Post.body_md.icontains(query, autoescape=True),
                Post.title.icontains(query, autoescape=True),
                approval.title.icontains(query, autoescape=True),
            ),
        )
        .order_by(Post.id.desc())
        .limit(RESULT_LIMIT)
    )
    results = []
    for post, channel_name, author_name, approval_title, parent_id in rows:
        root = parent_id or post.id
        # 답글의 원글이 결재면 /posts/{id}가 결재 화면으로 넘겨 준다.
        page = f"/approvals/{root}" if approval_title else f"/posts/{root}"
        results.append(
            SearchResult(
                href=page if parent_id is None else f"{page}#post-{post.id}",
                channel_name=channel_name,
                author_name=author_name,
                created_at=post.created_at,
                title=approval_title or post.title,
                snippet=snippet(post.body_md, query),
                is_reply=parent_id is not None,
            )
        )
    return results

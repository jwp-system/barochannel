from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Post, PostRevision, User
from app.services.permissions import ChannelAccess

DOCUMENT_LIMIT = 500


@dataclass(frozen=True)
class DocumentRow:
    post_id: int
    title: str
    author_name: str
    updated_at: datetime
    versions: int


async def document_rows(
    session: AsyncSession, channel: Channel, access: ChannelAccess, user_id: int
) -> list[DocumentRow]:
    """최근에 고친 문서부터. 쓰기만 있는 사람은 자기 문서만 본다."""
    versions = (
        select(PostRevision.post_id, func.count().label("versions"))
        .group_by(PostRevision.post_id)
        .subquery()
    )
    updated = func.coalesce(Post.edited_at, Post.created_at)
    query = (
        select(Post, User.name, updated, func.coalesce(versions.c.versions, 0))
        .join(User, User.id == Post.author_id)
        .outerjoin(versions, versions.c.post_id == Post.id)
        .where(Post.channel_id == channel.id, Post.parent_post_id.is_(None))
        .order_by(updated.desc())
        .limit(DOCUMENT_LIMIT)
    )
    if not access.read:
        query = query.where(Post.author_id == user_id)
    rows = await session.execute(query)
    return [
        DocumentRow(post.id, post.title or "(제목 없음)", name, when, count)
        for post, name, when, count in rows
    ]

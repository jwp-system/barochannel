"""채널별 새 글 개수. 채널의 글만 세고 답글은 세지 않는다.

소통·문서·투표·업무 채널만 센다. 결재는 내 할 일에 처리할 건이 따로 나온다.
읽기 권한이 없는 채널(쓰기만 되는 건의함 등)은 남의 글이 있다는 사실이 새지 않도록 세지 않는다.
"""

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import ChannelRead, Post

COUNTED_TYPES = ("talk", "doc", "vote", "work")


def _channel_posts():
    """새 글로 셀 수 있는 글. 지운 글과 임시저장은 빼고 채널의 글만 본다."""
    return (
        Post.parent_post_id.is_(None),
        Post.deleted_at.is_(None),
        Post.is_draft.is_(False),
    )


async def ensure_marks(user_id: int, channel_ids: Iterable[int]) -> None:
    """처음 보는 채널은 지금까지의 글을 읽은 것으로 시작한다.

    화면을 그리는 요청의 트랜잭션과 섞이지 않도록 따로 연결해 바로 커밋한다.
    """
    wanted = set(channel_ids)
    if not wanted:
        return
    async with SessionLocal() as session:
        known = set(
            await session.scalars(
                select(ChannelRead.channel_id).where(
                    ChannelRead.user_id == user_id, ChannelRead.channel_id.in_(wanted)
                )
            )
        )
        missing = wanted - known
        if not missing:
            return
        latest = dict(
            (
                await session.execute(
                    select(Post.channel_id, func.max(Post.id))
                    .where(Post.channel_id.in_(missing), Post.parent_post_id.is_(None))
                    .group_by(Post.channel_id)
                )
            ).all()
        )
        await session.execute(
            insert(ChannelRead)
            .values(
                [
                    {
                        "user_id": user_id,
                        "channel_id": channel_id,
                        "last_post_id": latest.get(channel_id, 0),
                    }
                    for channel_id in sorted(missing)
                ]
            )
            .on_conflict_do_nothing()
        )
        await session.commit()


async def unread_counts(
    session: AsyncSession, user_id: int, channel_ids: Iterable[int]
) -> dict[int, int]:
    """읽은 표시보다 뒤에 올라온 남의 채널 글 수. 0인 채널은 결과에 없다."""
    wanted = set(channel_ids)
    if not wanted:
        return {}
    rows = await session.execute(
        select(Post.channel_id, func.count())
        .join(
            ChannelRead,
            (ChannelRead.channel_id == Post.channel_id) & (ChannelRead.user_id == user_id),
        )
        .where(
            Post.channel_id.in_(wanted),
            Post.id > ChannelRead.last_post_id,
            Post.author_id != user_id,
            *_channel_posts(),
        )
        .group_by(Post.channel_id)
    )
    return dict(rows.all())


async def mark_read(session: AsyncSession, user_id: int, channel_id: int) -> None:
    """채널의 마지막 글까지 읽은 것으로 한다. 표시는 앞으로만 움직인다."""
    latest = await session.scalar(
        select(func.max(Post.id)).where(
            Post.channel_id == channel_id, Post.parent_post_id.is_(None)
        )
    )
    statement = insert(ChannelRead).values(
        user_id=user_id, channel_id=channel_id, last_post_id=latest or 0
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ChannelRead.user_id, ChannelRead.channel_id],
            set_={
                "last_post_id": func.greatest(
                    ChannelRead.last_post_id, statement.excluded.last_post_id
                )
            },
        )
    )

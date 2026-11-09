"""투표 규칙과 결과 화면 모델. 결과 공개 시점과 익명 여부는 여기서만 판정한다."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Poll, PollOption, PollVote, PollVoteHistory, User

MIN_OPTIONS = 2
MAX_OPTIONS = 20


def is_closed(poll: Poll, now: datetime | None = None) -> bool:
    return (now or datetime.now(UTC)) >= poll.closes_at


def results_visible(poll: Poll, now: datetime | None = None) -> bool:
    return poll.result_open == "immediate" or is_closed(poll, now)


@dataclass
class OptionView:
    id: int
    label: str
    count: int = 0
    percent: int = 0
    names: list[str] = field(default_factory=list)
    mine: bool = False


@dataclass
class PollView:
    poll: Poll
    options: list[OptionView]
    closed: bool
    visible: bool
    voters: int
    changed: int | None
    voted: bool
    can_vote: bool


async def poll_views(
    session: AsyncSession, post_ids: Sequence[int], user_id: int, can_read: bool
) -> dict[int, PollView]:
    polls = list(await session.scalars(select(Poll).where(Poll.post_id.in_(post_ids))))
    if not polls:
        return {}
    ids = [poll.post_id for poll in polls]
    options: dict[int, list[PollOption]] = defaultdict(list)
    for option in await session.scalars(
        select(PollOption).where(PollOption.post_id.in_(ids)).order_by(PollOption.position)
    ):
        options[option.post_id].append(option)
    votes: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    rows = await session.execute(
        select(PollVote.post_id, PollVote.option_id, PollVote.user_id, User.name)
        .join(User, User.id == PollVote.user_id)
        .where(PollVote.post_id.in_(ids))
        .order_by(User.name)
    )
    for post_id, option_id, voter_id, name in rows:
        votes[post_id].append((option_id, voter_id, name))
    # 변심자: 이미 고른 선택을 마감 전에 바꾼 사람. 처음 투표는 세지 않는다.
    changed = dict(
        (
            await session.execute(
                select(PollVoteHistory.post_id, func.count(func.distinct(PollVoteHistory.user_id)))
                .where(
                    PollVoteHistory.post_id.in_(ids),
                    func.cardinality(PollVoteHistory.before_option_ids) > 0,
                )
                .group_by(PollVoteHistory.post_id)
            )
        ).all()
    )

    now = datetime.now(UTC)
    views: dict[int, PollView] = {}
    for poll in polls:
        closed = is_closed(poll, now)
        visible = results_visible(poll, now)
        voters = {voter_id for _, voter_id, _ in votes[poll.post_id]}
        mine = {option_id for option_id, voter_id, _ in votes[poll.post_id] if voter_id == user_id}
        option_views = []
        for option in options[poll.post_id]:
            chosen = [
                (voter_id, name)
                for option_id, voter_id, name in votes[poll.post_id]
                if option_id == option.id
            ]
            view = OptionView(option.id, option.label, mine=option.id in mine)
            if visible:
                view.count = len(chosen)
                view.percent = round(len(chosen) * 100 / len(voters)) if voters else 0
                if not poll.anonymous:
                    view.names = [name for _, name in chosen]
            option_views.append(view)
        views[poll.post_id] = PollView(
            poll=poll,
            options=option_views,
            closed=closed,
            visible=visible,
            voters=len(voters) if visible else 0,
            changed=changed.get(poll.post_id, 0) if visible and not poll.anonymous else None,
            voted=bool(mine),
            can_vote=can_read and not closed,
        )
    return views

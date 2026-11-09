from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Poll(Base):
    __tablename__ = "poll"
    __table_args__ = (
        CheckConstraint("result_open IN ('immediate', 'after_close')", name="result_open"),
    )

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    multi_select: Mapped[bool] = mapped_column(Boolean, default=False)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    result_open: Mapped[str] = mapped_column(String(20), default="immediate")
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PollOption(Base):
    __tablename__ = "poll_option"
    __table_args__ = (UniqueConstraint("post_id", "position", name="position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("poll.post_id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(200))


class PollVote(Base):
    __tablename__ = "poll_vote"

    post_id: Mapped[int] = mapped_column(ForeignKey("poll.post_id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    option_id: Mapped[int] = mapped_column(ForeignKey("poll_option.id"), primary_key=True)


class PollVoteHistory(Base):
    """투표와 변경을 모두 남긴다. 처음 투표는 before가 빈 배열이다."""

    __tablename__ = "poll_vote_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("poll.post_id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    before_option_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    after_option_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

CHANNEL_TYPES = {
    "talk": "소통",
    "vote": "투표",
    "doc": "문서",
    "approval": "결재",
    "work": "업무",
}


class WorkspaceGroup(Base):
    __tablename__ = "workspace_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GroupManager(Base):
    """시스템 관리자가 지정한다. 그룹 안의 채널 생성과 관리를 맡는다."""

    __tablename__ = "group_manager"

    group_id: Mapped[int] = mapped_column(ForeignKey("workspace_group.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True, index=True)


class Channel(Base):
    __tablename__ = "channel"
    __table_args__ = (
        CheckConstraint("type IN ('talk', 'vote', 'doc', 'approval', 'work')", name="type"),
        CheckConstraint("pin_limit >= 0", name="pin_limit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("workspace_group.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    pin_limit: Mapped[int] = mapped_column(Integer, default=5)
    approval_template_md: Mapped[str | None] = mapped_column(Text)
    default_publish_channel_id: Mapped[int | None] = mapped_column(ForeignKey("channel.id"))

    group: Mapped[WorkspaceGroup] = relationship(lazy="joined")


class PermissionGrant(Base):
    __tablename__ = "permission_grant"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "subject_type", "subject_id", name="target"),
        CheckConstraint("scope_type IN ('group', 'channel')", name="scope_type"),
        CheckConstraint("subject_type IN ('user', 'department')", name="subject_type"),
        *[
            CheckConstraint(f"{column} IN ('allow', 'deny')", name=column)
            for column in ("can_read", "can_write", "can_reply_read", "can_reply_write")
        ],
        Index("ix_permission_grant_subject", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[int] = mapped_column(Integer)
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[int] = mapped_column(Integer)
    can_read: Mapped[str | None] = mapped_column(String(10))
    can_write: Mapped[str | None] = mapped_column(String(10))
    can_reply_read: Mapped[str | None] = mapped_column(String(10))
    can_reply_write: Mapped[str | None] = mapped_column(String(10))

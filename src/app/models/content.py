from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Post(Base):
    __tablename__ = "post"
    __table_args__ = (Index("ix_post_channel_top", "channel_id", "parent_post_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"))
    parent_post_id: Mapped[int | None] = mapped_column(ForeignKey("post.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    body_md: Mapped[str] = mapped_column(Text)
    # 상신 전 결재 문서. 작성자 외에는 어디에도 보이지 않는다.
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pinned_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))


class PostRevision(Base):
    __tablename__ = "post_revision"

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    body_md: Mapped[str] = mapped_column(Text)
    edited_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    size: Mapped[int] = mapped_column(BigInteger)
    mime: Mapped[str] = mapped_column(String(100))
    # 글과 같이 자리만 지운다. 저장소의 파일과 행은 이력으로 남긴다.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


class MentionAck(Base):
    """멘션 대상자마다 한 행이다. acked_at이 비어 있으면 미확인이다."""

    __tablename__ = "mention_ack"
    __table_args__ = (
        Index(
            "ix_mention_ack_pending",
            "user_id",
            postgresql_where=text("acked_at IS NULL"),
        ),
    )

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    # 직접 멘션이면 비어 있고, @소속으로 펼쳐졌으면 그 소속이다.
    source_department_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reaction(Base):
    __tablename__ = "reaction"

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    emoji: Mapped[str] = mapped_column(String(16), primary_key=True)

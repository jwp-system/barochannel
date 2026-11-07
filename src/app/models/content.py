from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
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


class Reaction(Base):
    __tablename__ = "reaction"

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    emoji: Mapped[str] = mapped_column(String(16), primary_key=True)

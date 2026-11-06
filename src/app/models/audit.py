from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditLog(Base):
    """추가 전용 로그. 수정과 삭제는 마이그레이션의 트리거가 막는다."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_target", "target_type", "target_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    action: Mapped[str] = mapped_column(String(50))
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

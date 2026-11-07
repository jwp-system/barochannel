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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

STATUS_LABELS = {
    "draft": "임시저장",
    "in_progress": "결재 중",
    "approved": "승인 완료",
    "rejected": "반려",
    "withdrawn": "기안 취소",
    "discarded": "폐기",
}
REVIEW_LABELS = {"pass": "적격", "revise": "보완필", "fail": "부적격"}


class ApprovalLineTemplate(Base):
    """채널과 소속별 승인선 원본. 상신하면 approval_step으로 복사되어 이후 변경과 무관하다."""

    __tablename__ = "approval_line_template"
    __table_args__ = (
        UniqueConstraint("channel_id", "department_id", "step_no", name="step"),
        Index("ix_approval_line_template_approver", "approver_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("department.id"))
    step_no: Mapped[int] = mapped_column(Integer)
    approver_id: Mapped[int] = mapped_column(ForeignKey("user.id"))


class Approval(Base):
    __tablename__ = "approval"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'in_progress', 'approved', 'rejected', 'withdrawn', 'discarded')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), unique=True)
    drafter_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    # 문서에 지정한 기안 소속. 이 소속의 승인선을 적용한다.
    department_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_post_id: Mapped[int | None] = mapped_column(ForeignKey("post.id"))
    reference_post_id: Mapped[int | None] = mapped_column(ForeignKey("post.id"))
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discard_reason: Mapped[str | None] = mapped_column(Text)


class ApprovalStep(Base):
    """상신 시점의 승인선 사본. 0단계는 기안자다."""

    __tablename__ = "approval_step"
    __table_args__ = (
        CheckConstraint("step_type IN ('draft', 'approve')", name="step_type"),
        CheckConstraint("status IN ('waiting', 'approved', 'rejected')", name="status"),
    )

    approval_id: Mapped[int] = mapped_column(ForeignKey("approval.id"), primary_key=True)
    step_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_type: Mapped[str] = mapped_column(String(10), default="approve")
    approver_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="waiting")
    comment: Mapped[str | None] = mapped_column(Text)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalReview(Base):
    __tablename__ = "approval_review"
    __table_args__ = (CheckConstraint("result IN ('pass', 'revise', 'fail')", name="result"),)

    approval_id: Mapped[int] = mapped_column(ForeignKey("approval.id"), primary_key=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True, index=True)
    result: Mapped[str | None] = mapped_column(String(10))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    """결재 결과처럼 처리할 일은 없지만 알아야 하는 건. 개수 표시에만 쓴다."""

    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_unread", "user_id", postgresql_where=text("read_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    type: Mapped[str] = mapped_column(String(30))
    ref_id: Mapped[int] = mapped_column(Integer)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Department(Base):
    __tablename__ = "department"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    auth_provider: Mapped[str] = mapped_column(String(20), default="local")
    external_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")
    # 비어 있으면 초기 PIN 상태다. 시스템 관리자만 쓴다.
    pin_hash: Mapped[str | None] = mapped_column(String(255))
    pin_failed_count: Mapped[int] = mapped_column(Integer, default=0)
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # 요청마다 current_user가 정한다. 저장하지 않으며 기본값은 관리 권한 없음이다.
    admin_mode = False

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class UserDepartment(Base):
    __tablename__ = "user_department"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("department.id"), primary_key=True, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

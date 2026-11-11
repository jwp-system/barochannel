"""시스템 관리자. 로그인 정보는 환경 변수로만 관리한다."""

import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import check_password
from app.models import User

ADMIN_NAME = "시스템 관리자"


def is_admin_email(email: str) -> bool:
    admin_email = get_settings().admin_email
    return bool(admin_email) and email.lower() == admin_email


def is_system_admin(user: User) -> bool:
    return is_admin_email(user.email)


async def admin_login(session: AsyncSession, password: str) -> User | None:
    """환경 변수의 비밀번호로 확인한다. 사용자 행은 기록을 남기기 위해 처음 로그인할 때 만든다."""
    settings = get_settings()
    await check_password(None, password)  # 일반 계정과 응답 시간을 맞춘다.
    expected = settings.admin_password
    if not expected or not secrets.compare_digest(password.encode(), expected.encode()):
        return None
    await session.execute(
        insert(User)
        .values(name=ADMIN_NAME, email=settings.admin_email, status="active")
        .on_conflict_do_nothing(index_elements=[User.email])
    )
    await session.commit()
    return await session.scalar(select(User).where(User.email == settings.admin_email))

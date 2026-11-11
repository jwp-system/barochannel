from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionDep
from app.models import Department, User, UserDepartment
from app.services.admin import is_system_admin
from app.services.permissions import Authorizer


class LoginRequired(Exception):
    pass


async def primary_department_name(session: AsyncSession, user_id: int) -> str:
    """주 소속이 없으면 소속 중 하나를 쓴다."""
    name = await session.scalar(
        select(Department.name)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id)
        .order_by(UserDepartment.is_primary.desc(), Department.id)
        .limit(1)
    )
    return name or ""


async def current_user(request: Request, session: SessionDep) -> User:
    user_id = request.session.get("user_id")
    user = await session.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.clear()
        raise LoginRequired
    # 시스템 관리자는 로그인하면 바로 관리자 모드다.
    user.admin_mode = is_system_admin(user)
    user.department_name = await primary_department_name(session, user.id)
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def get_authorizer(session: SessionDep, user: CurrentUser) -> Authorizer:
    return Authorizer(session, user)


AuthzDep = Annotated[Authorizer, Depends(get_authorizer)]


async def require_admin(user: CurrentUser) -> User:
    if not is_system_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "시스템 관리자만 할 수 있습니다.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]

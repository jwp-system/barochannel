from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.db import SessionDep
from app.models import User
from app.services.admin import (
    PIN_CHANGE_PATH,
    admin_mode_active,
    is_system_admin,
    must_change_pin,
)
from app.services.permissions import Authorizer


class LoginRequired(Exception):
    pass


class AdminPinRequired(Exception):
    pass


class PinChangeRequired(Exception):
    pass


async def current_user(request: Request, session: SessionDep) -> User:
    user_id = request.session.get("user_id")
    user = await session.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.clear()
        raise LoginRequired
    if must_change_pin(user) and request.url.path != PIN_CHANGE_PATH:
        raise PinChangeRequired
    user.admin_mode = admin_mode_active(request.session, user)
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def get_authorizer(session: SessionDep, user: CurrentUser) -> Authorizer:
    return Authorizer(session, user)


AuthzDep = Annotated[Authorizer, Depends(get_authorizer)]


async def require_admin(user: CurrentUser) -> User:
    if not is_system_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "시스템 관리자만 할 수 있습니다.")
    if not user.admin_mode:
        raise AdminPinRequired
    return user


AdminUser = Annotated[User, Depends(require_admin)]

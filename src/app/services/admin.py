"""시스템 관리자. 로그인 정보는 환경 변수, PIN은 DB에 둔다."""

import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import check_password, hash_password
from app.models import User
from app.services.audit import record_audit

INITIAL_PIN = "123456"
MAX_PIN_FAILURES = 5
PIN_LOCK_MINUTES = 15
DEFAULT_NEXT = "/admin/org"
PIN_CHANGE_PATH = "/admin/pin/change"
ADMIN_NAME = "시스템 관리자"


@dataclass(frozen=True)
class PinResult:
    message: str
    admin_until: float | None = None


def is_admin_email(email: str) -> bool:
    admin_email = get_settings().admin_email
    return bool(admin_email) and email.lower() == admin_email


def is_system_admin(user: User) -> bool:
    return is_admin_email(user.email)


def must_change_pin(user: User) -> bool:
    return is_system_admin(user) and user.pin_hash is None


def admin_mode_active(session_data: Mapping[str, Any], user: User) -> bool:
    return is_system_admin(user) and session_data.get("admin_until", 0) > time.time()


def safe_next(target: str) -> str:
    """다른 사이트로 보내는 주소를 막는다."""
    if target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return DEFAULT_NEXT


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


def _pin_secret(pin: str) -> str:
    """6자리는 해시만으로는 쉽게 풀리므로 서버 비밀값을 섞는다. DB만 유출돼서는 풀 수 없다."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, pin.encode(), hashlib.sha256).hexdigest()


def hash_pin(pin: str) -> str:
    return hash_password(_pin_secret(pin))


async def _pin_matches(user: User, pin: str) -> bool:
    if user.pin_hash is None:
        return secrets.compare_digest(pin.encode(), INITIAL_PIN.encode())
    return await check_password(user.pin_hash, _pin_secret(pin))


def weak_pin_reason(pin: str) -> str | None:
    if not (len(pin) == 6 and pin.isascii() and pin.isdigit()):
        return "PIN은 숫자 6자리여야 합니다."
    steps = {int(right) - int(left) for left, right in zip(pin, pin[1:], strict=False)}
    if steps in ({0}, {1}, {-1}):
        return "같은 숫자나 연속된 숫자는 쓸 수 없습니다."
    return None


async def _locked_row(session: AsyncSession, user: User) -> User:
    """동시 시도가 실패 횟수를 건너뛰지 않도록 행을 잠근다."""
    return await session.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _verify(session: AsyncSession, user: User, pin: str) -> str | None:
    """맞으면 None을 돌려준다. 실패 횟수는 DB에 두어 다시 로그인해도 초기화되지 않는다."""
    now = datetime.now(UTC)
    if user.pin_locked_until is not None and user.pin_locked_until > now:
        await session.rollback()
        return f"PIN 입력이 잠겼습니다. {PIN_LOCK_MINUTES}분 뒤에 다시 시도해 주세요."
    if await _pin_matches(user, pin):
        user.pin_failed_count = 0
        user.pin_locked_until = None
        return None

    user.pin_failed_count += 1
    remaining = MAX_PIN_FAILURES - user.pin_failed_count
    if remaining <= 0:
        user.pin_failed_count = 0
        user.pin_locked_until = now + timedelta(minutes=PIN_LOCK_MINUTES)
    record_audit(
        session,
        actor_id=user.id,
        action="admin.pin_failure" if remaining > 0 else "admin.pin_locked",
        target_type="user",
        target_id=user.id,
    )
    await session.commit()
    if remaining <= 0:
        return f"PIN을 {MAX_PIN_FAILURES}회 틀려 {PIN_LOCK_MINUTES}분간 잠겼습니다."
    return f"PIN이 맞지 않습니다. {remaining}회 남았습니다."


def _open_admin_mode() -> float:
    return time.time() + get_settings().admin_mode_minutes * 60


async def check_pin(session: AsyncSession, user: User, pin: str) -> PinResult:
    locked = await _locked_row(session, user)
    if locked.pin_hash is None:
        # 초기 PIN으로는 관리자 모드를 열지 않는다.
        await session.rollback()
        return PinResult("PIN을 먼저 변경해 주세요.")
    message = await _verify(session, locked, pin)
    if message is not None:
        return PinResult(message)
    record_audit(
        session,
        actor_id=user.id,
        action="admin.pin_success",
        target_type="user",
        target_id=user.id,
    )
    await session.commit()
    return PinResult("", _open_admin_mode())


async def change_pin(
    session: AsyncSession, user: User, current_pin: str, new_pin: str, confirm_pin: str
) -> PinResult:
    locked = await _locked_row(session, user)
    message = await _verify(session, locked, current_pin)
    if message is not None:
        return PinResult(message)
    problem = weak_pin_reason(new_pin)
    if problem is None and new_pin != confirm_pin:
        problem = "새 PIN이 서로 다릅니다."
    if problem is None and new_pin == current_pin:
        problem = "지금 쓰는 PIN과 다르게 정해 주세요."
    if problem is not None:
        await session.commit()
        return PinResult(problem)
    locked.pin_hash = hash_pin(new_pin)
    record_audit(
        session,
        actor_id=user.id,
        action="admin.pin_change",
        target_type="user",
        target_id=user.id,
    )
    await session.commit()
    return PinResult("", _open_admin_mode())


async def reset_pin(session: AsyncSession) -> bool:
    """PIN을 잊었을 때 서버에서 초기 상태로 되돌린다."""
    user = await session.scalar(select(User).where(User.email == get_settings().admin_email))
    if user is None:
        return False
    user.pin_hash = None
    user.pin_failed_count = 0
    user.pin_locked_until = None
    record_audit(
        session,
        actor_id=None,
        action="admin.pin_reset",
        target_type="user",
        target_id=user.id,
        via="cli",
    )
    await session.commit()
    return True

import secrets

from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, status

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"

_hasher = PasswordHasher()
_dummy_hash = _hasher.hash("dummy-password-for-constant-time")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """계정이 없을 때도 같은 시간이 걸리도록 더미 해시를 검증한다."""
    try:
        return _hasher.verify(password_hash or _dummy_hash, password) and bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


async def check_password(password_hash: str | None, password: str) -> bool:
    """해시 검증은 수십 ms가 걸리므로 다른 요청을 막지 않게 스레드에서 돌린다."""
    return await to_thread.run_sync(verify_password, password_hash, password)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """앱 전체 의존성으로 걸어 변경 요청이 빠지지 않게 한다."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    received = request.headers.get(CSRF_HEADER)
    if not expected or not received or not secrets.compare_digest(expected, received):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "보안 토큰이 맞지 않습니다. 새로고침해 주세요."
        )

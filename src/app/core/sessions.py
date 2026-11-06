import itsdangerous
from itsdangerous.encoding import base64_encode, int_to_bytes, want_bytes
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp

CLOCK_TOLERANCE_SECONDS = 5


class BackdatedSigner(itsdangerous.TimestampSigner):
    """서버 시계가 뒤로 조정되면 방금 만든 쿠키가 미래의 것으로 보여 세션이 끊긴다.

    WSL은 호스트와 맞추느라 시계를 주기적으로 되돌린다. 발급 시각만 앞당겨 이를 흡수한다.
    get_timestamp는 검증에도 쓰이므로 그쪽을 바꾸면 양쪽이 같이 밀려 효과가 없다.
    """

    def sign(self, value: str | bytes) -> bytes:
        issued_at = self.get_timestamp() - CLOCK_TOLERANCE_SECONDS
        separator = want_bytes(self.sep)
        stamped = want_bytes(value) + separator + base64_encode(int_to_bytes(issued_at))
        return stamped + separator + self.get_signature(stamped)


class ClockTolerantSessionMiddleware(SessionMiddleware):
    def __init__(self, app: ASGIApp, secret_key: str, **options: object) -> None:
        super().__init__(app, secret_key, **options)
        self.signer = BackdatedSigner(secret_key)

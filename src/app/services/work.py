"""업무 채널(v3). 화면 정의 읽기, 외부 API 중계, 채널 메시지 검사는 여기서만 한다.

업무 화면은 templates/works/{화면}/에 코드로 둔다. 화면은 바로채널을 거쳐서만 외부 시스템을
부르고, 부를 수 있는 경로는 화면 정의(screen.toml)에 적힌 것뿐이다. 외부 시스템은 믿지 않는다.
호출할 수 있는 호스트를 설정으로 제한하고, 리다이렉트를 따라가지 않고, 응답 크기를 제한한다.
"""

import hashlib
import hmac
import json
import re
import time
import tomllib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Channel, User, WorkCommand, WorkMessage

SCREENS_DIR = Path(__file__).resolve().parent.parent / "templates" / "works"
COMMAND_NAME = re.compile(r"^[a-z0-9가-힣_-]{1,30}$")
SCREEN_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PARAM_KEY = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
SEGMENT = re.compile(r"^[A-Za-z0-9_.~-]{1,100}$")
PLACEHOLDER = re.compile(r"^\{[a-z_]{1,30}\}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,58}$")
API_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
SIZES = ("small", "medium", "large", "full")

MAX_LABEL = 100
MAX_TEXT = 1000
MAX_FIELDS = 6
MAX_FIELD_LABEL = 30
MAX_FIELD_VALUE = 200
MAX_PARAMS = 5
MAX_PARAM_VALUE = 100
MAX_QUERY = 2000
MAX_REQUEST_BODY = 100_000
MAX_RESPONSE_BODY = 1_000_000
CALLS_PER_MINUTE = 120
WEBHOOK_TOLERANCE_SECONDS = 300
EXTERNAL_AUTHOR = "외부 시스템"

# 시험이 외부 시스템 대신 ASGI 앱을 끼울 수 있게 둔다.
transport: httpx2.AsyncBaseTransport | None = None


class IntegrationError(Exception):
    """사용자에게 그대로 보여줄 수 있는 문구를 담는다."""


class ScreenError(Exception):
    """화면 정의가 잘못됐다. 앱을 시작하지 않는다."""


class MessageError(ValueError):
    """남길 메시지의 형식이 틀렸다. 문구를 그대로 보여준다."""


# 화면 정의


@dataclass(frozen=True)
class ApiRoute:
    method: str
    parts: tuple[str, ...]

    def matches(self, method: str, parts: Sequence[str]) -> bool:
        if method != self.method or len(parts) != len(self.parts):
            return False
        return all(
            PLACEHOLDER.match(want) or want == got
            for want, got in zip(self.parts, parts, strict=True)
        )


@dataclass(frozen=True)
class Screen:
    name: str
    title: str
    description: str
    # 시작 화면 이름과 표시 이름. 명령어가 이 중 하나를 고른다.
    pages: dict[str, str]
    detail: str | None
    size: str
    api: tuple[ApiRoute, ...]

    def allows(self, method: str, parts: Sequence[str]) -> bool:
        return any(route.matches(method, parts) for route in self.api)


def _route(text: object) -> ApiRoute:
    if not isinstance(text, str):
        raise ScreenError('api 항목은 "메서드 경로" 글자여야 합니다.')
    method, _, path = text.strip().partition(" ")
    if method not in API_METHODS:
        raise ScreenError(f"쓸 수 없는 메서드입니다: {text}")
    parts = tuple(part for part in path.strip().strip("/").split("/") if part)
    for part in parts:
        if not (PLACEHOLDER.match(part) or SEGMENT.match(part)) or part in (".", ".."):
            raise ScreenError(f"경로가 맞지 않습니다: {text}")
    return ApiRoute(method, parts)


def load_screen(folder: Path) -> Screen:
    name = folder.name
    if not SCREEN_NAME.match(name):
        raise ScreenError(f"화면 폴더 이름이 맞지 않습니다: {name}")
    try:
        raw = tomllib.loads((folder / "screen.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ScreenError(f"{name}/screen.toml을 읽지 못했습니다: {error}") from error
    pages = raw.get("pages")
    if not isinstance(pages, dict) or not pages:
        raise ScreenError(f"{name}: pages에 시작 화면을 하나 이상 적어야 합니다.")
    detail = raw.get("detail")
    for page in [*pages, *([detail] if detail else [])]:
        if not isinstance(page, str) or not SCREEN_NAME.match(page):
            raise ScreenError(f"{name}: 화면 이름이 맞지 않습니다: {page}")
        if not (folder / f"{page}.html").is_file():
            raise ScreenError(f"{name}: {page}.html이 없습니다.")
    size = raw.get("size", "medium")
    if size not in SIZES:
        raise ScreenError(f"{name}: size는 {', '.join(SIZES)} 중 하나입니다.")
    api = raw.get("api", [])
    if not isinstance(api, list):
        raise ScreenError(f"{name}: api는 목록이어야 합니다.")
    return Screen(
        name=name,
        title=str(raw.get("title") or name),
        description=str(raw.get("description") or ""),
        pages={key: str(value) for key, value in pages.items()},
        detail=detail or None,
        size=size,
        api=tuple(_route(item) for item in api),
    )


@cache
def screens() -> dict[str, Screen]:
    """앱을 시작할 때 모든 화면을 읽어 검사한다. 하나라도 틀리면 ScreenError."""
    if not SCREENS_DIR.is_dir():
        return {}
    return {
        folder.name: load_screen(folder)
        for folder in sorted(SCREENS_DIR.iterdir())
        if folder.is_dir() and not folder.name.startswith(("_", "."))
    }


def screen_for(command: WorkCommand) -> Screen | None:
    return screens().get(command.screen)


def api_parts(path: str) -> tuple[str, ...] | None:
    """화면이 부른 경로를 마디로 나눈다. 빈 마디, ., .., 허용하지 않는 글자는 None."""
    if path == "":
        return ()
    parts = tuple(path.split("/"))
    if any(not SEGMENT.match(part) or part in (".", "..") for part in parts):
        return None
    return parts


# 연동 주소와 외부 호출


def check_endpoint(url: str) -> str:
    """연동 주소를 검사해 끝의 /를 뗀 주소를 돌려준다. 틀리면 ValueError."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("연동 주소는 http:// 또는 https://로 시작해야 합니다.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("연동 주소에는 계정, 물음표 뒤 값, # 뒤 값을 넣을 수 없습니다.")
    if not host_allowed(url):
        netloc = f"{parts.hostname.lower()}:{parts.port}" if parts.port else parts.hostname
        raise ValueError(
            f"허용되지 않은 호스트입니다: {netloc}. 시스템 관리자에게 "
            "BARO_INTEGRATION_ALLOWED_HOSTS 등록을 요청해 주세요."
        )
    return url


def host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    host = parts.hostname.lower()
    allowed = get_settings().integration_allowed_hosts
    try:
        netloc = f"{host}:{parts.port}" if parts.port else host
    except ValueError:
        return False
    return host in allowed or netloc in allowed


def _header_text(value: str) -> str:
    """머리글은 ASCII만 실린다. 한글 이름은 퍼센트 인코딩해 보낸다."""
    return quote(value, safe="@.+-_ ")


def user_headers(user: User, channel: Channel, command: WorkCommand) -> dict[str, str]:
    """요청한 사람. 외부 시스템은 이 사람 기준으로 권한을 판정한다."""
    return {
        "X-Baro-User-Id": str(user.id),
        "X-Baro-User-Email": _header_text(user.email),
        "X-Baro-User-Name": _header_text(user.name),
        "X-Baro-Channel-Id": str(channel.id),
        "X-Baro-Command": _header_text(command.name),
    }


@dataclass(frozen=True)
class Relayed:
    status: int
    content: bytes


def _error(status: int, message: str) -> Relayed:
    return Relayed(status, json.dumps({"error": message}, ensure_ascii=False).encode())


async def relay(
    command: WorkCommand,
    method: str,
    parts: Sequence[str],
    query: str,
    body: bytes,
    headers: Mapping[str, str],
) -> Relayed:
    """외부 시스템을 불러 상태 코드와 JSON 본문을 그대로 돌려준다.

    외부의 응답 머리글은 넘기지 않는다(쿠키 등). 3xx는 따라가지 않고, 5xx는 502로 바꾼다.
    """
    try:
        base = check_endpoint(command.endpoint_url)
    except ValueError as error:
        return _error(502, str(error))
    url = base + "".join("/" + quote(part, safe="") for part in parts)
    if query:
        url += "?" + query
    sent = {"Accept": "application/json", "User-Agent": "barochannel-work/3", **headers}
    if command.api_token:
        sent["Authorization"] = f"Bearer {command.api_token}"
    if body:
        sent["Content-Type"] = "application/json"
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            timeout=get_settings().integration_timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, url, content=body or None, headers=sent)
    except httpx2.TimeoutException:
        return _error(504, "외부 시스템이 제때 답하지 않았습니다.")
    except httpx2.HTTPError:
        return _error(502, "외부 시스템에 연결하지 못했습니다.")
    if len(response.content) > MAX_RESPONSE_BODY:
        return _error(502, "외부 시스템의 응답이 너무 큽니다.")
    if response.status_code >= 500 or 300 <= response.status_code < 400:
        return _error(502, "외부 시스템에서 오류가 났습니다.")
    if not response.content:
        return Relayed(response.status_code, b"")
    try:
        json.loads(response.content)
    except ValueError:
        return _error(502, "외부 시스템의 응답이 JSON이 아닙니다.")
    return Relayed(response.status_code, response.content)


class RateLimiter:
    """사용자마다 1분에 정해진 횟수까지 중계한다. 프로세스마다 따로 센다."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.counts: dict[tuple[int, int], int] = defaultdict(int)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        minute = int((now if now is not None else time.time()) // 60)
        if len(self.counts) > 10_000:
            self.counts = defaultdict(int, {k: v for k, v in self.counts.items() if k[1] == minute})
        self.counts[(user_id, minute)] += 1
        return self.counts[(user_id, minute)] <= self.limit


limiter = RateLimiter(CALLS_PER_MINUTE)


# 채널에 남기는 메시지


@dataclass
class MessageInput:
    text: str
    fields: list[dict[str, str]] = field(default_factory=list)
    detail: dict[str, str] | None = None
    link_label: str = ""
    link_url: str | None = None
    reply_to: int | None = None


def _string(value: object, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise MessageError(f"{name}은(는) 글자여야 합니다.")
    text = str(value).strip()
    if required and not text:
        raise MessageError(f"{name}이(가) 비어 있습니다.")
    if len(text) > limit:
        raise MessageError(f"{name}은(는) {limit}자까지입니다.")
    return text


def parse_message(raw: object, *, detail_allowed: bool, reply_allowed: bool) -> MessageInput:
    """화면이나 외부 시스템이 보낸 메시지를 검사한다. 틀리면 MessageError."""
    if not isinstance(raw, Mapping):
        raise MessageError("메시지는 JSON 객체여야 합니다.")
    message = MessageInput(text=_string(raw.get("text"), "text", MAX_TEXT, required=True))
    fields = raw.get("fields") or []
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise MessageError(f"fields는 {MAX_FIELDS}개까지의 목록입니다.")
    for item in fields:
        if not isinstance(item, Mapping):
            raise MessageError("fields의 항목은 label과 value를 가진 객체입니다.")
        message.fields.append(
            {
                "label": _string(item.get("label"), "label", MAX_FIELD_LABEL, required=True),
                "value": _string(item.get("value"), "value", MAX_FIELD_VALUE),
            }
        )
    detail = raw.get("detail")
    if detail is not None:
        if not detail_allowed:
            raise MessageError("이 화면에는 상세 화면이 없습니다.")
        if not isinstance(detail, Mapping) or not 0 < len(detail) <= MAX_PARAMS:
            raise MessageError(f"detail은 {MAX_PARAMS}개까지의 값을 가진 객체입니다.")
        message.detail = {}
        for key, value in detail.items():
            if not PARAM_KEY.match(str(key)):
                raise MessageError(f"detail의 이름이 맞지 않습니다: {key}")
            message.detail[str(key)] = _string(value, key, MAX_PARAM_VALUE, required=True)
    link = raw.get("link")
    if link is not None:
        if not isinstance(link, Mapping):
            raise MessageError("link는 label과 url을 가진 객체입니다.")
        url = _string(link.get("url"), "link.url", 500, required=True)
        if urlsplit(url).scheme not in ("http", "https") or not host_allowed(url):
            raise MessageError("link.url은 허용된 호스트의 http(s) 주소여야 합니다.")
        message.link_url = url
        message.link_label = _string(link.get("label"), "link.label", MAX_LABEL) or "열기"
    reply_to = raw.get("reply_to")
    if reply_to is not None:
        if not reply_allowed:
            raise MessageError("여기서는 답글로 남길 수 없습니다.")
        if isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to <= 0:
            raise MessageError("reply_to는 글 번호입니다.")
        message.reply_to = reply_to
    return message


MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~.-])")


def search_text(message: MessageInput) -> str:
    """검색과 고정 목록 요약에 쓰는 본문. 마크다운으로 해석되지 않게 특수 문자를 막는다."""
    parts = [message.text] + [
        f"{item['label']}: {item['value']}" for item in message.fields if item["value"]
    ]
    return MARKDOWN_SPECIAL.sub(r"\\\1", " · ".join(" ".join(part.split()) for part in parts))


# 서버 훅


def webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    """서명 = HMAC-SHA256(키, "{시각}.{본문}")의 16진수. 시각을 묶어 지난 요청의 재전송을 막는다."""
    message = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, timestamp: str, signature: str, body: bytes, now: float) -> bool:
    if not secret or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > WEBHOOK_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(webhook_signature(secret, timestamp, body), signature)


# 화면에 보일 메시지


@dataclass
class WorkView:
    message: WorkMessage
    command_name: str
    command_label: str
    # 명령어가 살아 있고 화면에 상세가 있을 때만 상세를 연다.
    can_detail: bool

    @property
    def author_name(self) -> str | None:
        if not self.message.actor_name:
            return None
        return f"{EXTERNAL_AUTHOR} · {self.message.actor_name}"


async def work_views(session: AsyncSession, post_ids: Sequence[int]) -> dict[int, WorkView]:
    rows = await session.execute(
        select(WorkMessage, WorkCommand)
        .join(WorkCommand, WorkCommand.id == WorkMessage.command_id)
        .where(WorkMessage.post_id.in_(post_ids))
    )
    views = {}
    for message, command in rows:
        screen = screen_for(command)
        views[message.post_id] = WorkView(
            message,
            command.name,
            command.label,
            can_detail=message.detail_params is not None
            and command.archived_at is None
            and screen is not None
            and screen.detail is not None,
        )
    return views


async def active_commands(session: AsyncSession, channel_id: int) -> list[WorkCommand]:
    return list(
        await session.scalars(
            select(WorkCommand)
            .where(WorkCommand.channel_id == channel_id, WorkCommand.archived_at.is_(None))
            .order_by(WorkCommand.name)
        )
    )


def frame_url(command: WorkCommand) -> str:
    return f"/works/{command.id}/pages/{command.page}"


def frame_size(command: WorkCommand) -> str:
    screen = screen_for(command)
    return screen.size if screen else "medium"


def json_object(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None

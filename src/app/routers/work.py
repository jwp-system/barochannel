"""업무 채널(v3). 명령어로 업무 화면을 열고, 화면이 부르는 외부 API를 중계하고,
사용자가 "남기고 닫기"를 고르면 메시지를 채널에 새 글로 남긴다.
명령어 관리와 서버 훅도 여기 있다."""

import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionDep
from app.core.deps import AuthzDep, CurrentUser, current_user
from app.core.security import csrf_token
from app.core.templating import fragment, templates
from app.models import Channel, Post, User, WorkCommand, WorkMessage
from app.routers.posts import channel_access, post_context
from app.routers.spaces import REFRESH, managed_channel
from app.services.audit import record_audit
from app.services.permissions import Authorizer, ChannelAccess, can_write_reply
from app.services.realtime import Event, publish
from app.services.work import (
    API_METHODS,
    COMMAND_NAME,
    EVENT_ID,
    IDEMPOTENCY_KEY,
    MAX_LABEL,
    MAX_QUERY,
    MAX_REQUEST_BODY,
    MessageError,
    MessageInput,
    Screen,
    active_commands,
    api_parts,
    check_endpoint,
    frame_size,
    frame_url,
    json_object,
    limiter,
    parse_message,
    relay,
    screen_for,
    screens,
    search_text,
    user_headers,
    verify_webhook,
)

router = APIRouter(tags=["work"], include_in_schema=False, dependencies=[Depends(current_user)])
# 외부 시스템이 부르는 주소. 로그인 세션 대신 서명으로 인증한다.
hook_router = APIRouter(tags=["work"], include_in_schema=False)
MAX_WEBHOOK_BODY = 10_000
PEOPLE_LIMIT = 20

# 업무 화면에만 거는 보안 정책. 인라인 스크립트를 막고, 외부 주소를 직접 부르지 못하게 한다.
SCREEN_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'"
)


def json_error(code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, code)


@router.get("/channels/{channel_id}/commands")
async def list_commands(
    channel_id: int, session: SessionDep, authz: AuthzDep
) -> list[dict[str, str]]:
    """입력창 자동완성. 쓰기 권한이 없으면 빈 목록이다. 화면이 없어진 명령어는 빼고 보여준다."""
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "work" or not access.write:
        return []
    return [
        {
            "name": row.name,
            "label": row.label,
            "description": row.description,
            "url": frame_url(row),
            "size": frame_size(row),
        }
        for row in await active_commands(session, channel.id)
        if screen_for(row) is not None
    ]


async def usable_command(
    session: AsyncSession, authz: Authorizer, command_id: int
) -> tuple[WorkCommand, Screen, Channel, ChannelAccess]:
    """보관되지 않은, 화면이 있는 명령어와 그 채널. 들어갈 수 없는 채널이면 404다."""
    command = await session.get(WorkCommand, command_id)
    if command is None or command.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "명령어를 찾을 수 없습니다.")
    channel, access = await channel_access(session, authz, command.channel_id)
    screen = screen_for(command)
    if channel.type != "work" or screen is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "명령어를 찾을 수 없습니다.")
    return command, screen, channel, access


def screen_page(
    request: Request,
    command: WorkCommand,
    screen: Screen,
    channel: Channel,
    user: User,
    page: str,
    *,
    can_write: bool,
    can_leave: bool,
    params: dict[str, str] | None = None,
    reply_to: int | None = None,
) -> HTMLResponse:
    context: dict[str, Any] = {
        "user": {"id": user.id, "name": user.name, "email": user.email},
        "channel": {"id": channel.id, "name": channel.name},
        "command": {"id": command.id, "name": command.name, "label": command.label},
        "screen": screen.name,
        "page": page,
        "params": params or {},
        "reply_to": reply_to,
        # 이 팝업에서 남기는 메시지와 idempotent 호출이 함께 쓰는 키.
        "key": uuid4().hex,
        "csrf": csrf_token(request),
        "api": f"/works/{command.id}/api/",
        "messages": f"/works/{command.id}/messages",
        "people": f"/works/{command.id}/people",
        # 화면은 이 값으로 처리 버튼과 [남기고 닫기]를 감춘다. 서버도 다시 확인한다.
        "can_write": can_write,
        "can_leave": can_leave,
    }
    response = templates.TemplateResponse(
        request,
        f"works/{screen.name}/{page}.html",
        {"baro": context, "screen": screen, "command": command, "params": params or {}},
    )
    response.headers["Content-Security-Policy"] = SCREEN_CSP
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/works/{command_id}/pages/{page}", response_class=HTMLResponse)
async def open_page(
    request: Request, command_id: int, page: str, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    command, screen, channel, access = await usable_command(session, authz, command_id)
    if not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "이 채널에서 명령어를 실행할 수 없습니다.")
    if page not in screen.pages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "화면을 찾을 수 없습니다.")
    return screen_page(
        request, command, screen, channel, authz.user, page, can_write=True, can_leave=True
    )


@router.get("/works/messages/{post_id}/detail", response_class=HTMLResponse)
async def open_detail(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    """메시지의 상세 화면. 그 메시지를 볼 수 있는 사람만 연다.

    여기서 남기면 그 쓰레드에 답글이 된다."""
    post, parent, channel, access = await post_context(session, authz, post_id)
    message = await session.get(WorkMessage, post.id)
    if message is None or message.detail_params is None or post.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "상세 화면이 없는 글입니다.")
    command, screen, channel, access = await usable_command(session, authz, message.command_id)
    if screen.detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "상세 화면이 없는 글입니다.")
    root = parent or post
    return screen_page(
        request,
        command,
        screen,
        channel,
        authz.user,
        screen.detail,
        can_write=access.write,
        can_leave=can_write_reply(access, root, authz.user.id) and root.deleted_at is None,
        params=message.detail_params,
        reply_to=root.id,
    )


@router.api_route("/works/{command_id}/api/{path:path}", methods=list(API_METHODS))
async def relay_api(
    request: Request, command_id: int, path: str, session: SessionDep, authz: AuthzDep
) -> Response:
    """화면의 API 호출을 연동 주소로 중계한다.

    읽기(GET)는 채널 읽기, 나머지는 채널 쓰기 권한이 있어야 한다."""
    command, screen, channel, access = await usable_command(session, authz, command_id)
    method = request.method
    if not (access.read if method == "GET" else access.write):
        return json_error(status.HTTP_403_FORBIDDEN, "이 업무를 쓸 권한이 없습니다.")
    parts = api_parts(path)
    if parts is None or not screen.allows(method, parts):
        return json_error(status.HTTP_404_NOT_FOUND, "화면에 등록되지 않은 API입니다.")
    query = request.url.query
    if len(query) > MAX_QUERY:
        return json_error(status.HTTP_414_URI_TOO_LONG, "조회 조건이 너무 깁니다.")
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY:
        return json_error(status.HTTP_413_CONTENT_TOO_LARGE, "보낼 내용이 너무 큽니다.")
    if body and json_object(body) is None:
        return json_error(status.HTTP_400_BAD_REQUEST, "본문은 JSON이어야 합니다.")
    if not limiter.allow(authz.user.id):
        return json_error(status.HTTP_429_TOO_MANY_REQUESTS, "잠시 뒤에 다시 해 주세요.")
    headers = user_headers(authz.user, channel, command)
    key = request.headers.get("Idempotency-Key", "")
    if IDEMPOTENCY_KEY.match(key):
        headers["Idempotency-Key"] = key
    result = await relay(command, method, parts, query, body, headers)
    if method != "GET":
        record_audit(
            session,
            actor_id=authz.user.id,
            action="work.api",
            target_type="channel",
            target_id=channel.id,
            command=command.name,
            method=method,
            path="/" + "/".join(parts),
            status=result.status,
        )
        await session.commit()
    return Response(
        result.content,
        result.status,
        media_type="application/json" if result.content else None,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/works/{command_id}/people")
async def people(
    command_id: int,
    session: SessionDep,
    authz: AuthzDep,
    q: Annotated[str, Query(max_length=100)] = "",
) -> list[dict[str, object]]:
    """담당자 고르기. 화면은 바로채널 사용자 중에서 이름이나 이메일로 찾는다."""
    await usable_command(session, authz, command_id)
    if not q.strip():
        return []
    users = await session.scalars(
        select(User)
        .where(
            User.status == "active",
            or_(
                User.name.contains(q.strip(), autoescape=True),
                User.email.contains(q.strip().lower(), autoescape=True),
            ),
        )
        .order_by(User.name, User.id)
        .limit(PEOPLE_LIMIT)
    )
    return [{"id": row.id, "name": row.name, "email": row.email} for row in users]


async def existing_message(session: AsyncSession, command_id: int, key: str) -> Post | None:
    return await session.scalar(
        select(Post)
        .join(WorkMessage, WorkMessage.post_id == Post.id)
        .where(WorkMessage.command_id == command_id, WorkMessage.idempotency_key == key)
    )


def posted(post: Post, code: int = status.HTTP_201_CREATED) -> JSONResponse:
    return JSONResponse({"post_id": post.id, "url": f"/posts/{post.id}"}, code)


async def add_message(
    session: AsyncSession,
    command: WorkCommand,
    channel: Channel,
    author_id: int,
    message: MessageInput,
    key: str,
    parent: Post | None,
    actor_name: str = "",
) -> Post:
    """메시지를 새 글로 남긴다. 고치는 길은 없다. 커밋은 부르는 쪽이 한다."""
    post = Post(
        channel_id=channel.id,
        parent_post_id=parent.id if parent else None,
        author_id=author_id,
        body_md=search_text(message),
    )
    session.add(post)
    await session.flush()
    session.add(
        WorkMessage(
            post_id=post.id,
            command_id=command.id,
            text=message.text,
            fields=message.fields,
            detail_params=message.detail,
            link_label=message.link_label,
            link_url=message.link_url,
            actor_name=actor_name,
            idempotency_key=key,
        )
    )
    await publish(session, Event("created", channel.id, post.id, author_id))
    return post


@router.post("/works/{command_id}/messages")
async def leave_message(
    request: Request, command_id: int, session: SessionDep, authz: AuthzDep
) -> JSONResponse:
    """남기고 닫기. 같은 Idempotency-Key로 다시 오면 처음 남긴 글을 돌려준다."""
    command, screen, channel, access = await usable_command(session, authz, command_id)
    key = request.headers.get("Idempotency-Key", "")
    if not IDEMPOTENCY_KEY.match(key):
        return json_error(status.HTTP_400_BAD_REQUEST, "화면을 다시 열어 주세요.")
    if (post := await existing_message(session, command.id, key)) is not None:
        return posted(post, status.HTTP_200_OK)
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY:
        return json_error(status.HTTP_413_CONTENT_TOO_LARGE, "메시지가 너무 큽니다.")
    try:
        message = parse_message(
            json_object(body), detail_allowed=screen.detail is not None, reply_allowed=True
        )
    except MessageError as error:
        return json_error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))
    parent = None
    if message.reply_to is not None:
        parent, grandparent, parent_channel, parent_access = await post_context(
            session, authz, message.reply_to, lock=True
        )
        if grandparent is not None or parent_channel.id != channel.id:
            return json_error(
                status.HTTP_400_BAD_REQUEST, "이 채널의 글에만 답글로 남길 수 있습니다."
            )
        if parent.deleted_at is not None:
            return json_error(status.HTTP_409_CONFLICT, "삭제된 글에는 남길 수 없습니다.")
        if not can_write_reply(parent_access, parent, authz.user.id):
            return json_error(status.HTTP_403_FORBIDDEN, "답글쓰기 권한이 없습니다.")
    elif not access.write:
        return json_error(status.HTTP_403_FORBIDDEN, "이 채널에 글을 남길 권한이 없습니다.")
    post = await add_message(session, command, channel, authz.user.id, message, key, parent)
    record_audit(
        session,
        actor_id=authz.user.id,
        action="work.message",
        target_type="post",
        target_id=post.id,
        channel_id=channel.id,
        command=command.name,
        reply_to=message.reply_to,
    )
    try:
        await session.commit()
    except IntegrityError:
        # 같은 키가 동시에 들어왔다. 먼저 남은 쪽을 돌려준다.
        await session.rollback()
        post = await existing_message(session, command.id, key)
        if post is None:
            raise
        return posted(post, status.HTTP_200_OK)
    return posted(post)


# 서버 훅. 외부 시스템이 직접 메시지를 남긴다. 명령어마다 키를 발급해야 켜진다.


def rejected() -> JSONResponse:
    # 명령어가 없는지, 키가 틀렸는지 구분해 알리지 않는다.
    return JSONResponse({"detail": "서명이 맞지 않습니다."}, status.HTTP_401_UNAUTHORIZED)


@hook_router.post("/hooks/work/{command_id}")
async def receive_event(command_id: int, request: Request, session: SessionDep) -> JSONResponse:
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY:
        return JSONResponse({"detail": "본문이 너무 깁니다."}, status.HTTP_413_CONTENT_TOO_LARGE)
    command = await session.get(WorkCommand, command_id)
    if command is None or not verify_webhook(
        command.webhook_secret,
        request.headers.get("X-Baro-Timestamp", ""),
        request.headers.get("X-Baro-Signature", ""),
        body,
        time.time(),
    ):
        return rejected()
    if command.archived_at is not None:
        return JSONResponse({"detail": "보관된 명령어입니다."}, status.HTTP_409_CONFLICT)
    event_id = request.headers.get("X-Baro-Event-Id", "")
    if not EVENT_ID.match(event_id):
        return JSONResponse(
            {"detail": "X-Baro-Event-Id가 필요합니다."}, status.HTTP_400_BAD_REQUEST
        )
    key = f"hook:{event_id}"
    if (post := await existing_message(session, command.id, key)) is not None:
        return JSONResponse({"post_id": post.id, "duplicate": True}, status.HTTP_202_ACCEPTED)
    payload = json_object(body)
    screen = screen_for(command)
    try:
        message = parse_message(
            payload,
            detail_allowed=screen is not None and screen.detail is not None,
            reply_allowed=False,
        )
    except MessageError as error:
        return JSONResponse({"detail": str(error)}, status.HTTP_422_UNPROCESSABLE_CONTENT)
    actor = payload.get("actor") if isinstance(payload, dict) else None
    actor_name = " ".join(str(actor or "").split())[:MAX_LABEL]
    channel = await session.get(Channel, command.channel_id)
    post = await add_message(
        session,
        command,
        channel,
        command.created_by,
        message,
        key,
        None,
        actor_name=actor_name or "알 수 없음",
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        post = await existing_message(session, command.id, key)
        if post is None:
            raise
        return JSONResponse({"post_id": post.id, "duplicate": True}, status.HTTP_202_ACCEPTED)
    return JSONResponse({"post_id": post.id}, status.HTTP_202_ACCEPTED)


@router.post("/work-commands/{command_id}/webhook", response_class=HTMLResponse)
async def issue_webhook(
    command_id: int, request: Request, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    """새 키를 만들어 한 번만 보여준다. 다시 발급하면 이전 키는 바로 못 쓴다."""
    command = await managed_command(session, command_id, user)
    replaced = bool(command.webhook_secret)
    command.webhook_secret = secrets.token_hex(32)
    record_audit(
        session,
        actor_id=user.id,
        action="work.webhook_issue",
        target_type="channel",
        target_id=command.channel_id,
        command=command.name,
        replaced=replaced,
    )
    await session.commit()
    return fragment(
        "partials/work_webhook.html",
        command=command,
        url=str(request.url_for("receive_event", command_id=command.id)),
    )


@router.post("/work-commands/{command_id}/webhook/delete")
async def revoke_webhook(command_id: int, session: SessionDep, user: CurrentUser) -> Response:
    command = await managed_command(session, command_id, user)
    command.webhook_secret = ""
    record_audit(
        session,
        actor_id=user.id,
        action="work.webhook_revoke",
        target_type="channel",
        target_id=command.channel_id,
        command=command.name,
    )
    await session.commit()
    return Response(headers=REFRESH)


# 명령어 관리. 그룹 관리자만 한다.

Label = Annotated[str, Form(min_length=1, max_length=100)]
Description = Annotated[str, Form(max_length=300)]
Endpoint = Annotated[str, Form(min_length=1, max_length=500)]
Token = Annotated[str, Form(max_length=200)]
Target = Annotated[str, Form(min_length=3, max_length=81)]


def chosen_screen(target: str) -> tuple[str, str]:
    """ "화면/시작 화면" 값을 검사한다."""
    name, _, page = target.partition("/")
    screen = screens().get(name)
    if screen is None or page not in screen.pages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "화면을 목록에서 골라 주세요.")
    return name, page


def checked_endpoint(url: str) -> str:
    try:
        return check_endpoint(url)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error


@router.post("/channels/{channel_id}/work-commands")
async def create_command(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    name: Annotated[str, Form(min_length=1, max_length=31)],
    label: Label,
    target: Target,
    endpoint_url: Endpoint,
    description: Description = "",
    api_token: Token = "",
) -> Response:
    channel = await managed_channel(session, channel_id, user)
    if channel.type != "work":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "업무 채널에만 명령어를 둘 수 있습니다.")
    name = name.strip().removeprefix("/").lower()
    if not COMMAND_NAME.match(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "명령어 이름은 영문 소문자, 숫자, 한글, -, _로 30자까지 쓸 수 있습니다.",
        )
    screen, page = chosen_screen(target)
    endpoint = checked_endpoint(endpoint_url)
    taken = await session.scalar(
        select(WorkCommand.id).where(WorkCommand.channel_id == channel.id, WorkCommand.name == name)
    )
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"이미 있는 명령어입니다: /{name}")
    command = WorkCommand(
        channel_id=channel.id,
        name=name,
        label=label.strip(),
        description=description.strip(),
        screen=screen,
        page=page,
        endpoint_url=endpoint,
        api_token=api_token.strip(),
        created_by=user.id,
    )
    session.add(command)
    await session.flush()
    record_audit(
        session,
        actor_id=user.id,
        action="work.command_create",
        target_type="channel",
        target_id=channel.id,
        command=name,
        screen=f"{screen}/{page}",
        label=command.label,
        endpoint_url=command.endpoint_url,
        has_token=bool(command.api_token),
    )
    try:
        await session.commit()
    except IntegrityError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, f"이미 있는 명령어입니다: /{name}") from error
    return Response(headers=REFRESH)


async def managed_command(session: AsyncSession, command_id: int, user: CurrentUser) -> WorkCommand:
    command = await session.get(WorkCommand, command_id, with_for_update=True)
    if command is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "명령어를 찾을 수 없습니다.")
    await managed_channel(session, command.channel_id, user)
    return command


def command_settings(command: WorkCommand) -> dict[str, str]:
    return {
        "label": command.label,
        "description": command.description,
        "screen": f"{command.screen}/{command.page}",
        "endpoint_url": command.endpoint_url,
    }


@router.post("/work-commands/{command_id}")
async def update_command(
    command_id: int,
    session: SessionDep,
    user: CurrentUser,
    label: Label,
    target: Target,
    endpoint_url: Endpoint,
    description: Description = "",
    api_token: Token = "",
) -> Response:
    """토큰은 비워 두면 그대로 둔다."""
    command = await managed_command(session, command_id, user)
    before = command_settings(command)
    command.screen, command.page = chosen_screen(target)
    command.endpoint_url = checked_endpoint(endpoint_url)
    command.label = label.strip()
    command.description = description.strip()
    if api_token.strip():
        command.api_token = api_token.strip()
    record_audit(
        session,
        actor_id=user.id,
        action="work.command_update",
        target_type="channel",
        target_id=command.channel_id,
        command=command.name,
        before=before,
        after=command_settings(command),
        token_changed=bool(api_token.strip()),
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/work-commands/{command_id}/archive")
async def toggle_archive(command_id: int, session: SessionDep, user: CurrentUser) -> Response:
    command = await managed_command(session, command_id, user)
    archived = command.archived_at is None
    command.archived_at = datetime.now(UTC) if archived else None
    record_audit(
        session,
        actor_id=user.id,
        action="work.command_archive" if archived else "work.command_restore",
        target_type="channel",
        target_id=command.channel_id,
        command=command.name,
    )
    await session.commit()
    return Response(headers=REFRESH)

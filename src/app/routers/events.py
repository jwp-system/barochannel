import asyncio
from collections.abc import AsyncGenerator, Awaitable

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.core.deps import LoginRequired
from app.services.delivery import messages_for, still_allowed, todo_count
from app.services.realtime import hub

router = APIRouter(tags=["events"], include_in_schema=False)

HEARTBEAT_SECONDS = 15


async def uninterrupted[T](work: Awaitable[T]) -> T:
    """탭을 닫아 연결이 끊겨도 진행 중인 짧은 조회는 끝낸다. DB 연결이 강제로 닫히지 않는다."""
    with CancelScope(shield=True):
        return await work


def format_event(name: str, html: str) -> str:
    data = "\n".join(f"data: {line}" for line in html.splitlines())
    return f"event: {name}\n{data}\n\n"


async def stream(user_id: int, channel_id: int) -> AsyncGenerator[str]:
    queue = hub.subscribe(channel_id)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                # 로드밸런서가 연결을 끊지 않게 하고, 권한 회수도 이때 다시 확인한다.
                if not await uninterrupted(still_allowed(user_id, channel_id)):
                    return
                yield ": ping\n\n"
                continue
            if event is None:
                return
            messages = await uninterrupted(messages_for(event, user_id))
            if messages is None:
                return
            for name, html in messages:
                yield format_event(name, html)
    finally:
        hub.unsubscribe(channel_id, queue)


async def count_stream(user_id: int) -> AsyncGenerator[str]:
    """내 할 일 개수만 보낸다. 접속하면 바로 한 번 보내고, 바뀌면 다시 보낸다."""
    queue = hub.subscribe_user(user_id)
    sent: int | None = None
    try:
        while True:
            count = await uninterrupted(todo_count(user_id))
            if count is None:
                return
            if count != sent:
                sent = count
                yield format_event("counts", str(count))
            else:
                yield ": ping\n\n"
            try:
                if await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS) is None:
                    return
            except TimeoutError:
                # 권한이 바뀌어도 개수가 달라지므로 신호가 없어도 다시 센다.
                continue
    finally:
        hub.unsubscribe_user(user_id, queue)


@router.get("/events/me")
async def my_events(request: Request) -> StreamingResponse:
    user_id = request.session.get("user_id")
    if not user_id:
        raise LoginRequired
    return StreamingResponse(
        count_stream(user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/{channel_id}")
async def events(channel_id: int, request: Request) -> StreamingResponse:
    """응답이 오래 유지되므로 DB 세션 의존성을 쓰지 않고 필요할 때만 연결을 빌린다."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise LoginRequired
    if not await still_allowed(user_id, channel_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "채널을 찾을 수 없습니다.")
    return StreamingResponse(
        stream(user_id, channel_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

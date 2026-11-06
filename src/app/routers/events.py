import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.core.deps import LoginRequired
from app.services.delivery import messages_for, still_allowed
from app.services.realtime import hub

router = APIRouter(tags=["events"], include_in_schema=False)

HEARTBEAT_SECONDS = 15


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
                if not await still_allowed(user_id, channel_id):
                    return
                yield ": ping\n\n"
                continue
            if event is None:
                return
            messages = await messages_for(event, user_id)
            if messages is None:
                return
            for name, html in messages:
                yield format_event(name, html)
    finally:
        hub.unsubscribe(channel_id, queue)


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

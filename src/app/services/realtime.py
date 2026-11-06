"""서버 간 실시간 전달. PostgreSQL NOTIFY로 받아 이 프로세스의 SSE 연결에 나눠 준다.

NOTIFY에는 번호만 싣고 본문은 싣지 않는다. 내용은 연결마다 권한을 판정한 뒤 만든다.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

NOTIFY_CHANNEL = "baro_events"
QUEUE_SIZE = 100
RECONNECT_SECONDS = 3

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    kind: str  # created, updated, pins
    channel_id: int
    post_id: int
    actor_id: int


async def publish(session: AsyncSession, event: Event) -> None:
    """NOTIFY는 트랜잭션이 커밋될 때 전달되므로 롤백된 변경은 알려지지 않는다."""
    await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, json.dumps(asdict(event)))))


class Hub:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[Event | None]]] = {}
        self._task: asyncio.Task[None] | None = None

    def subscribe(self, channel_id: int) -> asyncio.Queue[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(QUEUE_SIZE)
        self._subscribers.setdefault(channel_id, set()).add(queue)
        return queue

    def unsubscribe(self, channel_id: int, queue: asyncio.Queue[Event | None]) -> None:
        subscribers = self._subscribers.get(channel_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                del self._subscribers[channel_id]

    def dispatch(self, event: Event) -> None:
        for queue in list(self._subscribers.get(event.channel_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 따라오지 못하는 연결은 끊는다. 브라우저가 다시 접속한다.
                self.unsubscribe(event.channel_id, queue)
                queue.get_nowait()
                queue.put_nowait(None)

    def _on_notify(self, _connection: object, _pid: int, _channel: str, payload: str) -> None:
        self.dispatch(Event(**json.loads(payload)))

    async def _listen(self) -> None:
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(dsn)
                await connection.add_listener(NOTIFY_CHANNEL, self._on_notify)
                while not connection.is_closed():
                    await asyncio.sleep(RECONNECT_SECONDS)
            except (OSError, asyncpg.PostgresError) as error:
                logger.warning("실시간 수신 연결 실패, 다시 시도합니다: %s", error)
            finally:
                if connection is not None and not connection.is_closed():
                    await connection.close()
            await asyncio.sleep(RECONNECT_SECONDS)

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        for queues in self._subscribers.values():
            for queue in queues:
                queue.put_nowait(None)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


hub = Hub()

"""서버 간 실시간 전달. PostgreSQL NOTIFY로 받아 이 프로세스의 SSE 연결에 나눠 준다.

NOTIFY에는 번호만 싣고 본문은 싣지 않는다. 내용은 연결마다 권한을 판정한 뒤 만든다.
"""

import asyncio
import json
import logging
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

NOTIFY_CHANNEL = "baro_events"
QUEUE_SIZE = 100
RECONNECT_SECONDS = 3
# NOTIFY 내용은 8000바이트를 넘을 수 없다.
COUNT_CHUNK = 500

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


async def publish_counts(session: AsyncSession, user_ids: Iterable[int]) -> None:
    """개수가 바뀐 사람을 알린다. 개수는 받는 쪽이 다시 센다."""
    ids = sorted(user_ids)
    for start in range(0, len(ids), COUNT_CHUNK):
        payload = json.dumps({"user_ids": ids[start : start + COUNT_CHUNK]})
        await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, payload)))


class Hub:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[Event | None]]] = {}
        self._user_subscribers: dict[int, set[asyncio.Queue[bool | None]]] = {}
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

    def subscribe_user(self, user_id: int) -> asyncio.Queue[bool | None]:
        # 신호는 쌓을 필요가 없다. 한 칸이면 다음에 한 번 다시 세는 것으로 충분하다.
        queue: asyncio.Queue[bool | None] = asyncio.Queue(1)
        self._user_subscribers.setdefault(user_id, set()).add(queue)
        return queue

    def unsubscribe_user(self, user_id: int, queue: asyncio.Queue[bool | None]) -> None:
        subscribers = self._user_subscribers.get(user_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                del self._user_subscribers[user_id]

    def dispatch_counts(self, user_ids: Iterable[int]) -> None:
        for user_id in user_ids:
            for queue in list(self._user_subscribers.get(user_id, ())):
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(True)

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
        data = json.loads(payload)
        if "user_ids" in data:
            self.dispatch_counts(data["user_ids"])
        else:
            self.dispatch(Event(**data))

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
        for user_queues in self._user_subscribers.values():
            for user_queue in user_queues:
                with suppress(asyncio.QueueEmpty):
                    user_queue.get_nowait()
                user_queue.put_nowait(None)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


hub = Hub()

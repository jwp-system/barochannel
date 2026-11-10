"""샘플 할 일 시스템. 바로채널 업무 채널(v3)의 연동을 검증하려고 만든 별도 서버다.

바로채널 전용 규격은 없다. 평범한 JSON API이고, 바로채널의 업무 화면(templates/works/todo)이
바로채널의 중계를 거쳐 부른다. 중계는 요청한 사람을 X-Baro-User-* 머리글로 알려준다
(이름은 퍼센트 인코딩).

    GET   /todos/options              고를 수 있는 값(우선순위, 상태). 화면이 목록을 그릴 때 쓴다
    GET   /todos?q=&status=&assignee= 할 일 찾기 {"items": [...], "total": n}. 최근 20개
    POST  /todos                      할 일 등록. Idempotency-Key가 같으면 처음 결과를 돌려준다
    GET   /todos/{id}                 할 일 하나. 지금 할 수 있는 동작(actions)을 싣는다
    POST  /todos/{id}/actions/{key}   동작(start, done, reopen)
    PATCH /todos/{id}                 이 시스템 사용자의 상태 변경
                                      {"status": "todo"|"doing"|"done", "actor": "이름"}

서버 훅(이 시스템이 바로채널에 메시지를 남긴다)
    PATCH로 상태를 바꾸면 TODO_WEBHOOK_URL로 메시지를 보낸다. TODO_WEBHOOK_SECRET으로 서명한다.
    둘 다 바로채널 채널 설정의 "훅 키 발급"에서 받는다.

모든 요청에 Authorization: Bearer {TODO_API_TOKEN}이 필요하다.

    uv run samples/todo_api.py
    uv run uvicorn --app-dir samples todo_api:app --port 8100
"""

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

LOCAL_TZ = ZoneInfo("Asia/Seoul")
STATUS_LABELS = {"todo": "할 일", "doing": "진행 중", "done": "완료"}
PRIORITIES = ("낮음", "보통", "높음")
# 동작 키: (버튼 이름, 바뀔 상태, 확인 문구)
ACTIONS = {
    "start": ("시작", "doing", ""),
    "done": ("완료", "done", "완료로 바꿀까요?"),
    "reopen": ("다시 열기", "todo", ""),
}
# 상태마다 지금 할 수 있는 동작.
ALLOWED_ACTIONS = {"todo": ("start", "done"), "doing": ("done", "reopen"), "done": ("reopen",)}
SEARCH_LIMIT = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS todo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL DEFAULT '',
    due TEXT,
    priority TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class StatusChange(BaseModel):
    status: Literal["todo", "doing", "done"]
    actor: str = ""


Notifier = Callable[[bytes, dict[str, str]], None]


def signed(secret: str, body: bytes, event_id: str) -> dict[str, str]:
    """바로채널 서버 훅 서명: HMAC-SHA256(키, "{시각}.{본문}")."""
    timestamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return {
        "Content-Type": "application/json",
        "X-Baro-Timestamp": timestamp,
        "X-Baro-Signature": "sha256=" + digest.hexdigest(),
        "X-Baro-Event-Id": event_id,
    }


def post_in_background(url: str) -> Notifier:
    """응답을 기다리지 않는다. 사용자의 상태 변경이 바로채널 때문에 느려지지 않게 한다."""

    def send(body: bytes, headers: dict[str, str]) -> None:
        def run() -> None:
            request = urllib.request.Request(url, body, headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    print(f"훅 {response.status}: {body.decode()}")
            except OSError as error:
                print(f"훅 실패: {error}")

        threading.Thread(target=run, daemon=True).start()

    return send


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _local(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def todo_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "description": row["description"],
        "assignee": row["assignee"],
        "due": row["due"] or "",
        "priority": row["priority"],
        "status": row["status"],
        "status_label": STATUS_LABELS[row["status"]],
        "actions": [
            {"key": key, "label": ACTIONS[key][0], "confirm": ACTIONS[key][2]}
            for key in ALLOWED_ACTIONS[row["status"]]
        ],
        "created_by": row["created_by"],
        "updated_by": row["updated_by"],
        "channel": row["channel"],
        "created_at": _local(row["created_at"]),
        "updated_at": _local(row["updated_at"]),
    }


def validate(data: object) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(data, dict):
        return {}, {"title": "입력값 형식이 맞지 않습니다."}
    errors: dict[str, str] = {}
    title = str(data.get("title") or "").strip()
    if not title:
        errors["title"] = "제목을 입력해 주세요."
    elif len(title) > 200:
        errors["title"] = "제목은 200자까지입니다."
    priority = data.get("priority") or "보통"
    if priority not in PRIORITIES:
        errors["priority"] = "우선순위를 목록에서 골라 주세요."
    due = data.get("due")
    if due:
        try:
            if date.fromisoformat(str(due)) < datetime.now(LOCAL_TZ).date():
                errors["due"] = "마감일은 오늘 이후여야 합니다."
        except ValueError:
            errors["due"] = "날짜를 확인해 주세요."
    clean = {
        "title": title,
        "description": str(data.get("description") or "").strip()[:2000],
        "assignee": str(data.get("assignee") or "").strip()[:50],
        "due": str(due) if due else None,
        "priority": priority,
        "channel": str(data.get("channel") or "").strip()[:100],
    }
    return clean, errors


def requester(name: str | None) -> str:
    """바로채널 중계가 보낸 요청자 이름. 퍼센트 인코딩돼 있다."""
    return unquote(name or "")[:50]


def create_app(
    db_path: str, token: str, webhook_secret: str = "", notify: Notifier | None = None
) -> FastAPI:
    """notify를 주면 이 시스템 사용자의 상태 변경을 바로채널에 메시지로 남긴다."""
    application = FastAPI(title="샘플 할 일 시스템")
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(todo)")}
    if "updated_by" not in columns:  # 예전 판에서 만든 파일
        connection.execute("ALTER TABLE todo ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''")
    connection.commit()
    lock = threading.Lock()

    def authorize(authorization: Annotated[str, Header()] = "") -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "연동 토큰이 맞지 않습니다.")

    def load(todo_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM todo WHERE id = ?", (todo_id,)).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "할 일을 찾을 수 없습니다.")
        return row

    guarded = [Depends(authorize)]
    UserName = Annotated[str | None, Header(alias="X-Baro-User-Name")]

    @application.get("/todos/options", dependencies=guarded)
    def options() -> dict[str, Any]:
        return {
            "priorities": list(PRIORITIES),
            "statuses": [{"value": code, "label": label} for code, label in STATUS_LABELS.items()],
        }

    @application.get("/todos", dependencies=guarded)
    def todos(
        q: str = "",
        status_code: Annotated[str, Query(alias="status")] = "",
        assignee: str = "",
    ) -> JSONResponse:
        where, args = [], []
        if q.strip():
            where.append("(title LIKE ? OR description LIKE ?)")
            args += [f"%{q.strip()}%"] * 2
        if status_code:
            if status_code not in STATUS_LABELS:
                return JSONResponse(
                    {"errors": {"status": "상태를 목록에서 골라 주세요."}},
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                )
            where.append("status = ?")
            args.append(status_code)
        if assignee.strip():
            where.append("assignee LIKE ?")
            args.append(f"%{assignee.strip()}%")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = connection.execute(f"SELECT count(*) FROM todo{clause}", args).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM todo{clause} ORDER BY id DESC LIMIT {SEARCH_LIMIT}", args
        ).fetchall()
        return JSONResponse({"items": [todo_json(row) for row in rows], "total": total})

    @application.post("/todos", dependencies=guarded)
    async def create(
        request: Request,
        user_name: UserName = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"detail": "JSON 본문이 필요합니다."}, status.HTTP_400_BAD_REQUEST)
        with lock:
            if idempotency_key:
                row = connection.execute(
                    "SELECT * FROM todo WHERE request_id = ?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    return JSONResponse(todo_json(row), status.HTTP_200_OK)
            clean, errors = validate(payload)
            if errors:
                return JSONResponse({"errors": errors}, status.HTTP_422_UNPROCESSABLE_CONTENT)
            now = _now()
            cursor = connection.execute(
                "INSERT INTO todo (request_id, title, description, assignee, due, priority,"
                " created_by, channel, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    clean["title"],
                    clean["description"],
                    clean["assignee"],
                    clean["due"],
                    clean["priority"],
                    requester(user_name),
                    clean["channel"],
                    now,
                    now,
                ),
            )
            connection.commit()
            return JSONResponse(todo_json(load(cursor.lastrowid)), status.HTTP_201_CREATED)

    @application.get("/todos/{todo_id}", dependencies=guarded)
    def detail(todo_id: int) -> dict[str, Any]:
        return todo_json(load(todo_id))

    @application.post("/todos/{todo_id}/actions/{key}", dependencies=guarded)
    def act(todo_id: int, key: str, user_name: UserName = None) -> dict[str, Any]:
        with lock:
            row = load(todo_id)
            if key not in ALLOWED_ACTIONS[row["status"]]:
                raise HTTPException(status.HTTP_409_CONFLICT, "지금 상태에서는 할 수 없습니다.")
            connection.execute(
                "UPDATE todo SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                (ACTIONS[key][1], requester(user_name), _now(), todo_id),
            )
            connection.commit()
            return todo_json(load(todo_id))

    @application.patch("/todos/{todo_id}", dependencies=guarded)
    def change(todo_id: int, change: StatusChange) -> dict[str, Any]:
        """이 시스템 사용자의 상태 변경. 바뀌면 바로채널에 메시지를 남긴다."""
        with lock:
            row = load(todo_id)
            connection.execute(
                "UPDATE todo SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                (change.status, change.actor, _now(), todo_id),
            )
            connection.commit()
            after = load(todo_id)
        if notify and webhook_secret and change.status != row["status"]:
            before, now = STATUS_LABELS[row["status"]], STATUS_LABELS[change.status]
            message = {
                "text": f"{row['title']}: 할 일 시스템에서 상태를 바꿨습니다.",
                "fields": [
                    {"label": "상태", "value": f"{before} → {now}"},
                    {"label": "담당자", "value": row["assignee"]},
                ],
                "detail": {"id": str(todo_id)},
                "actor": change.actor,
            }
            body = json.dumps(message, ensure_ascii=False).encode()
            event_id = f"todo-{todo_id}-{time.time_ns()}"
            notify(body, signed(webhook_secret, body, event_id))
        return todo_json(after)

    return application


WEBHOOK_URL = os.environ.get("TODO_WEBHOOK_URL", "")
app = create_app(
    os.environ.get("TODO_DB", "todo.db"),
    os.environ.get("TODO_API_TOKEN", "dev-todo-token"),
    os.environ.get("TODO_WEBHOOK_SECRET", ""),
    post_in_background(WEBHOOK_URL) if WEBHOOK_URL else None,
)


def main() -> None:
    """주소와 포트는 TODO_HOST(기본 127.0.0.1), TODO_PORT(기본 8100)로 바꾼다."""
    uvicorn.run(
        app,
        host=os.environ.get("TODO_HOST", "127.0.0.1"),
        port=int(os.environ.get("TODO_PORT", "8100")),
    )


if __name__ == "__main__":
    main()

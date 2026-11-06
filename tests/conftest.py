import asyncio
import os
import re
from collections.abc import Awaitable, Iterator

os.environ.update(
    BARO_DATABASE_URL="postgresql+asyncpg://barochannel:barochannel@localhost:5433/barochannel_test",
    BARO_DB_NULL_POOL="true",
    BARO_DEBUG="true",
    BARO_S3_BUCKET="barochannel-test",
    BARO_ADMIN_EMAIL="admin@test.local",
    BARO_ADMIN_PASSWORD="test-password",
)

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.db import Base, SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Channel,
    Department,
    GroupManager,
    PermissionGrant,
    User,
    UserDepartment,
    WorkspaceGroup,
)
from app.services.admin import hash_pin  # noqa: E402
from app.services.storage import ensure_bucket  # noqa: E402

PASSWORD = "test-password"
ADMIN_PIN = "482913"
PASSWORD_HASH = hash_password(PASSWORD)
ADMIN_PIN_HASH = hash_pin(ADMIN_PIN)


def run[T](awaitable: Awaitable[T]) -> T:
    async def wrapper() -> T:
        return await awaitable

    return asyncio.run(wrapper())


async def _recreate_database() -> None:
    connection = await asyncpg.connect(
        "postgresql://barochannel:barochannel@localhost:5433/postgres"
    )
    await connection.execute("DROP DATABASE IF EXISTS barochannel_test WITH (FORCE)")
    await connection.execute("CREATE DATABASE barochannel_test")
    await connection.close()


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    run(_recreate_database())
    command.upgrade(Config("alembic.ini"), "head")
    run(ensure_bucket())


async def _clean() -> None:
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with SessionLocal() as session:
        await session.execute(text("SET session_replication_role = replica"))
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture(autouse=True)
def clean_tables(database: None) -> Iterator[None]:
    run(_clean())
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


class Browser:
    """로그인한 사용자 한 명의 브라우저. 변경 요청에 CSRF 토큰을 붙인다."""

    def __init__(self) -> None:
        self.http = TestClient(app, follow_redirects=False)
        self.token = ""

    def refresh_token(self) -> None:
        page = self.http.get("/login")
        self.token = re.search(r'X-CSRF-Token": "([^"]+)"', page.text).group(1)

    def login(self, email: str, password: str = PASSWORD) -> "Browser":
        self.refresh_token()
        response = self.post("/login", data={"email": email, "password": password})
        assert response.headers.get("HX-Redirect") == "/"
        self.refresh_token()
        return self

    def get(self, url: str, **kwargs):
        return self.http.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        headers = {"X-CSRF-Token": self.token, "HX-Request": "true"}
        return self.http.post(url, headers=headers, **kwargs)


async def _add(*rows: object) -> None:
    async with SessionLocal() as session:
        session.add_all(rows)
        await session.commit()


def add_user(name: str, **fields: object) -> User:
    if name == "admin":
        # 대부분의 시험은 PIN 변경을 마친 관리자를 쓴다. 초기 상태는 pin_hash=None으로 만든다.
        fields.setdefault("pin_hash", ADMIN_PIN_HASH)
    user = User(name=name, email=f"{name}@test.local", password_hash=PASSWORD_HASH, **fields)
    run(_add(user))
    return user


def add_department(name: str, parent: Department | None = None) -> Department:
    department = Department(name=name, parent_id=parent.id if parent else None)
    run(_add(department))
    return department


def join(user: User, department: Department) -> None:
    run(_add(UserDepartment(user_id=user.id, department_id=department.id)))


def add_group(owner: User, name: str = "그룹") -> WorkspaceGroup:
    """owner를 그룹 관리자로 지정한다."""
    group = WorkspaceGroup(name=name, created_by=owner.id)
    run(_add(group))
    run(_add(GroupManager(group_id=group.id, user_id=owner.id)))
    return group


def add_channel(group: WorkspaceGroup, name: str = "일반", type: str = "talk", **fields) -> Channel:
    channel = Channel(group_id=group.id, name=name, type=type, **fields)
    run(_add(channel))
    return channel


def grant(scope: Channel | WorkspaceGroup, subject: User | Department, **values: str) -> None:
    run(
        _add(
            PermissionGrant(
                scope_type="channel" if isinstance(scope, Channel) else "group",
                scope_id=scope.id,
                subject_type="user" if isinstance(subject, User) else "department",
                subject_id=subject.id,
                **{f"can_{key}": value for key, value in values.items()},
            )
        )
    )


FULL = {"read": "allow", "write": "allow", "reply_read": "allow", "reply_write": "allow"}


def browser(user: User) -> Browser:
    return Browser().login(user.email)


def admin_browser(user: User) -> Browser:
    """PIN까지 확인한 시스템 관리자 브라우저."""
    signed_in = browser(user)
    response = signed_in.post("/admin/pin", data={"pin": ADMIN_PIN})
    assert "HX-Redirect" in response.headers, response.text
    return signed_in


def post_id(response) -> int:
    return int(re.search(r'id="post-(\d+)"', response.text).group(1))

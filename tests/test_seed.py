import pytest
from sqlalchemy import func, select

from app.cli import reset_refusal
from app.core.config import Settings
from app.core.db import SessionLocal
from app.models import (
    Attachment,
    Channel,
    Department,
    GroupManager,
    PermissionGrant,
    Post,
    User,
    WorkCommand,
    WorkspaceGroup,
)
from app.seed import EMAIL_DOMAIN, SAMPLE_PASSWORD, seed
from app.services.permissions import Authorizer, ChannelAccess
from tests.conftest import Browser, add_user, run


def apply_seed() -> None:
    async def load() -> None:
        async with SessionLocal() as session:
            await seed(session)

    run(load())


def access(login: str, channel_name: str) -> ChannelAccess:
    async def load() -> ChannelAccess:
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(User.email == f"{login}@{EMAIL_DOMAIN}"))
            channel = await session.scalar(select(Channel).where(Channel.name == channel_name))
            return await Authorizer(session, user).access(channel)

    return run(load())


def count(model) -> int:
    async def load() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(func.count()).select_from(model))

    return run(load())


def sign_in(login: str) -> Browser:
    return Browser().login(f"{login}@{EMAIL_DOMAIN}", SAMPLE_PASSWORD)


def test_seed_builds_every_group_with_a_manager() -> None:
    apply_seed()
    assert count(WorkspaceGroup) == 3 and count(GroupManager) == 3
    assert count(User) == 10 and count(Channel) == 12
    assert count(PermissionGrant) == 17
    assert count(Post) > 15 and count(Attachment) == 2


def test_seed_refuses_non_empty_database() -> None:
    add_user("plain")
    with pytest.raises(RuntimeError, match="빈 DB"):
        apply_seed()


def test_seed_admin_starts_in_initial_pin_state() -> None:
    apply_seed()

    async def load() -> User:
        async with SessionLocal() as session:
            return await session.scalar(select(User).where(User.email == "admin@test.local"))

    admin = run(load())
    assert admin.pin_hash is None and admin.password_hash is None


def test_seed_shows_spec_permission_examples() -> None:
    apply_seed()
    assert access("sa2", "영업 일반").write is True
    assert access("sa3", "영업 일반").write is False
    assert access("pd2", "고객 미팅").read is True
    assert access("pd2", "영업 일반").can_enter is False


def test_seed_suggestion_box_is_write_only_except_hr() -> None:
    apply_seed()
    assert (access("sa2", "건의함").read, access("sa2", "건의함").write) == (False, True)
    assert access("hr1", "건의함").read is True
    assert access("sa2", "공지").write is False
    assert access("hr1", "공지").write is True


def test_seed_users_can_log_in_and_see_only_their_channels() -> None:
    apply_seed()
    page = sign_in("qa1").get("/").text
    assert "생산 일반" in page and "영업 일반" not in page


def test_seed_write_only_channel_hides_others_in_real_page() -> None:
    apply_seed()

    async def channel_id() -> int:
        async with SessionLocal() as session:
            return await session.scalar(select(Channel.id).where(Channel.name == "건의함"))

    identifier = run(channel_id())
    page = sign_in("sa2").get(f"/channels/{identifier}").text
    assert "정수기" in page and "회의실 예약" not in page
    page = sign_in("hr1").get(f"/channels/{identifier}").text
    assert "정수기" in page and "회의실 예약" in page


@pytest.mark.parametrize(
    ("debug", "url", "allowed"),
    [
        (True, "postgresql+asyncpg://u:p@localhost:5433/d", True),
        (True, "postgresql+asyncpg://u:p@127.0.0.1/d", True),
        (False, "postgresql+asyncpg://u:p@localhost:5433/d", False),
        (True, "postgresql+asyncpg://u:p@db.ncloud.example:5432/d", False),
    ],
)
def test_reset_only_allowed_on_local_debug(debug: bool, url: str, allowed: bool) -> None:
    settings = Settings(debug=debug, database_url=url)
    assert (reset_refusal(settings) is None) is allowed


def test_seed_uses_department_abbreviations_and_production() -> None:
    apply_seed()

    async def load() -> tuple[dict[str, str], set[str], set[str]]:
        async with SessionLocal() as session:
            users = dict((await session.execute(select(User.email, User.name))).all())
            groups = set(await session.scalars(select(WorkspaceGroup.name)))
            departments = set(await session.scalars(select(Department.name)))
            return users, groups, departments

    users, groups, departments = run(load())
    assert users[f"hr1@{EMAIL_DOMAIN}"] == "박인사"
    assert users[f"sa2@{EMAIL_DOMAIN}"] == "이영업"
    assert users[f"pd1@{EMAIL_DOMAIN}"] == "임생산"
    assert groups == {"전사", "영업", "생산"}
    assert {"생산본부", "생산팀", "품질팀"} <= departments
    assert not any("개발" in name for name in departments | groups)


def test_seed_work_channel_calls_sample_todo() -> None:
    """영업 그룹의 할 일 채널에 샘플 할 일 시스템을 부르는 /add와 /find가 있다."""
    apply_seed()

    async def load() -> tuple[str, set[tuple[str, str, str]]]:
        async with SessionLocal() as session:
            channel = await session.scalar(select(Channel).where(Channel.name == "할 일"))
            commands = await session.scalars(
                select(WorkCommand).where(WorkCommand.channel_id == channel.id)
            )
            return channel.type, {
                (row.name, row.screen, row.page, row.endpoint_url) for row in commands
            }

    url = "http://127.0.0.1:8100/todos"
    assert run(load()) == ("work", {("add", "todo", "index", url), ("find", "todo", "find", url)})
    assert access("sa2", "할 일").write

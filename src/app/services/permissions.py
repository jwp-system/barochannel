"""권한 판정은 이 파일 한 곳에만 둔다.

순서: 승인자 자동 부여 > 사용자 설정(채널, 그룹) > 소속 설정(상위 소속까지) > 기본 차단.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Channel,
    Department,
    GroupManager,
    PermissionGrant,
    Post,
    User,
    UserDepartment,
)

PERMISSIONS = ("read", "write", "reply_read", "reply_write")
Layer = Mapping[str, str | None]


@dataclass(frozen=True)
class ChannelAccess:
    read: bool = False
    write: bool = False
    reply_read: bool = False
    reply_write: bool = False

    @property
    def can_enter(self) -> bool:
        return self.read or self.write


NO_ACCESS = ChannelAccess()


def _first_value(layers: Iterable[Layer], permission: str) -> str | None:
    for layer in layers:
        value = layer.get(permission)
        if value is not None:
            return value
    return None


def _decide_one(
    permission: str, user_layers: Sequence[Layer], department_chains: Sequence[Sequence[Layer]]
) -> bool:
    user_value = _first_value(user_layers, permission)
    if user_value is not None:
        return user_value == "allow"
    # 겸직은 소속별로 따로 판정하고 하나라도 허용이면 허용한다.
    return any(_first_value(chain, permission) == "allow" for chain in department_chains)


def decide_access(
    *,
    is_approver: bool,
    user_layers: Sequence[Layer],
    department_chains: Sequence[Sequence[Layer]],
) -> ChannelAccess:
    """user_layers는 [채널, 그룹], chain은 [소속 채널, 소속 그룹, 상위 채널, 상위 그룹, ...]이다."""
    if is_approver:
        return ChannelAccess(True, True, True, True)
    read, write, reply_read, reply_write = (
        _decide_one(permission, user_layers, department_chains) for permission in PERMISSIONS
    )
    reply_read = reply_read and (read or write)
    reply_write = reply_write and reply_read
    return ChannelAccess(read, write, reply_read, reply_write)


def normalize_grant(values: dict[str, str | None]) -> dict[str, str | None]:
    """화면의 선행 조건을 서버에서도 맞춘다. 요청을 조작해도 저장값은 같은 규칙을 따른다."""
    result = dict(values)
    if result.get("reply_write") == "allow":
        result["reply_read"] = "allow"
    if result.get("reply_read") == "deny":
        result["reply_write"] = "deny"
    if result.get("read") == "deny" and result.get("write") == "deny":
        result["reply_read"] = "deny"
        result["reply_write"] = "deny"
    return result


def can_view_post(access: ChannelAccess, post: Post, user_id: int) -> bool:
    return access.read or (access.write and post.author_id == user_id)


def can_view_replies(access: ChannelAccess, parent: Post, user_id: int) -> bool:
    return can_view_post(access, parent, user_id) and access.reply_read


def can_write_reply(access: ChannelAccess, parent: Post, user_id: int) -> bool:
    return can_view_replies(access, parent, user_id) and access.reply_write


def _layer(grant: PermissionGrant | None) -> Layer:
    if grant is None:
        return {}
    return {
        "read": grant.can_read,
        "write": grant.can_write,
        "reply_read": grant.can_reply_read,
        "reply_write": grant.can_reply_write,
    }


class Authorizer:
    """한 요청 안에서 판정 재료를 한 번만 읽어 재사용한다."""

    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user
        self._loaded = False
        self._grants: dict[tuple[str, int, str, int], PermissionGrant] = {}
        self._chains: list[list[int]] = []
        self._managed: set[int] = set()

    async def _load(self) -> None:
        if self._loaded:
            return
        parents = dict(
            (await self.session.execute(select(Department.id, Department.parent_id))).all()
        )
        department_ids = await self.session.scalars(
            select(UserDepartment.department_id).where(UserDepartment.user_id == self.user.id)
        )
        self._chains = [self._ancestors(department_id, parents) for department_id in department_ids]
        related = {department_id for chain in self._chains for department_id in chain}
        grants = await self.session.scalars(
            select(PermissionGrant).where(
                or_(
                    and_(
                        PermissionGrant.subject_type == "user",
                        PermissionGrant.subject_id == self.user.id,
                    ),
                    and_(
                        PermissionGrant.subject_type == "department",
                        PermissionGrant.subject_id.in_(related),
                    ),
                )
            )
        )
        self._grants = {
            (grant.scope_type, grant.scope_id, grant.subject_type, grant.subject_id): grant
            for grant in grants
        }
        self._managed = set(
            await self.session.scalars(
                select(GroupManager.group_id).where(GroupManager.user_id == self.user.id)
            )
        )
        self._loaded = True

    @staticmethod
    def _ancestors(department_id: int, parents: Mapping[int, int | None]) -> list[int]:
        chain: list[int] = []
        current: int | None = department_id
        while current is not None and current not in chain:
            chain.append(current)
            current = parents.get(current)
        return chain

    def _subject_layers(self, channel: Channel, subject_type: str, subject_id: int) -> list[Layer]:
        return [
            _layer(self._grants.get(("channel", channel.id, subject_type, subject_id))),
            _layer(self._grants.get(("group", channel.group_id, subject_type, subject_id))),
        ]

    def _is_approver(self, channel: Channel) -> bool:
        # 2단계에서 approval_line_template 조회로 바꾼다. 판정 규칙은 decide_access에 이미 있다.
        return False

    async def access(self, channel: Channel) -> ChannelAccess:
        if channel.group.archived_at is not None:
            return NO_ACCESS
        await self._load()
        return decide_access(
            is_approver=self._is_approver(channel),
            user_layers=self._subject_layers(channel, "user", self.user.id),
            department_chains=[
                [
                    layer
                    for department_id in chain
                    for layer in self._subject_layers(channel, "department", department_id)
                ]
                for chain in self._chains
            ],
        )

    async def managed_group_ids(self) -> set[int]:
        await self._load()
        return self._managed

    async def is_manager(self, channel: Channel) -> bool:
        """채널은 그룹 관리자만 관리한다. 시스템 관리자의 권한은 그룹 단위에서 끝난다."""
        return channel.group_id in await self.managed_group_ids()

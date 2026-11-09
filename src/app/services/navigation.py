from dataclasses import dataclass, field

from sqlalchemy import select

from app.models import Channel, WorkspaceGroup
from app.services.permissions import Authorizer
from app.services.unread import COUNTED_TYPES, ensure_marks, unread_counts


@dataclass
class NavGroup:
    group: WorkspaceGroup
    channels: list[Channel] = field(default_factory=list)
    managed: bool = False
    # 채널 번호별 새 글 수. 0인 채널은 없다.
    unread: dict[int, int] = field(default_factory=dict)

    @property
    def unread_total(self) -> int:
        return sum(self.unread.values())

    @property
    def href(self) -> str:
        return (
            f"/channels/{self.channels[0].id}"
            if self.channels
            else f"/groups/{self.group.id}/settings"
        )


async def build_navigation(authz: Authorizer) -> list[NavGroup]:
    """볼 수 있는 채널이 있는 그룹과 내가 관리하는 그룹을 보여준다."""
    managed = await authz.managed_group_ids()
    rows = await authz.session.execute(
        select(WorkspaceGroup, Channel)
        .outerjoin(Channel, Channel.group_id == WorkspaceGroup.id)
        .where(WorkspaceGroup.archived_at.is_(None))
        .order_by(WorkspaceGroup.name, WorkspaceGroup.id, Channel.sort_order, Channel.id)
    )
    groups: dict[int, NavGroup] = {}
    counted: dict[int, NavGroup] = {}
    for group, channel in rows:
        if group.id in managed:
            groups.setdefault(group.id, NavGroup(group, managed=True))
        if channel is None:
            continue
        access = await authz.access(channel)
        if access.can_enter:
            nav = groups.setdefault(group.id, NavGroup(group))
            nav.channels.append(channel)
            if access.read and channel.type in COUNTED_TYPES:
                counted[channel.id] = nav
    await ensure_marks(authz.user.id, counted)
    for channel_id, count in (await unread_counts(authz.session, authz.user.id, counted)).items():
        counted[channel_id].unread[channel_id] = count
    return list(groups.values())


def unread_payload(navigation: list[NavGroup]) -> dict[str, dict[str, int]]:
    """화면에 보낼 새 글 수. 0도 보내야 읽은 채널의 표시가 지워진다."""
    return {
        "channels": {
            str(channel.id): item.unread.get(channel.id, 0)
            for item in navigation
            for channel in item.channels
        },
        "groups": {str(item.group.id): item.unread_total for item in navigation},
    }

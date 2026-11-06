from dataclasses import dataclass, field

from sqlalchemy import select

from app.models import Channel, WorkspaceGroup
from app.services.permissions import Authorizer


@dataclass
class NavGroup:
    group: WorkspaceGroup
    channels: list[Channel] = field(default_factory=list)
    managed: bool = False

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
    for group, channel in rows:
        if group.id in managed:
            groups.setdefault(group.id, NavGroup(group, managed=True))
        if channel is not None and (await authz.access(channel)).can_enter:
            groups.setdefault(group.id, NavGroup(group)).channels.append(channel)
    return list(groups.values())

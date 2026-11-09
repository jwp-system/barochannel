from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionDep
from app.core.deps import CurrentUser, current_user
from app.core.templating import fragment, templates
from app.models import (
    CHANNEL_TYPES,
    Channel,
    ChannelDispatchTarget,
    Department,
    GroupManager,
    PermissionGrant,
    User,
    WorkspaceGroup,
)
from app.routers.org import tree_rows
from app.services.approvals import line_rows, stalled_documents
from app.services.audit import record_audit
from app.services.bulk_import import download, run_import
from app.services.channel_import import (
    CHANNEL_SPECS,
    channel_importer,
    export_channels,
    managed_group_ids,
)
from app.services.permissions import PERMISSIONS, normalize_grant
from app.services.spreadsheet import MAX_FILE_BYTES

router = APIRouter(tags=["spaces"], include_in_schema=False, dependencies=[Depends(current_user)])

REFRESH = {"HX-Refresh": "true"}
Setting = Literal["", "allow", "deny"]
NameField = Annotated[str, Form(min_length=1, max_length=100)]
ImportMode = Annotated[str, Form(alias="mode", pattern="^(check|apply)$")]


async def managed_group(session: AsyncSession, group_id: int, user: User) -> WorkspaceGroup:
    group = await session.get(WorkspaceGroup, group_id)
    if group is None or group.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "그룹을 찾을 수 없습니다.")
    manager = await session.get(GroupManager, (group.id, user.id))
    if manager is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "그룹 관리자만 할 수 있습니다.")
    return group


async def managed_channel(session: AsyncSession, channel_id: int, user: User) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "채널을 찾을 수 없습니다.")
    await managed_group(session, channel.group_id, user)
    return channel


async def dispatch_context(session: AsyncSession, channel: Channel) -> dict[str, object]:
    if channel.type != "approval":
        return {"dispatch_targets": [], "dispatch_choices": []}
    current = (
        await session.execute(
            select(Channel, ChannelDispatchTarget.auto_send)
            .join(ChannelDispatchTarget, ChannelDispatchTarget.target_channel_id == Channel.id)
            .where(ChannelDispatchTarget.source_channel_id == channel.id)
            .order_by(Channel.name)
        )
    ).all()
    taken = {row.id for row, _ in current} | {channel.id}
    choices = await session.execute(
        select(Channel, WorkspaceGroup.name)
        .join(WorkspaceGroup, WorkspaceGroup.id == Channel.group_id)
        .where(Channel.type == "approval", WorkspaceGroup.archived_at.is_(None))
        .order_by(WorkspaceGroup.name, Channel.name)
    )
    return {
        "dispatch_targets": current,
        "dispatch_choices": [(row, name) for row, name in choices if row.id not in taken],
    }


async def grant_context(session: AsyncSession, scope_type: str, scope_id: int) -> dict[str, object]:
    grants = list(
        await session.scalars(
            select(PermissionGrant)
            .where(PermissionGrant.scope_type == scope_type, PermissionGrant.scope_id == scope_id)
            .order_by(PermissionGrant.subject_type, PermissionGrant.subject_id)
        )
    )
    departments = list(await session.scalars(select(Department)))
    users = list(
        await session.scalars(select(User).where(User.status == "active").order_by(User.name))
    )
    names = {("department", item.id): item.name for item in departments}
    names |= {("user", item.id): f"{item.name} ({item.email})" for item in users}
    return {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "grants": [
            (grant, names.get((grant.subject_type, grant.subject_id), "(비활성)"))
            for grant in grants
        ],
        "tree": tree_rows(departments),
        "subject_users": users,
    }


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, session: SessionDep, user: CurrentUser) -> HTMLResponse:
    groups = await session.scalars(
        select(WorkspaceGroup)
        .join(GroupManager, GroupManager.group_id == WorkspaceGroup.id)
        .where(GroupManager.user_id == user.id, WorkspaceGroup.archived_at.is_(None))
        .order_by(WorkspaceGroup.name, WorkspaceGroup.id)
    )
    return templates.TemplateResponse(
        request, "groups.html", {"user": user, "groups": list(groups), "active_tab": "more"}
    )


async def own_group_ids(session: AsyncSession, user: User) -> set[int]:
    group_ids = await managed_group_ids(session, user)
    if not group_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "그룹 관리자만 할 수 있습니다.")
    return group_ids


@router.get("/groups/export.xlsx")
async def channels_export(session: SessionDep, user: CurrentUser) -> Response:
    group_ids = await own_group_ids(session, user)
    return download(await export_channels(session, group_ids), "barochannel-channels")


@router.post("/groups/import", response_class=HTMLResponse)
async def channels_import(
    session: SessionDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    mode: ImportMode = "check",
) -> HTMLResponse:
    group_ids = await own_group_ids(session, user)
    data = await file.read(MAX_FILE_BYTES + 1)
    importer = channel_importer(group_ids)
    report = await run_import(session, user, data, CHANNEL_SPECS, importer, mode == "apply")
    return fragment("partials/import_report.html", report=report)


@router.get("/groups/{group_id}/settings", response_class=HTMLResponse)
async def group_settings(
    request: Request, group_id: int, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    group = await managed_group(session, group_id, user)
    channels = await session.scalars(
        select(Channel).where(Channel.group_id == group.id).order_by(Channel.sort_order, Channel.id)
    )
    return templates.TemplateResponse(
        request,
        "group_settings.html",
        {
            "user": user,
            "active_tab": "more",
            "group": group,
            "channels": list(channels),
            **await grant_context(session, "group", group.id),
        },
    )


@router.post("/groups/{group_id}/channels")
async def create_channel(
    group_id: int,
    session: SessionDep,
    user: CurrentUser,
    name: NameField,
    channel_type: Annotated[str, Form(alias="type")],
) -> Response:
    group = await managed_group(session, group_id, user)
    if channel_type not in CHANNEL_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "채널 유형이 맞지 않습니다.")
    last = await session.scalar(
        select(func.coalesce(func.max(Channel.sort_order), 0)).where(Channel.group_id == group.id)
    )
    channel = Channel(
        group_id=group.id, type=channel_type, name=name.strip(), sort_order=(last or 0) + 1
    )
    session.add(channel)
    await session.flush()
    record_audit(
        session,
        actor_id=user.id,
        action="channel.create",
        target_type="channel",
        target_id=channel.id,
        name=channel.name,
        type=channel_type,
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/channels/{channel_id}/move")
async def move_channel(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    direction: Annotated[Literal["up", "down"], Form()],
) -> Response:
    channel = await managed_channel(session, channel_id, user)
    ordered = list(
        await session.scalars(
            select(Channel)
            .where(Channel.group_id == channel.group_id)
            .order_by(Channel.sort_order, Channel.id)
            .with_for_update(of=Channel)
        )
    )
    index = next(i for i, item in enumerate(ordered) if item.id == channel.id)
    other = index - 1 if direction == "up" else index + 1
    if 0 <= other < len(ordered):
        ordered[index], ordered[other] = ordered[other], ordered[index]
        for position, item in enumerate(ordered, start=1):
            item.sort_order = position
    await session.commit()
    return Response(headers=REFRESH)


@router.get("/channels/{channel_id}/settings", response_class=HTMLResponse)
async def channel_settings(
    request: Request, channel_id: int, session: SessionDep, user: CurrentUser
) -> HTMLResponse:
    channel = await managed_channel(session, channel_id, user)
    talk_channels = await session.scalars(
        select(Channel).where(Channel.type == "talk").order_by(Channel.name)
    )
    lines = await line_rows(session, channel.id)
    return templates.TemplateResponse(
        request,
        "channel_settings.html",
        {
            "user": user,
            "active_tab": "more",
            "channel": channel,
            "lines": lines,
            "line_data": {str(row.department_id): row.approvers for row in lines},
            "stalled": await stalled_documents(session, Channel.id == channel.id),
            **await dispatch_context(session, channel),
            "talk_channels": list(talk_channels),
            **await grant_context(session, "channel", channel.id),
        },
    )


@router.post("/channels/{channel_id}/settings")
async def update_channel(
    channel_id: int,
    session: SessionDep,
    user: CurrentUser,
    name: NameField,
    pin_limit: Annotated[int, Form(ge=0, le=50)],
) -> Response:
    channel = await managed_channel(session, channel_id, user)
    before = {"name": channel.name, "pin_limit": channel.pin_limit}
    channel.name = name.strip()
    channel.pin_limit = pin_limit
    record_audit(
        session,
        actor_id=user.id,
        action="channel.update",
        target_type="channel",
        target_id=channel.id,
        before=before,
        after={"name": channel.name, "pin_limit": pin_limit},
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/permissions/{scope_type}/{scope_id}")
async def save_grant(
    scope_type: Literal["group", "channel"],
    scope_id: int,
    session: SessionDep,
    user: CurrentUser,
    subject: Annotated[str, Form(pattern=r"^(user|department):\d+$")],
    read: Annotated[Setting, Form()] = "",
    write: Annotated[Setting, Form()] = "",
    reply_read: Annotated[Setting, Form()] = "",
    reply_write: Annotated[Setting, Form()] = "",
) -> Response:
    if scope_type == "group":
        await managed_group(session, scope_id, user)
    else:
        await managed_channel(session, scope_id, user)
    subject_type, raw_id = subject.split(":")
    subject_id = int(raw_id)
    model = User if subject_type == "user" else Department
    if await session.get(model, subject_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "대상을 찾을 수 없습니다.")

    values = normalize_grant(
        {
            "read": read or None,
            "write": write or None,
            "reply_read": reply_read or None,
            "reply_write": reply_write or None,
        }
    )
    grant = await session.scalar(
        select(PermissionGrant)
        .where(
            PermissionGrant.scope_type == scope_type,
            PermissionGrant.scope_id == scope_id,
            PermissionGrant.subject_type == subject_type,
            PermissionGrant.subject_id == subject_id,
        )
        .with_for_update()
    )
    before = (
        {permission: getattr(grant, f"can_{permission}") for permission in PERMISSIONS}
        if grant
        else None
    )
    if all(value is None for value in values.values()):
        if grant is not None:
            await session.delete(grant)
    else:
        if grant is None:
            grant = PermissionGrant(
                scope_type=scope_type,
                scope_id=scope_id,
                subject_type=subject_type,
                subject_id=subject_id,
            )
            session.add(grant)
        for permission, value in values.items():
            setattr(grant, f"can_{permission}", value)
    record_audit(
        session,
        actor_id=user.id,
        action="permission.change",
        target_type=scope_type,
        target_id=scope_id,
        subject=subject,
        before=before,
        after=values,
    )
    await session.commit()
    return Response(headers=REFRESH)

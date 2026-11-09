from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.db import SessionDep
from app.core.deps import AuthzDep
from app.core.templating import templates
from app.models import Channel, Department, UserDepartment
from app.services.approvals import (
    my_drafts,
    my_handled,
    my_references,
    my_reviews,
    my_turn,
    stalled_in_groups,
    unread_notices,
)
from app.services.dispatch import receiving_for
from app.services.documents import document_rows
from app.services.mentions import pending_acks
from app.services.navigation import build_navigation
from app.services.permissions import ChannelAccess
from app.services.post_views import build_views, pinned_views, timeline_posts
from app.services.realtime import publish_counts
from app.services.unread import COUNTED_TYPES, mark_read

router = APIRouter(tags=["pages"], include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, authz: AuthzDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "root_tab": True,
        },
    )


@router.get("/todo", response_class=HTMLResponse)
async def todo(request: Request, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    items = []
    for item in await pending_acks(session, authz):
        (view,) = await build_views(
            session, [item.post], authz, item.channel, item.access, item.parent
        )
        items.append((item.channel, view))
    return templates.TemplateResponse(
        request,
        "todo.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "todo",
            "root_tab": True,
            "items": items,
            "stalled": await stalled_in_groups(session, await authz.managed_group_ids()),
            "turns": await my_turn(session, authz.user.id),
            "receiving": await receiving_for(session, authz),
            "reviews": await my_reviews(session, authz.user.id),
            "references": await my_references(session, authz.user.id),
            "notices": await unread_notices(session, authz.user.id),
            "drafts": await my_drafts(session, authz.user.id),
            "handled": await my_handled(session, authz.user.id),
        },
    )


@router.get("/more", response_class=HTMLResponse)
async def more(request: Request, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    departments = await session.scalars(
        select(Department.name)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == authz.user.id)
        .order_by(UserDepartment.is_primary.desc(), Department.name)
    )
    return templates.TemplateResponse(
        request,
        "more.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "more",
            "root_tab": True,
            "departments": list(departments),
            "manages_groups": bool(await authz.managed_group_ids()),
        },
    )


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_page(
    request: Request, channel_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    channel = await session.get(Channel, channel_id)
    access = await authz.access(channel) if channel else None
    if channel is None or access is None or not access.can_enter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "채널을 찾을 수 없습니다.")
    posts, has_more = await timeline_posts(session, channel, access, authz.user.id)
    if counts_unread(channel, access):
        await mark_read(session, authz.user.id, channel.id)
        await session.commit()
    navigation = await build_navigation(authz)
    return templates.TemplateResponse(
        request,
        "shell.html",
        {
            "user": authz.user,
            "navigation": navigation,
            "active_tab": "home",
            "channel": channel,
            "current_group": next(
                (item for item in navigation if item.group.id == channel.group_id), None
            ),
            "access": access,
            "views": await build_views(session, posts, authz, channel, access),
            "pinned": await pinned_views(session, authz, channel, access),
            "has_more": has_more,
            "is_manager": await authz.is_manager(channel),
            "can_post": access.write and channel.type == "talk",
            "documents": await document_rows(session, channel, access, authz.user.id)
            if channel.type == "doc"
            else [],
        },
    )


def counts_unread(channel: Channel, access: ChannelAccess) -> bool:
    return access.read and channel.type in COUNTED_TYPES


@router.post("/channels/{channel_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def read_channel(channel_id: int, session: SessionDep, authz: AuthzDep) -> Response:
    """채널을 보고 있는 동안 들어온 새 글을 읽은 것으로 한다. 개수 연결이 다시 센다."""
    channel = await session.get(Channel, channel_id)
    access = await authz.access(channel) if channel else None
    if channel is None or access is None or not access.can_enter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "채널을 찾을 수 없습니다.")
    if counts_unread(channel, access):
        await mark_read(session, authz.user.id, channel.id)
        await publish_counts(session, {authz.user.id})
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

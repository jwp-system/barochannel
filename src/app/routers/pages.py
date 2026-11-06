from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.db import SessionDep
from app.core.deps import AuthzDep
from app.core.templating import templates
from app.models import Channel, Department, UserDepartment
from app.services.navigation import build_navigation
from app.services.post_views import build_views, pinned_views, timeline_posts

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
async def todo(request: Request, authz: AuthzDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "todo.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "todo",
            "root_tab": True,
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
        },
    )

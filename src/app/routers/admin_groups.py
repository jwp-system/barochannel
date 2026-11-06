"""시스템 관리자의 그룹 관리. 그룹을 만들고 관리자를 지정하는 데까지 맡는다."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionDep
from app.core.deps import AdminUser, require_admin
from app.core.templating import templates
from app.models import GroupManager, User, WorkspaceGroup
from app.services.audit import record_audit

router = APIRouter(
    prefix="/admin", tags=["groups"], include_in_schema=False, dependencies=[Depends(require_admin)]
)

REFRESH = {"HX-Refresh": "true"}
NameField = Annotated[str, Form(min_length=1, max_length=100)]


async def get_group(session: AsyncSession, group_id: int) -> WorkspaceGroup:
    group = await session.get(WorkspaceGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "그룹을 찾을 수 없습니다.")
    return group


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, session: SessionDep, user: AdminUser) -> HTMLResponse:
    groups = await session.scalars(select(WorkspaceGroup).order_by(WorkspaceGroup.name))
    return templates.TemplateResponse(
        request,
        "admin_groups.html",
        {"user": user, "groups": list(groups), "active_tab": "more"},
    )


@router.post("/groups")
async def create_group(session: SessionDep, user: AdminUser, name: NameField) -> Response:
    group = WorkspaceGroup(name=name.strip(), created_by=user.id)
    session.add(group)
    await session.flush()
    record_audit(
        session,
        actor_id=user.id,
        action="group.create",
        target_type="group",
        target_id=group.id,
        name=group.name,
    )
    await session.commit()
    return Response(headers={"HX-Redirect": f"/admin/groups/{group.id}"})


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def group_page(
    request: Request, group_id: int, session: SessionDep, user: AdminUser
) -> HTMLResponse:
    group = await get_group(session, group_id)
    managers = list(
        await session.scalars(
            select(User)
            .join(GroupManager, GroupManager.user_id == User.id)
            .where(GroupManager.group_id == group.id)
            .order_by(User.name)
        )
    )
    manager_ids = {manager.id for manager in managers}
    users = await session.scalars(select(User).where(User.status == "active").order_by(User.name))
    return templates.TemplateResponse(
        request,
        "admin_group.html",
        {
            "user": user,
            "active_tab": "more",
            "group": group,
            "managers": managers,
            "candidates": [item for item in users if item.id not in manager_ids],
        },
    )


@router.post("/groups/{group_id}")
async def update_group(
    group_id: int, session: SessionDep, user: AdminUser, name: NameField
) -> Response:
    group = await get_group(session, group_id)
    before = group.name
    group.name = name.strip()
    record_audit(
        session,
        actor_id=user.id,
        action="group.update",
        target_type="group",
        target_id=group.id,
        before=before,
        after=group.name,
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/groups/{group_id}/archive")
async def toggle_archive(group_id: int, session: SessionDep, user: AdminUser) -> Response:
    group = await get_group(session, group_id)
    group.archived_at = None if group.archived_at else datetime.now(UTC)
    record_audit(
        session,
        actor_id=user.id,
        action="group.archive" if group.archived_at else "group.unarchive",
        target_type="group",
        target_id=group.id,
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/groups/{group_id}/managers")
async def add_manager(
    group_id: int, session: SessionDep, admin: AdminUser, user_id: Annotated[int, Form()]
) -> Response:
    group = await get_group(session, group_id)
    target = await session.get(User, user_id)
    if target is None or not target.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사용자를 찾을 수 없습니다.")
    await session.execute(
        insert(GroupManager).values(group_id=group.id, user_id=user_id).on_conflict_do_nothing()
    )
    record_audit(
        session,
        actor_id=admin.id,
        action="group.manager_add",
        target_type="group",
        target_id=group.id,
        user_id=user_id,
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/groups/{group_id}/managers/{user_id}/delete")
async def remove_manager(
    group_id: int, user_id: int, session: SessionDep, admin: AdminUser
) -> Response:
    group = await get_group(session, group_id)
    manager = await session.get(GroupManager, (group.id, user_id))
    if manager is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "그룹 관리자가 아닙니다.")
    await session.delete(manager)
    record_audit(
        session,
        actor_id=admin.id,
        action="group.manager_remove",
        target_type="group",
        target_id=group.id,
        user_id=user_id,
    )
    await session.commit()
    return Response(headers=REFRESH)

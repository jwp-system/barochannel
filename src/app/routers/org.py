from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.deps import AdminUser, require_admin
from app.core.security import hash_password
from app.core.templating import fragment, templates
from app.models import Department, User, UserDepartment
from app.services.audit import record_audit

router = APIRouter(
    prefix="/admin", tags=["org"], include_in_schema=False, dependencies=[Depends(require_admin)]
)

REFRESH = {"HX-Refresh": "true"}


def tree_rows(departments: list[Department]) -> list[tuple[Department, int]]:
    children: dict[int | None, list[Department]] = {}
    for department in departments:
        children.setdefault(department.parent_id, []).append(department)
    rows: list[tuple[Department, int]] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for department in sorted(children.get(parent_id, []), key=lambda item: item.name):
            rows.append((department, depth))
            walk(department.id, depth + 1)

    walk(None, 0)
    return rows


@router.get("/org", response_class=HTMLResponse)
async def org_page(request: Request, session: SessionDep, user: AdminUser) -> HTMLResponse:
    departments = list(await session.scalars(select(Department)))
    users = list(await session.scalars(select(User).order_by(User.name, User.id)))
    memberships: dict[int, set[int]] = {}
    primary: dict[int, int] = {}
    for link in await session.scalars(select(UserDepartment)):
        memberships.setdefault(link.user_id, set()).add(link.department_id)
        if link.is_primary:
            primary[link.user_id] = link.department_id
    return templates.TemplateResponse(
        request,
        "admin_org.html",
        {
            "user": user,
            "active_tab": "more",
            "tree": tree_rows(departments),
            "users": users,
            "memberships": memberships,
            "primary": primary,
        },
    )


@router.post("/departments")
async def create_department(
    session: SessionDep,
    user: AdminUser,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    parent_id: Annotated[int | None, Form()] = None,
) -> Response:
    if parent_id is not None and await session.get(Department, parent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "상위 소속을 찾을 수 없습니다.")
    department = Department(name=name.strip(), parent_id=parent_id)
    session.add(department)
    await session.flush()
    record_audit(
        session,
        actor_id=user.id,
        action="department.create",
        target_type="department",
        target_id=department.id,
        name=department.name,
        parent_id=parent_id,
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/departments/{department_id}")
async def update_department(
    department_id: int,
    session: SessionDep,
    user: AdminUser,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    parent_id: Annotated[int | None, Form()] = None,
) -> Response:
    department = await session.get(Department, department_id)
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "소속을 찾을 수 없습니다.")
    parents = dict((await session.execute(select(Department.id, Department.parent_id))).all())
    current = parent_id
    while current is not None:
        if current == department_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "소속 트리가 순환하게 됩니다.")
        if current not in parents:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "상위 소속을 찾을 수 없습니다.")
        current = parents[current]
    before = {"name": department.name, "parent_id": department.parent_id}
    department.name = name.strip()
    department.parent_id = parent_id
    record_audit(
        session,
        actor_id=user.id,
        action="department.update",
        target_type="department",
        target_id=department.id,
        before=before,
        after={"name": department.name, "parent_id": parent_id},
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/users")
async def create_user(
    session: SessionDep,
    admin: AdminUser,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    email: Annotated[str, Form(min_length=3, max_length=255)],
    password: Annotated[str, Form(min_length=8)],
) -> Response:
    email = email.strip().lower()
    reserved = email == get_settings().admin_email
    if reserved or await session.scalar(select(User.id).where(User.email == email)):
        return fragment("partials/message.html", message="이미 등록된 이메일입니다.")
    user = User(name=name.strip(), email=email, password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    record_audit(
        session,
        actor_id=admin.id,
        action="user.create",
        target_type="user",
        target_id=user.id,
        email=email,
    )
    await session.commit()
    return Response(headers=REFRESH)


@router.post("/users/{user_id}")
async def update_user(
    user_id: int,
    session: SessionDep,
    admin: AdminUser,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    status_value: Annotated[str, Form(alias="status", pattern="^(active|inactive)$")],
    department_ids: Annotated[list[int] | None, Form()] = None,
    primary_department_id: Annotated[int | None, Form()] = None,
) -> Response:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사용자를 찾을 수 없습니다.")
    if user.id == admin.id and status_value != "active":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "자신의 계정은 끌 수 없습니다.")
    selected = set(department_ids or [])
    if selected:
        known = set(await session.scalars(select(Department.id).where(Department.id.in_(selected))))
        if known != selected:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "소속을 찾을 수 없습니다.")
    if primary_department_id not in selected:
        primary_department_id = min(selected) if selected else None

    before = {
        "name": user.name,
        "status": user.status,
        "departments": sorted(
            await session.scalars(
                select(UserDepartment.department_id).where(UserDepartment.user_id == user.id)
            )
        ),
    }
    user.name = name.strip()
    user.status = status_value
    await session.execute(delete(UserDepartment).where(UserDepartment.user_id == user.id))
    session.add_all(
        UserDepartment(
            user_id=user.id,
            department_id=department_id,
            is_primary=department_id == primary_department_id,
        )
        for department_id in selected
    )
    record_audit(
        session,
        actor_id=admin.id,
        action="user.update",
        target_type="user",
        target_id=user.id,
        before=before,
        after={
            "name": user.name,
            "status": status_value,
            "departments": sorted(selected),
        },
    )
    await session.commit()
    return Response(headers=REFRESH)

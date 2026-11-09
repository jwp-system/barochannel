from datetime import datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import Integer, cast, or_, select
from sqlalchemy.orm import aliased

from app.core.db import SessionDep
from app.core.deps import AdminUser, require_admin
from app.core.templating import LOCAL_TZ, fragment, templates
from app.models import AuditLog, User
from app.services.audit_view import ACTION_GROUPS, action_label, describe, names_for

router = APIRouter(
    prefix="/admin", tags=["audit"], include_in_schema=False, dependencies=[Depends(require_admin)]
)

PAGE = 50


def day_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.fromisoformat(value).date(), time(), LOCAL_TZ)
    except ValueError:
        return None


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    session: SessionDep,
    user: AdminUser,
    kind: Annotated[str, Query(max_length=30)] = "",
    actor: Annotated[str, Query(max_length=255)] = "",
    target: Annotated[str, Query(max_length=20)] = "",
    since: Annotated[str, Query(max_length=10)] = "",
    until: Annotated[str, Query(max_length=10)] = "",
    before: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    """권한 변경과 승인 이력을 찾는다. 로그는 추가 전용이라 이 화면도 보기만 한다."""
    who = aliased(User)
    query = select(AuditLog, who.name, who.email).outerjoin(who, who.id == AuditLog.actor_id)
    if kind in ACTION_GROUPS:
        prefixes = ACTION_GROUPS[kind][1]
        query = query.where(or_(*(AuditLog.action.startswith(prefix) for prefix in prefixes)))
    if actor.strip():
        query = query.where(
            or_(
                who.email.icontains(actor.strip(), autoescape=True),
                who.name.icontains(actor.strip(), autoescape=True),
            )
        )
    if target.strip().isdigit():
        number = int(target.strip())
        # 결재는 결재 번호와 글 번호 어느 쪽으로도 찾는다.
        query = query.where(
            or_(
                AuditLog.target_id == number,
                cast(AuditLog.detail["post_id"].astext, Integer) == number,
            )
        )
    if start := day_start(since):
        query = query.where(AuditLog.created_at >= start)
    if end := day_start(until):
        query = query.where(AuditLog.created_at < end + timedelta(days=1))
    if before is not None:
        query = query.where(AuditLog.id < before)
    rows = (await session.execute(query.order_by(AuditLog.id.desc()).limit(PAGE + 1))).all()
    names = await names_for(session, (row[0] for row in rows[:PAGE]), full=False)
    filters = {"kind": kind, "actor": actor, "target": target, "since": since, "until": until}
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "user": user,
            "active_tab": "more",
            "rows": rows[:PAGE],
            "next_before": rows[PAGE - 1][0].id if len(rows) > PAGE else None,
            "groups": {key: label for key, (label, _) in ACTION_GROUPS.items()},
            "names": names,
            "action_label": action_label,
            "filters": filters,
        },
    )


@router.get("/audit/{log_id}", response_class=HTMLResponse)
async def audit_detail(log_id: int, session: SessionDep) -> HTMLResponse:
    log = await session.get(AuditLog, log_id)
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기록을 찾을 수 없습니다.")
    names = await names_for(session, [log], full=True)
    return fragment(
        "partials/audit_detail.html",
        log=log,
        detail=describe(names, log),
        action_label=action_label,
        LOCAL_TZ=LOCAL_TZ,
    )

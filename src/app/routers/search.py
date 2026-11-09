from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.core.templating import templates
from app.models import Channel, WorkspaceGroup
from app.services.navigation import build_navigation
from app.services.search import search_posts

router = APIRouter(tags=["search"], include_in_schema=False, dependencies=[Depends(current_user)])


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    session: SessionDep,
    authz: AuthzDep,
    q: Annotated[str, Query(max_length=100)] = "",
    group_id: Annotated[int | None, Query()] = None,
    channel_id: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    query = q.strip()
    group = await session.get(WorkspaceGroup, group_id) if group_id else None
    channel = await session.get(Channel, channel_id) if channel_id else None
    results = await search_posts(authz, query, group_id, channel_id) if query else []
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "q": query,
            "group": group,
            "scope_channel": channel,
            "results": results,
        },
    )

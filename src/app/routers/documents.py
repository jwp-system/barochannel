from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.core.markdown import render_markdown
from app.core.templating import templates
from app.models import Post, PostRevision, User
from app.routers.posts import (
    Body,
    Files,
    apply_mentions,
    channel_access,
    check_upload_size,
    is_document,
    not_found,
    post_context,
    require_content,
    store_attachments,
)
from app.services.navigation import build_navigation
from app.services.realtime import Event, publish

router = APIRouter(
    tags=["documents"], include_in_schema=False, dependencies=[Depends(current_user)]
)


@router.get("/channels/{channel_id}/docs/new", response_class=HTMLResponse)
async def new_document(
    request: Request, channel_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "doc" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "문서를 올릴 수 없습니다.")
    return templates.TemplateResponse(
        request,
        "document_new.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
        },
    )


@router.post("/channels/{channel_id}/docs", dependencies=[Depends(check_upload_size)])
async def create_document(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    title: Annotated[str, Form(min_length=1, max_length=200)],
    body: Body = "",
    files: Files = None,
) -> Response:
    files = files or []
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "doc" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "문서를 올릴 수 없습니다.")
    if not title.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "제목을 입력해 주세요.")
    require_content(body, files)
    post = Post(channel_id=channel.id, author_id=authz.user.id, body_md=body, title=title.strip())
    session.add(post)
    await session.flush()
    await apply_mentions(session, post, None, channel)
    await store_attachments(session, post, files)
    await publish(session, Event("created", channel.id, post.id, authz.user.id))
    await session.commit()
    return Response(headers={"HX-Redirect": f"/posts/{post.id}"})


@router.get("/posts/{post_id}/revisions", response_class=HTMLResponse)
async def revisions(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    """F-41: 이전 버전은 보기만 한다. 비교와 복원은 없다."""
    post, _, channel, _ = await post_context(session, authz, post_id)
    if not is_document(post, channel):
        raise not_found()
    rows = await session.execute(
        select(PostRevision, User.name)
        .join(User, User.id == PostRevision.edited_by)
        .where(PostRevision.post_id == post.id)
        .order_by(PostRevision.version.desc())
    )
    versions = [
        {
            "version": revision.version,
            "title": revision.title,
            "html": render_markdown(revision.body_md),
            "replaced_by": name,
            "replaced_at": revision.created_at,
        }
        for revision, name in rows
    ]
    return templates.TemplateResponse(
        request,
        "revisions.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
            "post": post,
            "current": len(versions) + 1,
            "versions": versions,
        },
    )

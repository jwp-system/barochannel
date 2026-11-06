from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.core.markdown import render_markdown
from app.core.templating import fragment
from app.models import Attachment, Channel, Post, PostRevision, Reaction
from app.services.audit import record_audit
from app.services.permissions import (
    Authorizer,
    ChannelAccess,
    can_view_replies,
    can_write_reply,
)
from app.services.post_views import EMOJIS, build_views, can_view_any, pinned_views, timeline_posts
from app.services.realtime import Event, publish
from app.services.storage import mime_for, put_file

router = APIRouter(tags=["posts"], include_in_schema=False, dependencies=[Depends(current_user)])

MAX_BODY_LENGTH = 50_000
Files = Annotated[list[UploadFile] | None, File()]
Body = Annotated[str, Form(max_length=MAX_BODY_LENGTH)]


def not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "글을 찾을 수 없습니다.")


async def channel_access(
    session: AsyncSession, authz: Authorizer, channel_id: int
) -> tuple[Channel, ChannelAccess]:
    channel = await session.get(Channel, channel_id)
    access = await authz.access(channel) if channel else None
    if channel is None or access is None or not access.can_enter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "채널을 찾을 수 없습니다.")
    return channel, access


async def post_context(
    session: AsyncSession, authz: Authorizer, post_id: int, *, lock: bool = False
) -> tuple[Post, Post | None, Channel, ChannelAccess]:
    """볼 수 없는 글은 존재 여부도 알리지 않도록 모두 404로 돌려준다."""
    post = await session.get(Post, post_id, with_for_update=lock or None)
    if post is None:
        raise not_found()
    channel = await session.get(Channel, post.channel_id)
    access = await authz.access(channel) if channel else None
    parent = await session.get(Post, post.parent_post_id) if post.parent_post_id else None
    if channel is None or access is None or not can_view_any(access, post, parent, authz.user.id):
        raise not_found()
    return post, parent, channel, access


def check_upload_size(request: Request) -> None:
    settings = get_settings()
    limit = (settings.max_upload_mb * settings.max_files_per_post + 1) * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "첨부 용량이 너무 큽니다.")


async def store_attachments(session: AsyncSession, post: Post, files: list[UploadFile]) -> None:
    settings = get_settings()
    uploads = [file for file in files if file.filename]
    if len(uploads) > settings.max_files_per_post:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"첨부는 {settings.max_files_per_post}개까지 가능합니다.",
        )
    checked = []
    for file in uploads:
        name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1][:255]
        mime = mime_for(name)
        if mime is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"허용하지 않는 파일 형식입니다: {name}"
            )
        if (file.size or 0) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"파일은 {settings.max_upload_mb}MB까지 가능합니다: {name}",
            )
        checked.append((file, name, mime))
    for file, name, mime in checked:
        key = f"attachments/{uuid4().hex}"
        await put_file(key, file.file, mime)
        session.add(
            Attachment(
                post_id=post.id, file_name=name, storage_key=key, size=file.size or 0, mime=mime
            )
        )


def require_content(body: str, files: list[UploadFile]) -> None:
    if not body.strip() and not any(file.filename for file in files):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "내용을 입력해 주세요.")


async def card(
    session: AsyncSession,
    authz: Authorizer,
    post: Post,
    parent: Post | None,
    channel: Channel,
    access: ChannelAccess,
) -> HTMLResponse:
    (view,) = await build_views(session, [post], authz, channel, access, parent)
    return fragment("partials/post_card.html", v=view)


@router.post("/preview", response_class=HTMLResponse)
async def preview(body: Body = "") -> HTMLResponse:
    return fragment("partials/preview.html", html=render_markdown(body))


@router.get("/channels/{channel_id}/posts", response_class=HTMLResponse)
async def older_posts(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    before: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    channel, access = await channel_access(session, authz, channel_id)
    posts, has_more = await timeline_posts(session, channel, access, authz.user.id, before)
    views = await build_views(session, posts, authz, channel, access)
    return fragment("partials/post_page.html", views=views, has_more=has_more, channel=channel)


@router.post(
    "/channels/{channel_id}/posts",
    response_class=HTMLResponse,
    dependencies=[Depends(check_upload_size)],
)
async def create_post(
    channel_id: int, session: SessionDep, authz: AuthzDep, body: Body = "", files: Files = None
) -> HTMLResponse:
    files = files or []
    channel, access = await channel_access(session, authz, channel_id)
    if not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "쓰기 권한이 없습니다.")
    if channel.type != "talk":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "이 채널 유형의 글쓰기는 아직 열리지 않았습니다."
        )
    require_content(body, files)
    post = Post(channel_id=channel.id, author_id=authz.user.id, body_md=body)
    session.add(post)
    await session.flush()
    await store_attachments(session, post, files)
    await publish(session, Event("created", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, None, channel, access)


@router.get("/posts/{post_id}/thread", response_class=HTMLResponse)
async def thread(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    post, parent, channel, access = await post_context(session, authz, post_id)
    if parent is not None or not can_view_replies(access, post, authz.user.id):
        raise not_found()
    replies = await session.scalars(
        select(Post).where(Post.parent_post_id == post.id).order_by(Post.id)
    )
    (root,) = await build_views(session, [post], authz, channel, access)
    return fragment(
        "partials/thread.html",
        root=root,
        views=await build_views(session, list(replies), authz, channel, access, post),
        can_reply=can_write_reply(access, post, authz.user.id) and post.deleted_at is None,
    )


@router.post(
    "/posts/{post_id}/replies",
    response_class=HTMLResponse,
    dependencies=[Depends(check_upload_size)],
)
async def create_reply(
    post_id: int, session: SessionDep, authz: AuthzDep, body: Body = "", files: Files = None
) -> HTMLResponse:
    files = files or []
    # 글 삭제와 답글 작성이 겹쳐도 F-18이 깨지지 않도록 부모 행을 잠근다.
    post, parent, channel, access = await post_context(session, authz, post_id, lock=True)
    if parent is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "쓰레드는 1단계만 쓸 수 있습니다.")
    if not can_write_reply(access, post, authz.user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "답글쓰기 권한이 없습니다.")
    if post.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "삭제된 글에는 답글을 쓸 수 없습니다.")
    require_content(body, files)
    reply = Post(
        channel_id=channel.id, parent_post_id=post.id, author_id=authz.user.id, body_md=body
    )
    session.add(reply)
    await session.flush()
    await store_attachments(session, reply, files)
    await publish(session, Event("created", channel.id, reply.id, authz.user.id))
    await session.commit()
    return await card(session, authz, reply, post, channel, access)


@router.get("/posts/{post_id}/card", response_class=HTMLResponse)
async def post_card(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    post, parent, channel, access = await post_context(session, authz, post_id)
    return await card(session, authz, post, parent, channel, access)


async def editable_view(
    session: AsyncSession, authz: Authorizer, post_id: int, *, lock: bool = False
) -> tuple[Post, Post | None, Channel, ChannelAccess]:
    context = await post_context(session, authz, post_id, lock=lock)
    post, parent, channel, access = context
    (view,) = await build_views(session, [post], authz, channel, access, parent)
    if not view.can_edit:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "본인 글만 고칠 수 있습니다.")
    return context


@router.get("/posts/{post_id}/edit", response_class=HTMLResponse)
async def edit_form(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    post, *_ = await editable_view(session, authz, post_id)
    return fragment("partials/post_edit.html", post=post)


@router.post("/posts/{post_id}/edit", response_class=HTMLResponse)
async def update_post(
    post_id: int, session: SessionDep, authz: AuthzDep, body: Body
) -> HTMLResponse:
    post, parent, channel, access = await editable_view(session, authz, post_id, lock=True)
    if not body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "내용을 입력해 주세요.")
    if body != post.body_md:
        version = await session.scalar(
            select(func.coalesce(func.max(PostRevision.version), 0)).where(
                PostRevision.post_id == post.id
            )
        )
        session.add(
            PostRevision(
                post_id=post.id,
                version=(version or 0) + 1,
                body_md=post.body_md,
                edited_by=authz.user.id,
            )
        )
        post.body_md = body
        post.edited_at = datetime.now(UTC)
        await publish(session, Event("updated", channel.id, post.id, authz.user.id))
        await session.commit()
    return await card(session, authz, post, parent, channel, access)


@router.post("/posts/{post_id}/delete", response_class=HTMLResponse)
async def delete_post(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    post, parent, channel, access = await editable_view(session, authz, post_id, lock=True)
    live_replies = await session.scalar(
        select(func.count())
        .select_from(Post)
        .where(Post.parent_post_id == post.id, Post.deleted_at.is_(None))
    )
    if live_replies:
        raise HTTPException(status.HTTP_409_CONFLICT, "답글이 남아 있어 삭제할 수 없습니다.")
    was_pinned = post.pinned_at is not None
    post.deleted_at = datetime.now(UTC)
    post.deleted_by = authz.user.id
    post.pinned_at = None
    post.pinned_by = None
    record_audit(
        session,
        actor_id=authz.user.id,
        action="post.delete",
        target_type="post",
        target_id=post.id,
        channel_id=channel.id,
    )
    await publish(session, Event("updated", channel.id, post.id, authz.user.id))
    if was_pinned:
        await publish(session, Event("pins", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, parent, channel, access)


@router.post("/posts/{post_id}/reactions", response_class=HTMLResponse)
async def toggle_reaction(
    post_id: int, session: SessionDep, authz: AuthzDep, emoji: Annotated[str, Form()]
) -> HTMLResponse:
    post, parent, channel, access = await post_context(session, authz, post_id)
    if emoji not in EMOJIS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "지원하지 않는 반응입니다.")
    if not access.read or post.deleted_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "읽기 권한자만 반응할 수 있습니다.")
    removed = await session.execute(
        delete(Reaction).where(
            Reaction.post_id == post.id,
            Reaction.user_id == authz.user.id,
            Reaction.emoji == emoji,
        )
    )
    if removed.rowcount == 0:
        # 빠른 연속 클릭이 겹쳐도 기본 키 충돌로 실패하지 않게 한다.
        await session.execute(
            insert(Reaction)
            .values(post_id=post.id, user_id=authz.user.id, emoji=emoji)
            .on_conflict_do_nothing()
        )
    await publish(session, Event("updated", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, parent, channel, access)


@router.post("/posts/{post_id}/pin", response_class=HTMLResponse)
async def toggle_pin(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    post, parent, channel, access = await post_context(session, authz, post_id)
    if parent is not None or channel.type == "approval" or post.deleted_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "고정할 수 없는 글입니다.")
    if not await authz.is_manager(channel):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "채널 관리자만 고정할 수 있습니다.")
    # 고정 개수를 동시에 넘지 않도록 채널 행으로 순서를 세운다.
    await session.execute(select(Channel.id).where(Channel.id == channel.id).with_for_update())
    await session.refresh(post)
    if post.pinned_at is None:
        pinned = await session.scalar(
            select(func.count())
            .select_from(Post)
            .where(Post.channel_id == channel.id, Post.pinned_at.is_not(None))
        )
        if (pinned or 0) >= channel.pin_limit:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"고정은 {channel.pin_limit}개까지 가능합니다."
            )
        post.pinned_at = datetime.now(UTC)
        post.pinned_by = authz.user.id
    else:
        post.pinned_at = None
        post.pinned_by = None
    await publish(session, Event("updated", channel.id, post.id, authz.user.id))
    await publish(session, Event("pins", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, parent, channel, access)


@router.get("/channels/{channel_id}/pins", response_class=HTMLResponse)
async def pins(channel_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    channel, access = await channel_access(session, authz, channel_id)
    views = await pinned_views(session, authz, channel, access)
    return fragment("partials/pinned.html", views=views)

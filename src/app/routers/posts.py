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
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.core.markdown import render_markdown
from app.core.templating import fragment, templates
from app.models import (
    Approval,
    Attachment,
    Channel,
    Department,
    MentionAck,
    Post,
    PostRevision,
    Reaction,
    User,
)
from app.services.audit import record_audit
from app.services.mentions import MentionError, sync_mentions
from app.services.navigation import build_navigation
from app.services.permissions import (
    Authorizer,
    ChannelAccess,
    access_for_users,
    can_view_replies,
    can_write_reply,
)
from app.services.post_views import EMOJIS, build_views, can_view_any, pinned_views, timeline_posts
from app.services.realtime import Event, publish, publish_counts
from app.services.storage import mime_for, put_file

router = APIRouter(tags=["posts"], include_in_schema=False, dependencies=[Depends(current_user)])

MAX_BODY_LENGTH = 50_000
MENTION_CANDIDATES = 8
Files = Annotated[list[UploadFile] | None, File()]
Body = Annotated[str, Form(max_length=MAX_BODY_LENGTH)]


def is_document(post: Post, channel: Channel) -> bool:
    return channel.type == "doc" and post.parent_post_id is None


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
    parent = await session.get(Post, post.parent_post_id) if post.parent_post_id else None
    access = await authz.post_access(channel, post, parent) if channel else None
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


async def apply_mentions(
    session: AsyncSession, post: Post, parent: Post | None, channel: Channel
) -> None:
    if post.is_draft:
        # 임시저장은 대상자가 볼 수 없다. 상신할 때 멘션을 처리한다.
        return
    try:
        changed = await sync_mentions(session, post, parent, channel)
    except MentionError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    await publish_counts(session, changed)


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
    await apply_mentions(session, post, None, channel)
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
    await apply_mentions(session, reply, post, channel)
    await store_attachments(session, reply, files)
    await publish(session, Event("created", channel.id, reply.id, authz.user.id))
    await session.commit()
    return await card(session, authz, reply, post, channel, access)


@router.get("/posts/{post_id}", response_class=HTMLResponse)
async def post_page(
    request: Request, post_id: int, session: SessionDep, authz: AuthzDep
) -> Response:
    """글 하나만 보는 화면. 검색 결과와 문서 보기가 쓴다. 답글은 원글로 보낸다."""
    post, parent, channel, access = await post_context(session, authz, post_id)
    if parent is not None:
        return RedirectResponse(f"/posts/{parent.id}#post-{post.id}", status.HTTP_303_SEE_OTHER)
    if channel.type == "approval":
        return RedirectResponse(f"/approvals/{post.id}", status.HTTP_303_SEE_OTHER)
    (view,) = await build_views(session, [post], authz, channel, access)
    return templates.TemplateResponse(
        request,
        "post.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
            "v": view,
            "show_thread": can_view_replies(access, post, authz.user.id),
            "live_channel": (await authz.access(channel)).can_enter,
        },
    )


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
    has_acks = await session.scalar(
        select(func.count()).select_from(MentionAck).where(MentionAck.post_id == post.id)
    )
    approval = await session.scalar(select(Approval).where(Approval.post_id == post.id))
    return fragment(
        "partials/post_edit.html", post=post, has_acks=bool(has_acks), approval=approval
    )


@router.post("/posts/{post_id}/edit", response_class=HTMLResponse)
async def update_post(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    body: Body,
    reconfirm: Annotated[bool, Form()] = False,
    title: Annotated[str, Form(max_length=200)] = "",
) -> HTMLResponse:
    post, parent, channel, access = await editable_view(session, authz, post_id, lock=True)
    if not body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "내용을 입력해 주세요.")
    if title.strip():
        approval = await session.scalar(select(Approval).where(Approval.post_id == post.id))
        if approval is not None:
            approval.title = title.strip()
    # 문서는 제목도 버전에 들어간다. 제목만 바꿔도 이전 버전을 남긴다.
    new_title = title.strip() if post.title is not None and title.strip() else post.title
    if body != post.body_md or new_title != post.title:
        version = await session.scalar(
            select(func.coalesce(func.max(PostRevision.version), 0)).where(
                PostRevision.post_id == post.id
            )
        )
        session.add(
            PostRevision(
                post_id=post.id,
                version=(version or 0) + 1,
                title=post.title,
                body_md=post.body_md,
                edited_by=authz.user.id,
            )
        )
        post.body_md = body
        post.title = new_title
        post.edited_at = datetime.now(UTC)
        await apply_mentions(session, post, parent, channel)
        if reconfirm:
            await request_reconfirm(session, post, authz.user.id)
        await publish(session, Event("updated", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, parent, channel, access)


async def request_reconfirm(session: AsyncSession, post: Post, actor_id: int) -> None:
    """F-26: 작성자가 고른 경우에만 확인했던 사람을 다시 미확인으로 돌린다."""
    reset = list(
        await session.scalars(
            update(MentionAck)
            .where(MentionAck.post_id == post.id, MentionAck.acked_at.is_not(None))
            .values(acked_at=None)
            .returning(MentionAck.user_id)
        )
    )
    if reset:
        record_audit(
            session,
            actor_id=actor_id,
            action="mention.reconfirm",
            target_type="post",
            target_id=post.id,
            user_ids=sorted(reset),
        )
        await publish_counts(session, reset)


@router.post("/posts/{post_id}/ack", response_class=HTMLResponse)
async def acknowledge(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    """누른 시각을 남기고 취소할 수 없다. 같은 글을 두 번 눌러도 처음 시각이 유지된다."""
    post, parent, channel, access = await post_context(session, authz, post_id)
    if post.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "삭제된 글입니다.")
    target = await session.get(MentionAck, (post.id, authz.user.id))
    if target is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "읽음 확인 대상이 아닙니다.")
    acked = await session.scalar(
        update(MentionAck)
        .where(
            MentionAck.post_id == post.id,
            MentionAck.user_id == authz.user.id,
            MentionAck.acked_at.is_(None),
        )
        .values(acked_at=datetime.now(UTC))
        .returning(MentionAck.user_id)
    )
    if acked is not None:
        record_audit(
            session,
            actor_id=authz.user.id,
            action="mention.ack",
            target_type="post",
            target_id=post.id,
            channel_id=channel.id,
        )
        await publish(session, Event("updated", channel.id, post.id, authz.user.id))
        await publish_counts(session, [authz.user.id])
    await session.commit()
    return await card(session, authz, post, parent, channel, access)


@router.get("/channels/{channel_id}/mention-candidates")
async def mention_candidates(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    q: Annotated[str, Query(max_length=100)] = "",
) -> list[dict[str, object]]:
    """F-20 자동완성. 읽기 권한이 없는 사람은 고를 수 없게 표시한다(F-25)."""
    channel, _ = await channel_access(session, authz, channel_id)
    users = list(
        await session.scalars(
            select(User)
            .where(
                User.status == "active",
                User.id != authz.user.id,
                User.name.contains(q, autoescape=True),
            )
            .order_by(User.name, User.id)
            .limit(MENTION_CANDIDATES)
        )
    )
    departments = list(
        await session.scalars(
            select(Department)
            .where(Department.name.contains(q, autoescape=True))
            .order_by(Department.name, Department.id)
            .limit(MENTION_CANDIDATES)
        )
    )
    names = [row.name for row in (*users, *departments)]
    same_user_names = set(
        await session.scalars(
            select(User.name)
            .where(User.status == "active", User.name.in_(names))
            .group_by(User.name)
            .having(func.count() > 1)
        )
    )
    same_department_names = set(
        await session.scalars(
            select(Department.name)
            .where(Department.name.in_(names))
            .group_by(Department.name)
            .having(func.count() > 1)
        )
    )
    repeated = same_user_names | same_department_names
    repeated |= {row.name for row in users} & {row.name for row in departments}

    def insert_text(row: User | Department) -> str:
        return f"{row.name}#{row.id}" if row.name in repeated else row.name

    access = await access_for_users(session, channel, [row.id for row in users])
    return [
        *[
            {
                "label": row.name,
                "detail": row.email,
                "insert": insert_text(row),
                "disabled": not access[row.id].read,
            }
            for row in users
        ],
        *[
            {"label": row.name, "detail": "소속", "insert": insert_text(row), "disabled": False}
            for row in departments
        ],
    ][:MENTION_CANDIDATES]


@router.post("/posts/{post_id}/delete", response_class=HTMLResponse)
async def delete_post(post_id: int, session: SessionDep, authz: AuthzDep) -> HTMLResponse:
    post, parent, channel, access = await editable_view(session, authz, post_id, lock=True)
    if is_document(post, channel):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "등록한 문서는 삭제할 수 없습니다.")
    live_replies = await session.scalar(
        select(func.count())
        .select_from(Post)
        .where(Post.parent_post_id == post.id, Post.deleted_at.is_(None))
    )
    if live_replies:
        raise HTTPException(status.HTTP_409_CONFLICT, "답글이 남아 있어 삭제할 수 없습니다.")
    was_pinned = post.pinned_at is not None
    pending = await session.scalars(
        select(MentionAck.user_id).where(
            MentionAck.post_id == post.id, MentionAck.acked_at.is_(None)
        )
    )
    await publish_counts(session, list(pending))
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

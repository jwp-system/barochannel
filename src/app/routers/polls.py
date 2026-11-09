from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select

from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.core.templating import LOCAL_TZ, templates
from app.models import Poll, PollOption, PollVote, PollVoteHistory, Post
from app.routers.posts import (
    Body,
    Files,
    apply_mentions,
    card,
    channel_access,
    check_upload_size,
    not_found,
    post_context,
    store_attachments,
)
from app.services.navigation import build_navigation
from app.services.polls import MAX_OPTIONS, MIN_OPTIONS, is_closed
from app.services.realtime import Event, publish

router = APIRouter(tags=["polls"], include_in_schema=False, dependencies=[Depends(current_user)])


def parse_local(value: str) -> datetime:
    """화면의 datetime-local 값은 서울 시각이다."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "마감 시각을 확인해 주세요.") from error
    return parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed


@router.get("/channels/{channel_id}/polls/new", response_class=HTMLResponse)
async def new_poll(
    request: Request, channel_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "vote" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "투표를 올릴 수 없습니다.")
    return templates.TemplateResponse(
        request,
        "poll_new.html",
        {
            "user": authz.user,
            "navigation": await build_navigation(authz),
            "active_tab": "home",
            "channel": channel,
            "min_options": MIN_OPTIONS,
        },
    )


@router.post("/channels/{channel_id}/polls", dependencies=[Depends(check_upload_size)])
async def create_poll(
    channel_id: int,
    session: SessionDep,
    authz: AuthzDep,
    body: Body,
    closes_at: Annotated[str, Form()],
    options: Annotated[list[str], Form()],
    multi_select: Annotated[bool, Form()] = False,
    anonymous: Annotated[bool, Form()] = False,
    result_open: Annotated[Literal["immediate", "after_close"], Form()] = "immediate",
    files: Files = None,
) -> Response:
    channel, access = await channel_access(session, authz, channel_id)
    if channel.type != "vote" or not access.write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "투표를 올릴 수 없습니다.")
    if not body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "안건을 입력해 주세요.")
    labels = [label.strip() for label in options if label.strip()]
    if not MIN_OPTIONS <= len(labels) <= MAX_OPTIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"선택지는 {MIN_OPTIONS}개부터 {MAX_OPTIONS}개까지 넣을 수 있습니다.",
        )
    if len(set(labels)) != len(labels) or any(len(label) > 200 for label in labels):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "선택지가 겹치거나 너무 깁니다.")
    deadline = parse_local(closes_at)
    if deadline <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "마감 시각은 지금 이후여야 합니다.")

    post = Post(channel_id=channel.id, author_id=authz.user.id, body_md=body)
    session.add(post)
    await session.flush()
    session.add(
        Poll(
            post_id=post.id,
            multi_select=multi_select,
            anonymous=anonymous,
            result_open=result_open,
            closes_at=deadline,
        )
    )
    await session.flush()
    session.add_all(
        PollOption(post_id=post.id, position=number, label=label)
        for number, label in enumerate(labels, start=1)
    )
    await apply_mentions(session, post, None, channel)
    await store_attachments(session, post, files or [])
    await publish(session, Event("created", channel.id, post.id, authz.user.id))
    await session.commit()
    return Response(headers={"HX-Redirect": f"/channels/{channel.id}"})


@router.post("/posts/{post_id}/vote", response_class=HTMLResponse)
async def vote(
    post_id: int,
    session: SessionDep,
    authz: AuthzDep,
    option_ids: Annotated[list[int] | None, Form()] = None,
) -> HTMLResponse:
    """선택 전체를 새로 보낸다. 같은 사람의 동시 요청이 겹치지 않게 투표 행을 잠근다."""
    post, parent, channel, access = await post_context(session, authz, post_id)
    poll = await session.scalar(
        select(Poll)
        .where(Poll.post_id == post.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if poll is None or parent is not None:
        raise not_found()
    if not access.read:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "읽기 권한자만 투표할 수 있습니다.")
    if post.deleted_at is not None or is_closed(poll):
        raise HTTPException(status.HTTP_409_CONFLICT, "마감된 투표입니다.")
    chosen = sorted(set(option_ids or []))
    valid = set(await session.scalars(select(PollOption.id).where(PollOption.post_id == post.id)))
    if not chosen or not set(chosen) <= valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "선택지를 골라 주세요.")
    if len(chosen) > 1 and not poll.multi_select:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "하나만 고를 수 있습니다.")
    before = sorted(
        await session.scalars(
            select(PollVote.option_id).where(
                PollVote.post_id == post.id, PollVote.user_id == authz.user.id
            )
        )
    )
    if before != chosen:
        await session.execute(
            delete(PollVote).where(PollVote.post_id == post.id, PollVote.user_id == authz.user.id)
        )
        session.add_all(
            PollVote(post_id=post.id, user_id=authz.user.id, option_id=option_id)
            for option_id in chosen
        )
        session.add(
            PollVoteHistory(
                post_id=post.id,
                user_id=authz.user.id,
                before_option_ids=before,
                after_option_ids=chosen,
            )
        )
        await publish(session, Event("updated", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, parent, channel, access)

"""F-64~68: 완료 결재의 발신과 수신.

발신하면 수신 채널에 '수신 대기' 결재가 생긴다. 수신 완료를 체크한 사람이 기안자가 되고
그 사람 소속의 승인선으로 일반 결재가 이어진다. 한 문서의 발신 사슬은 root_approval_id로 묶어
이미 거친 채널로 되돌아가지 않게 한다.
"""

from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import format_time
from app.models import (
    Approval,
    ApprovalDispatch,
    ApprovalStep,
    Attachment,
    Channel,
    ChannelDispatchTarget,
    Post,
    User,
    WorkspaceGroup,
)
from app.services.approvals import ApprovalRow, step_label
from app.services.audit import record_audit
from app.services.permissions import Authorizer, access_for_users
from app.services.realtime import Event, publish, publish_counts
from app.services.storage import copy_file


async def copy_attachments(session: AsyncSession, source_post_id: int, target_post_id: int) -> None:
    """글마다 파일을 따로 둔다. 한쪽을 지워도 다른 쪽의 보존에 영향이 없다."""
    rows = await session.scalars(
        select(Attachment).where(
            Attachment.post_id == source_post_id, Attachment.deleted_at.is_(None)
        )
    )
    for row in list(rows):
        key = f"attachments/{uuid4().hex}"
        await copy_file(row.storage_key, key)
        session.add(
            Attachment(
                post_id=target_post_id,
                file_name=row.file_name,
                storage_key=key,
                size=row.size,
                mime=row.mime,
            )
        )


async def document_body(
    session: AsyncSession, approval: Approval, post: Post, steps: Sequence[ApprovalStep]
) -> str:
    """게시와 발신이 함께 쓰는 사본. 제목, 본문, 승인선 결과, 원본 링크를 담는다."""
    names = dict(
        (
            await session.execute(
                select(User.id, User.name).where(User.id.in_([s.approver_id for s in steps]))
            )
        ).all()
    )
    last = max(step.step_no for step in steps)
    line = " → ".join(
        f"{step_label(step.step_no, last)} {names.get(step.approver_id, '')} "
        f"({format_time(step.acted_at)})"
        for step in steps
    )
    return (
        f"## {approval.title}\n\n{post.body_md}\n\n---\n\n"
        f"승인선: {line}\n\n[원본 결재](/approvals/{post.id})"
    )


async def chain_root(session: AsyncSession, approval_id: int) -> int:
    root = await session.scalar(
        select(ApprovalDispatch.root_approval_id).where(
            ApprovalDispatch.received_approval_id == approval_id
        )
    )
    return root or approval_id


async def chain_channels(session: AsyncSession, root_id: int) -> set[int]:
    """사슬이 이미 지나간 채널. 처음 결재 채널, 발신한 채널, 수신한 채널이 모두 들어간다."""
    dispatches = list(
        await session.scalars(
            select(ApprovalDispatch).where(ApprovalDispatch.root_approval_id == root_id)
        )
    )
    approvals = {root_id} | {row.source_approval_id for row in dispatches}
    channels = set(
        await session.scalars(
            select(Post.channel_id)
            .join(Approval, Approval.post_id == Post.id)
            .where(Approval.id.in_(approvals))
        )
    )
    return channels | {row.target_channel_id for row in dispatches}


async def available_targets(
    session: AsyncSession, approval: Approval, channel_id: int
) -> list[tuple[Channel, bool]]:
    excluded = await chain_channels(session, await chain_root(session, approval.id))
    rows = await session.execute(
        select(Channel, ChannelDispatchTarget.auto_send)
        .join(ChannelDispatchTarget, ChannelDispatchTarget.target_channel_id == Channel.id)
        .join(WorkspaceGroup, WorkspaceGroup.id == Channel.group_id)
        .where(
            ChannelDispatchTarget.source_channel_id == channel_id,
            Channel.type == "approval",
            WorkspaceGroup.archived_at.is_(None),
        )
        .order_by(Channel.name, Channel.id)
    )
    return [(channel, auto) for channel, auto in rows if channel.id not in excluded]


async def receivers(session: AsyncSession, channel: Channel) -> set[int]:
    """수신 완료를 체크할 수 있는 사람. 읽기와 쓰기가 모두 있어야 한다."""
    users = list(await session.scalars(select(User.id).where(User.status == "active")))
    access = await access_for_users(session, channel, users)
    return {user_id for user_id, row in access.items() if row.read and row.write}


async def send(
    session: AsyncSession,
    approval: Approval,
    post: Post,
    steps: Sequence[ApprovalStep],
    target: Channel,
    actor_id: int | None,
) -> None:
    """actor_id가 없으면 자동 발신이다."""
    root = await chain_root(session, approval.id)
    copy = Post(
        channel_id=target.id,
        author_id=approval.drafter_id,
        body_md=await document_body(session, approval, post, steps),
    )
    session.add(copy)
    await session.flush()
    received = Approval(
        post_id=copy.id,
        drafter_id=approval.drafter_id,
        title=approval.title,
        status="receiving",
        received=True,
    )
    session.add(received)
    await session.flush()
    await copy_attachments(session, post.id, copy.id)
    session.add(
        ApprovalDispatch(
            root_approval_id=root,
            source_approval_id=approval.id,
            target_channel_id=target.id,
            send_type="manual" if actor_id else "auto",
            sent_by=actor_id,
            received_approval_id=received.id,
        )
    )
    record_audit(
        session,
        actor_id=actor_id,
        action="approval.dispatch",
        target_type="approval",
        target_id=approval.id,
        post_id=post.id,
        channel_id=target.id,
        received_post_id=copy.id,
        send_type="manual" if actor_id else "auto",
    )
    await publish(session, Event("created", target.id, copy.id, actor_id or approval.drafter_id))
    await publish_counts(session, await receivers(session, target))


async def auto_dispatch(
    session: AsyncSession, approval: Approval, post: Post, steps: Sequence[ApprovalStep]
) -> None:
    for target, auto in await available_targets(session, approval, post.channel_id):
        if auto:
            await send(session, approval, post, steps, target, None)


async def receiving_for(session: AsyncSession, authz: Authorizer) -> list[ApprovalRow]:
    rows = await session.execute(
        select(Approval, Channel, User.name)
        .join(Post, Post.id == Approval.post_id)
        .join(Channel, Channel.id == Post.channel_id)
        .join(User, User.id == Approval.drafter_id)
        .where(Approval.status == "receiving", Post.deleted_at.is_(None))
        .order_by(Approval.id)
    )
    waiting = []
    for approval, channel, sender in rows:
        access = await authz.access(channel)
        if access.read and access.write:
            waiting.append(
                ApprovalRow(
                    approval.post_id, approval.title, "수신 대기", channel.name, sender, None
                )
            )
    return waiting

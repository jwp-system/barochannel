"""F-64~68: 완료 결재의 발신과 수신."""

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import (
    Approval,
    ApprovalDispatch,
    ApprovalLineTemplate,
    ApprovalStep,
    Attachment,
    AuditLog,
    Channel,
    User,
)
from app.services.delivery import todo_count
from tests.conftest import (
    FULL,
    add_channel,
    add_department,
    add_group,
    add_user,
    browser,
    grant,
    join,
    run,
)


@dataclass
class World:
    sales: Channel
    finance: Channel
    sales_manager: User
    finance_manager: User
    drafter: User
    sales_boss: User
    clerk: User
    clerk2: User
    finance_boss: User
    finance_team: object


async def _add(*rows) -> None:
    async with SessionLocal() as session:
        session.add_all(rows)
        await session.commit()


def set_line(channel, department, *approvers) -> None:
    run(
        _add(
            *[
                ApprovalLineTemplate(
                    channel_id=channel.id,
                    department_id=department.id,
                    step_no=number,
                    approver_id=user.id,
                )
                for number, user in enumerate(approvers, start=1)
            ]
        )
    )


@pytest.fixture
def world() -> World:
    sales_manager, finance_manager = add_user("sales-manager"), add_user("finance-manager")
    sales = add_channel(add_group(sales_manager, "영업"), "지출결의", "approval")
    finance = add_channel(add_group(finance_manager, "재무"), "재무결의", "approval")
    sales_team, finance_team = add_department("영업팀"), add_department("재무팀")
    drafter, sales_boss = add_user("drafter"), add_user("sales-boss")
    clerk, clerk2, finance_boss = add_user("clerk"), add_user("clerk2"), add_user("finance-boss")
    join(drafter, sales_team)
    for user in (clerk, clerk2):
        join(user, finance_team)
        grant(finance, user, **FULL)
    grant(sales, drafter, **FULL)
    set_line(sales, sales_team, sales_boss)
    set_line(finance, finance_team, finance_boss)
    return World(
        sales,
        finance,
        sales_manager,
        finance_manager,
        drafter,
        sales_boss,
        clerk,
        clerk2,
        finance_boss,
        finance_team,
    )


def target(world: World, source=None, target_channel=None, auto=False, manager=None):
    source = source or world.sales
    data = {"target_channel_id": (target_channel or world.finance).id}
    if auto:
        data["auto_send"] = "true"
    return browser(manager or world.sales_manager).post(
        f"/channels/{source.id}/dispatch-targets", data=data
    )


def approved(world: World, files=None) -> tuple[object, int]:
    author = browser(world.drafter)
    response = author.post(
        f"/channels/{world.sales.id}/approvals",
        data={"title": "부스 비용", "body": "100만원"},
        files=files,
    )
    post = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    author.post(f"/approvals/{post}/submit")
    assert browser(world.sales_boss).post(f"/approvals/{post}/approve").status_code == 200
    return author, post


def received_posts(channel: Channel) -> list[Approval]:
    async def load() -> list[Approval]:
        async with SessionLocal() as session:
            from app.models import Post

            rows = await session.scalars(
                select(Approval)
                .join(Post, Post.id == Approval.post_id)
                .where(Post.channel_id == channel.id)
                .order_by(Approval.id)
            )
            return list(rows)

    return run(load())


def dispatches() -> list[ApprovalDispatch]:
    async def load() -> list[ApprovalDispatch]:
        async with SessionLocal() as session:
            return list(
                await session.scalars(select(ApprovalDispatch).order_by(ApprovalDispatch.id))
            )

    return run(load())


def send(author, post: int, *channels: Channel):
    return author.post(
        f"/approvals/{post}/dispatch", data={"channel_ids": [channel.id for channel in channels]}
    )


# 대상 지정


def test_only_source_group_manager_sets_targets(world: World) -> None:
    assert target(world, manager=world.finance_manager).status_code == 403
    assert target(world).status_code == 200
    assert target(world, auto=True).status_code == 200
    page = browser(world.sales_manager).get(f"/channels/{world.sales.id}/settings").text
    assert "# 재무결의" in page and "checked" in page

    async def actions() -> list[str]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(AuditLog.action)))

    assert run(actions()).count("channel.dispatch_target") == 2


def test_target_must_be_another_approval_channel(world: World) -> None:
    talk = add_channel(add_group(world.finance_manager, "소통"), "공지")
    assert target(world, target_channel=talk).status_code == 400
    assert target(world, target_channel=world.sales).status_code == 400


def test_removed_target_cannot_receive(world: World) -> None:
    target(world)
    browser(world.sales_manager).post(
        f"/channels/{world.sales.id}/dispatch-targets",
        data={"target_channel_id": world.finance.id, "remove": "true"},
    )
    author, post = approved(world)
    assert send(author, post, world.finance).status_code == 400


# 발신


def test_drafter_sends_completed_approval_with_copy(world: World) -> None:
    target(world)
    author, post = approved(world, files=[("files", ("견적.txt", io.BytesIO(b"x")))])
    assert "다른 결재 채널로 발신" in author.get(f"/approvals/{post}").text
    assert send(author, post, world.finance).status_code == 200
    (received,) = received_posts(world.finance)
    assert received.status == "receiving" and received.received and received.title == "부스 비용"
    page = browser(world.clerk).get(f"/approvals/{received.post_id}").text
    assert "100만원" in page and "승인선:" in page and f"/approvals/{post}" in page
    assert "견적.txt" in page and "지출결의 채널에서 발신한 문서" in page
    assert "발신 # 재무결의: 수신 대기" in author.get(f"/approvals/{post}").text

    async def files() -> list[str]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(Attachment.storage_key)))

    first, copied = run(files())
    assert first != copied
    (row,) = dispatches()
    assert row.send_type == "manual" and row.sent_by == world.drafter.id


def test_only_drafter_sends_after_final_approval(world: World) -> None:
    target(world)
    author = browser(world.drafter)
    response = author.post(
        f"/channels/{world.sales.id}/approvals", data={"title": "x", "body": "y"}
    )
    post = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    author.post(f"/approvals/{post}/submit")
    assert send(author, post, world.finance).status_code == 409
    browser(world.sales_boss).post(f"/approvals/{post}/approve")
    assert send(browser(world.sales_boss), post, world.finance).status_code == 409
    assert send(author, post, world.finance).status_code == 200


def test_same_channel_is_never_sent_twice(world: World) -> None:
    target(world)
    author, post = approved(world)
    assert send(author, post, world.finance).status_code == 200
    assert send(author, post, world.finance).status_code == 400
    assert len(dispatches()) == 1


def test_many_channels_at_once(world: World) -> None:
    other = add_channel(add_group(world.finance_manager, "감사"), "감사결의", "approval")
    target(world)
    target(world, target_channel=other)
    author, post = approved(world)
    assert send(author, post, world.finance, other).status_code == 200
    assert len(received_posts(world.finance)) == 1 and len(received_posts(other)) == 1


def test_auto_target_receives_on_final_approval(world: World) -> None:
    target(world, auto=True)
    approved(world)
    (received,) = received_posts(world.finance)
    assert received.status == "receiving"
    (row,) = dispatches()
    assert row.send_type == "auto" and row.sent_by is None


# 수신 대기


def test_receiving_count_is_for_readers_who_can_write(world: World) -> None:
    reader, writer = add_user("reader"), add_user("writer")
    grant(world.finance, reader, read="allow")
    grant(world.finance, writer, write="allow")
    target(world, auto=True)
    approved(world)
    (received,) = received_posts(world.finance)
    assert run(todo_count(world.clerk.id)) == 1
    assert run(todo_count(reader.id)) == 0 and run(todo_count(writer.id)) == 0
    assert "수신 대기" in browser(world.clerk).get("/todo").text
    assert browser(reader).get(f"/approvals/{received.post_id}").status_code == 200
    assert browser(writer).get(f"/approvals/{received.post_id}").status_code == 404
    url = f"/approvals/{received.post_id}/receive"
    assert browser(reader).post(url).status_code == 403
    assert browser(writer).post(url).status_code == 404


# 수신 완료 체크


def receive(world: World, user: User | None = None):
    (received,) = received_posts(world.finance)
    response = browser(user or world.clerk).post(f"/approvals/{received.post_id}/receive")
    return response, received.post_id


def steps(post: int) -> list[int]:
    async def load() -> list[int]:
        async with SessionLocal() as session:
            approval = await session.scalar(select(Approval).where(Approval.post_id == post))
            rows = await session.scalars(
                select(ApprovalStep.approver_id)
                .where(ApprovalStep.approval_id == approval.id)
                .order_by(ApprovalStep.step_no)
            )
            return list(rows)

    return run(load())


def test_checker_becomes_drafter_with_own_line(world: World) -> None:
    target(world, auto=True)
    author, post = approved(world)
    response, received = receive(world)
    assert response.status_code == 200
    (approval,) = received_posts(world.finance)
    assert approval.status == "in_progress" and approval.drafter_id == world.clerk.id
    assert steps(received) == [world.clerk.id, world.finance_boss.id]
    assert run(todo_count(world.clerk2.id)) == 0
    assert run(todo_count(world.finance_boss.id)) == 1
    assert "발신 # 재무결의: 결재 중" in author.get(f"/approvals/{post}").text
    (row,) = dispatches()
    assert row.received_by == world.clerk.id and row.received_at is not None


def test_check_is_blocked_without_any_line(world: World) -> None:
    loner = add_user("loner")
    join(loner, add_department("승인선 없는 팀"))
    grant(world.finance, loner, **FULL)
    target(world, auto=True)
    approved(world)
    response, _ = receive(world, loner)
    assert response.status_code == 409
    assert received_posts(world.finance)[0].status == "receiving"


def test_check_uses_parent_department_line(world: World) -> None:
    child = add_department("재무1파트", world.finance_team)
    member = add_user("member")
    join(member, child)
    grant(world.finance, member, **FULL)
    target(world, auto=True)
    approved(world)
    response, received = receive(world, member)
    assert response.status_code == 200
    assert steps(received) == [member.id, world.finance_boss.id]


def test_final_approver_can_check_and_approves_last(world: World) -> None:
    join(world.finance_boss, world.finance_team)
    grant(world.finance, world.finance_boss, **FULL)
    target(world, auto=True)
    approved(world)
    response, received = receive(world, world.finance_boss)
    assert response.status_code == 200
    assert steps(received) == [world.finance_boss.id, world.finance_boss.id]
    boss = browser(world.finance_boss)
    assert boss.post(f"/approvals/{received}/approve").status_code == 200


def test_simultaneous_checks_only_one_wins(world: World) -> None:
    target(world, auto=True)
    approved(world)
    (received,) = received_posts(world.finance)
    url = f"/approvals/{received.post_id}/receive"
    first, second = browser(world.clerk), browser(world.clerk2)
    with ThreadPoolExecutor(2) as pool:
        results = [pool.submit(first.post, url), pool.submit(second.post, url)]
        codes = sorted(future.result().status_code for future in results)
    assert codes == [200, 409]
    assert len(steps(received.post_id)) == 2


def test_received_approval_is_locked_and_has_no_withdraw(world: World) -> None:
    target(world, auto=True)
    approved(world)
    _, received = receive(world)
    clerk = browser(world.clerk)
    assert clerk.post(f"/posts/{received}/edit", data={"body": "변조"}).status_code == 403
    assert clerk.post(f"/approvals/{received}/withdraw").status_code == 409
    assert "기안 취소</button>" not in clerk.get(f"/approvals/{received}").text
    assert clerk.post(f"/posts/{received}/replies", data={"body": "의견"}).status_code == 200


def test_rejected_receipt_keeps_source_completed(world: World) -> None:
    target(world, auto=True)
    author, post = approved(world)
    _, received = receive(world)
    boss = browser(world.finance_boss)
    assert (
        boss.post(f"/approvals/{received}/reject", data={"comment": "증빙 부족"}).status_code == 200
    )
    (approval,) = received_posts(world.finance)
    assert approval.status == "rejected"
    page = author.get(f"/approvals/{post}").text
    assert "승인 완료" in page and "발신 # 재무결의: 반려" in page


# 다시 발신


def test_redispatch_never_returns_to_visited_channels(world: World) -> None:
    audit = add_channel(add_group(world.finance_manager, "감사"), "감사결의", "approval")
    target(world, auto=True)
    target(
        world,
        source=world.finance,
        target_channel=world.sales,
        auto=True,
        manager=world.finance_manager,
    )
    target(world, source=world.finance, target_channel=audit, manager=world.finance_manager)
    approved(world)
    _, received = receive(world)
    browser(world.finance_boss).post(f"/approvals/{received}/approve")
    assert len(received_posts(world.sales)) == 1, "자동 발신이 원래 채널로 되돌아갔다"

    clerk = browser(world.clerk)
    page = clerk.get(f"/approvals/{received}").text
    assert f'name="channel_ids" value="{audit.id}"' in page
    assert f'name="channel_ids" value="{world.sales.id}"' not in page
    assert send(clerk, received, world.sales).status_code == 400
    assert send(clerk, received, audit).status_code == 200
    rows = dispatches()
    assert len(rows) == 2 and rows[1].root_approval_id == rows[0].root_approval_id


def test_completed_receipt_can_be_published(world: World) -> None:
    talk = add_channel(add_group(world.finance_manager, "소통"), "공지")
    grant(talk, world.clerk, **FULL)
    target(world, auto=True)
    approved(world)
    _, received = receive(world)
    browser(world.finance_boss).post(f"/approvals/{received}/approve")
    response = browser(world.clerk).post(
        f"/approvals/{received}/publish", data={"channel_id": talk.id}
    )
    assert response.status_code == 200

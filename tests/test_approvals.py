"""관문 2: 결재 규칙. 기획서의 상태 흐름과 금지 규칙을 그대로 옮겼다."""

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models import (
    Approval,
    ApprovalLineTemplate,
    ApprovalStep,
    Attachment,
    AuditLog,
    Channel,
    User,
    UserDepartment,
)
from app.services.approvals import retain_until
from app.services.delivery import messages_for, todo_count
from app.services.permissions import Authorizer, access_for_users
from app.services.realtime import Event
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
class Office:
    channel: Channel
    team: object
    manager: User
    drafter: User
    first: User
    final: User
    reader: User


async def _add(*rows) -> None:
    async with SessionLocal() as session:
        session.add_all(rows)
        await session.commit()


def set_line(channel, department, *approvers: User) -> None:
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
def office() -> Office:
    manager = add_user("manager")
    channel = add_channel(add_group(manager), "지출결의", "approval")
    team = add_department("영업팀")
    drafter, first, final, reader = (
        add_user(name) for name in ("drafter", "first", "final", "reader")
    )
    join(drafter, team)
    grant(channel, drafter, read="allow", write="allow", reply_read="allow", reply_write="allow")
    grant(channel, reader, read="allow", reply_read="allow")
    set_line(channel, team, first, final)
    return Office(channel, team, manager, drafter, first, final, reader)


def draft(signed_in, office: Office, body="본문", **data) -> int:
    response = signed_in.post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "부스 비용", "body": body, **data},
    )
    assert response.status_code == 200, response.text
    return int(response.headers["HX-Redirect"].rsplit("/", 1)[1])


def submitted(office: Office, **data) -> tuple[object, int]:
    signed_in = browser(office.drafter)
    post = draft(signed_in, office)
    response = signed_in.post(f"/approvals/{post}/submit", data=data)
    assert response.status_code == 200, response.text
    return signed_in, post


def state(post: int) -> Approval:
    async def load() -> Approval:
        async with SessionLocal() as session:
            return await session.scalar(select(Approval).where(Approval.post_id == post))

    return run(load())


def steps(post: int) -> list[ApprovalStep]:
    async def load() -> list[ApprovalStep]:
        async with SessionLocal() as session:
            approval = await session.scalar(select(Approval).where(Approval.post_id == post))
            rows = await session.scalars(
                select(ApprovalStep)
                .where(ApprovalStep.approval_id == approval.id)
                .order_by(ApprovalStep.step_no)
            )
            return list(rows)

    return run(load())


def audit_actions() -> list[str]:
    async def load() -> list[str]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(AuditLog.action).order_by(AuditLog.id)))

    return run(load())


# 임시저장


def test_draft_is_visible_only_to_author(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office, body="작성 중인 비밀")
    assert "작성 중인 비밀" in author.get(f"/channels/{office.channel.id}").text
    reader = browser(office.reader)
    assert "작성 중인 비밀" not in reader.get(f"/channels/{office.channel.id}").text
    assert reader.get(f"/approvals/{post}").status_code == 404
    assert run(messages_for(Event("updated", office.channel.id, post, 1), office.reader.id)) == []


def test_draft_can_be_edited_and_deleted(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office)
    assert (
        author.post(f"/posts/{post}/edit", data={"body": "고침", "title": "새 제목"}).status_code
        == 200
    )
    assert state(post).title == "새 제목"
    assert author.post(f"/posts/{post}/delete").status_code == 200


def test_new_form_uses_channel_template(office: Office) -> None:
    async def set_template() -> None:
        async with SessionLocal() as session:
            (await session.get(Channel, office.channel.id)).approval_template_md = "## 형식틀"
            await session.commit()

    run(set_template())
    page = browser(office.drafter).get(f"/channels/{office.channel.id}/approvals/new")
    assert "## 형식틀" in page.text


def test_reader_without_write_cannot_draft(office: Office) -> None:
    response = browser(office.reader).post(
        f"/channels/{office.channel.id}/approvals", data={"title": "x", "body": "y"}
    )
    assert response.status_code == 403


def test_talk_channel_rejects_approval(office: Office) -> None:
    channel = add_channel(add_group(office.manager, "다른"), "일반")
    grant(channel, office.drafter, **FULL)
    response = browser(office.drafter).post(
        f"/channels/{channel.id}/approvals", data={"title": "x", "body": "y"}
    )
    assert response.status_code == 403


def test_preview_shows_line_before_submit(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office)
    assert "first → final" in author.get(f"/approvals/{post}").text


# 승인선 결정


def approvers_of(post: int) -> list[int]:
    return [step.approver_id for step in steps(post)]


def test_submit_copies_designated_department_line(office: Office) -> None:
    other = add_department("겸직팀")
    join(office.drafter, other)
    alternate = add_user("alternate")
    set_line(office.channel, other, alternate, office.final)
    signed_in = browser(office.drafter)
    post = draft(signed_in, office, department_id=other.id)
    signed_in.post(f"/approvals/{post}/submit")
    assert approvers_of(post) == [office.drafter.id, alternate.id, office.final.id]
    assert state(post).department_id == other.id


def test_department_must_be_drafters_own(office: Office) -> None:
    foreign = add_department("남의 팀")
    response = browser(office.drafter).post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "x", "body": "y", "department_id": foreign.id},
    )
    assert response.status_code == 400


def test_line_falls_back_to_parent_department(office: Office) -> None:
    parent = add_department("본부")
    child = add_department("하위팀", parent)
    member = add_user("member")
    join(member, child)
    grant(office.channel, member, read="allow", write="allow")
    head = add_user("head")
    set_line(office.channel, parent, head, office.final)
    signed_in = browser(member)
    post = draft(signed_in, office)
    signed_in.post(f"/approvals/{post}/submit")
    assert approvers_of(post) == [member.id, head.id, office.final.id]


def test_no_line_makes_drafter_the_final_approver(office: Office) -> None:
    lonely = add_department("승인선 없는 팀")
    member = add_user("member")
    join(member, lonely)
    grant(office.channel, member, read="allow", write="allow")
    signed_in = browser(member)
    post = draft(signed_in, office)
    assert signed_in.post(f"/approvals/{post}/submit").status_code == 200
    assert approvers_of(post) == [member.id, member.id]
    assert state(post).status == "in_progress"
    assert signed_in.post(f"/approvals/{post}/approve").status_code == 200
    assert state(post).status == "approved"


def test_drafter_as_registered_final_approves_after_earlier_steps(office: Office) -> None:
    team = add_department("자기팀")
    boss = add_user("boss")
    join(boss, team)
    grant(office.channel, boss, read="allow", write="allow")
    set_line(office.channel, team, office.first, boss)
    signed_in = browser(boss)
    post = draft(signed_in, office)
    assert "first → boss" in signed_in.get(f"/approvals/{post}").text
    assert signed_in.post(f"/approvals/{post}/submit").status_code == 200
    assert approvers_of(post) == [boss.id, office.first.id, boss.id]

    assert signed_in.post(f"/approvals/{post}/approve").status_code == 409
    assert browser(office.first).post(f"/approvals/{post}/approve").status_code == 200
    assert signed_in.post(f"/approvals/{post}/approve").status_code == 200
    assert state(post).status == "approved"


def test_drafter_as_only_registered_step_approves_alone(office: Office) -> None:
    team = add_department("혼자팀")
    boss = add_user("boss")
    join(boss, team)
    grant(office.channel, boss, read="allow", write="allow")
    set_line(office.channel, team, boss)
    signed_in = browser(boss)
    post = draft(signed_in, office)
    signed_in.post(f"/approvals/{post}/submit")
    assert signed_in.post(f"/approvals/{post}/approve").status_code == 200
    assert state(post).status == "approved"


def test_review_also_blocks_drafter_as_final(office: Office) -> None:
    team = add_department("자기팀")
    boss = add_user("boss")
    reviewer = add_user("reviewer")
    join(boss, team)
    grant(office.channel, boss, read="allow", write="allow")
    set_line(office.channel, team, boss)
    signed_in = browser(boss)
    post = draft(signed_in, office)
    signed_in.post(f"/approvals/{post}/submit", data={"reviewer_ids": [reviewer.id]})
    assert signed_in.post(f"/approvals/{post}/approve").status_code == 409
    browser(reviewer).post(f"/approvals/{post}/review", data={"result": "pass"})
    assert signed_in.post(f"/approvals/{post}/approve").status_code == 200


def test_drafter_in_middle_of_line_is_skipped(office: Office) -> None:
    team = add_department("중간팀")
    middle = add_user("middle")
    join(middle, team)
    grant(office.channel, middle, read="allow", write="allow")
    set_line(office.channel, team, office.first, middle, office.final)
    signed_in = browser(middle)
    post = draft(signed_in, office)
    signed_in.post(f"/approvals/{post}/submit")
    assert approvers_of(post) == [middle.id, office.first.id, office.final.id]


def test_inactive_approver_blocks_submit(office: Office) -> None:
    async def retire() -> None:
        async with SessionLocal() as session:
            (await session.get(User, office.first.id)).status = "inactive"
            await session.commit()

    run(retire())
    signed_in = browser(office.drafter)
    post = draft(signed_in, office)
    assert signed_in.post(f"/approvals/{post}/submit").status_code == 409


def test_changing_template_does_not_touch_running_approval(office: Office) -> None:
    _, post = submitted(office)
    signed_in = browser(office.manager)
    signed_in.post(
        f"/channels/{office.channel.id}/approval-lines",
        data={"department_id": office.team.id, "approvers": office.final.email},
    )
    assert approvers_of(post) == [office.drafter.id, office.first.id, office.final.id]
    assert browser(office.first).post(f"/approvals/{post}/approve").status_code == 200


# 순차 승인


def test_full_sequence_completes_and_is_retained(office: Office) -> None:
    _, post = submitted(office)
    assert browser(office.first).post(f"/approvals/{post}/approve").status_code == 200
    assert state(post).current_step == 2
    assert browser(office.final).post(f"/approvals/{post}/approve").status_code == 200
    final = state(post)
    assert final.status == "approved" and final.closed_at is not None
    assert final.retain_until == retain_until(final.closed_at)
    assert audit_actions() == ["approval.submit", "approval.approve", "approval.approve"]


def test_retention_is_five_years_even_from_leap_day() -> None:
    assert retain_until(datetime(2028, 2, 29, tzinfo=UTC)) == datetime(2033, 2, 28, tzinfo=UTC)


def test_out_of_turn_approval_is_rejected(office: Office) -> None:
    _, post = submitted(office)
    assert browser(office.final).post(f"/approvals/{post}/approve").status_code == 409
    assert browser(office.drafter).post(f"/approvals/{post}/approve").status_code == 409
    assert browser(office.reader).post(f"/approvals/{post}/approve").status_code == 409
    assert state(post).current_step == 1


def test_button_only_for_current_approver(office: Office) -> None:
    _, post = submitted(office)
    url = f"/approvals/{post}"
    assert f'hx-post="{url}/approve"' in browser(office.first).get(url).text
    assert f'hx-post="{url}/approve"' not in browser(office.final).get(url).text


def test_approver_gets_all_permissions_despite_deny(office: Office) -> None:
    grant(office.channel, office.first, write="deny", read="deny")

    async def check() -> None:
        async with SessionLocal() as session:
            channel = await session.get(Channel, office.channel.id)
            user = await session.get(User, office.first.id)
            single = await Authorizer(session, user).access(channel)
            batch = await access_for_users(session, channel, [user.id])
            assert single.read and single.write and single.reply_read and single.reply_write
            assert batch[user.id] == single

    run(check())


def test_replaced_approver_keeps_running_document(office: Office) -> None:
    _, post = submitted(office)
    browser(office.manager).post(
        f"/channels/{office.channel.id}/approval-lines",
        data={"department_id": office.team.id, "approvers": ""},
    )
    assert browser(office.first).get(f"/approvals/{post}").status_code == 200


def test_drafter_own_step_grants_no_channel_access() -> None:
    manager = add_user("manager")
    channel = add_channel(add_group(manager), "결재", "approval")
    member = add_user("member")
    other = add_user("other")
    grant(channel, member, write="allow")
    grant(channel, other, read="allow", write="allow")
    office = Office(channel, None, manager, member, member, member, other)
    signed_in = browser(member)
    post = draft(signed_in, office)
    signed_in.post(f"/approvals/{post}/submit")

    theirs = draft(browser(other), office)
    browser(other).post(f"/approvals/{theirs}/submit")
    assert signed_in.get(f"/approvals/{theirs}").status_code == 404


# 본문 잠금


def test_submitted_body_and_attachments_are_locked(office: Office) -> None:
    signed_in = browser(office.drafter)
    response = signed_in.post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "x", "body": "본문"},
        files=[("files", ("a.txt", io.BytesIO(b"x")))],
    )
    post = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    signed_in.post(f"/approvals/{post}/submit")
    assert signed_in.post(f"/posts/{post}/edit", data={"body": "변조"}).status_code == 403
    assert signed_in.post(f"/posts/{post}/delete").status_code == 403
    assert signed_in.post("/attachments/1/delete").status_code == 403


def test_supplement_goes_to_thread(office: Office) -> None:
    signed_in, post = submitted(office)
    response = signed_in.post(f"/posts/{post}/replies", data={"body": "보완 자료"})
    assert response.status_code == 200


def test_mentions_activate_on_submit_not_on_draft(office: Office) -> None:
    signed_in = browser(office.drafter)
    post = draft(signed_in, office, body="@reader 참고")
    assert run(todo_count(office.reader.id)) == 0
    signed_in.post(f"/approvals/{post}/submit")
    assert run(todo_count(office.reader.id)) == 1


# 검토


def test_review_blocks_only_final_approval(office: Office) -> None:
    reviewer = add_user("reviewer")
    _, post = submitted(office, reviewer_ids=[reviewer.id])
    assert browser(office.first).post(f"/approvals/{post}/approve").status_code == 200
    final = browser(office.final)
    assert final.post(f"/approvals/{post}/approve").status_code == 409
    assert "검토 대기 중" in final.get(f"/approvals/{post}").text

    response = browser(reviewer).post(f"/approvals/{post}/review", data={"result": "fail"})
    assert response.status_code == 200
    assert final.post(f"/approvals/{post}/approve").status_code == 200
    assert state(post).status == "approved"


def test_reviewer_can_change_result_until_final_and_it_is_audited(office: Office) -> None:
    reviewer = add_user("reviewer")
    _, post = submitted(office, reviewer_ids=[reviewer.id])
    signed_in = browser(reviewer)
    signed_in.post(f"/approvals/{post}/review", data={"result": "revise"})
    signed_in.post(f"/approvals/{post}/review", data={"result": "pass"})
    assert audit_actions().count("approval.review") == 2

    browser(office.first).post(f"/approvals/{post}/approve")
    browser(office.final).post(f"/approvals/{post}/approve")
    assert signed_in.post(f"/approvals/{post}/review", data={"result": "fail"}).status_code == 409


def test_reviewer_sees_only_that_document(office: Office) -> None:
    reviewer = add_user("reviewer")
    author, post = submitted(office, reviewer_ids=[reviewer.id])
    other = draft(author, office)
    author.post(f"/approvals/{other}/submit")
    signed_in = browser(reviewer)
    assert signed_in.get(f"/approvals/{post}").status_code == 200
    assert signed_in.get(f"/approvals/{other}").status_code == 404
    assert signed_in.get(f"/channels/{office.channel.id}").status_code == 404
    assert signed_in.post(f"/posts/{post}/replies", data={"body": "검토 의견"}).status_code == 200
    assert signed_in.post(f"/approvals/{post}/approve").status_code == 409


def test_non_reviewer_cannot_review(office: Office) -> None:
    _, post = submitted(office)
    response = browser(office.first).post(f"/approvals/{post}/review", data={"result": "pass"})
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["drafter", "first"])
def test_drafter_and_approver_cannot_be_reviewers(office: Office, role: str) -> None:
    signed_in = browser(office.drafter)
    post = draft(signed_in, office)
    response = signed_in.post(
        f"/approvals/{post}/submit", data={"reviewer_ids": [getattr(office, role).id]}
    )
    assert response.status_code == 400
    assert state(post).status == "draft"


# 승인 취소, 반려, 기안 취소


def test_cancel_returns_turn_and_keeps_earlier_steps(office: Office) -> None:
    team = add_department("세단계팀")
    second = add_user("second")
    member = add_user("member")
    join(member, team)
    grant(office.channel, member, read="allow", write="allow")
    set_line(office.channel, team, office.first, second, office.final)
    signed_in = browser(member)
    post = draft(signed_in, office)
    signed_in.post(f"/approvals/{post}/submit")
    browser(office.first).post(f"/approvals/{post}/approve")
    browser(second).post(f"/approvals/{post}/approve")

    assert browser(office.first).post(f"/approvals/{post}/cancel").status_code == 409
    assert browser(second).post(f"/approvals/{post}/cancel").status_code == 200
    rows = steps(post)
    assert state(post).current_step == 2
    assert rows[1].status == "approved" and rows[2].status == "waiting"
    assert rows[2].acted_at is None
    assert "approval.cancel" in audit_actions()


def test_nobody_cancels_before_first_or_after_final(office: Office) -> None:
    author, post = submitted(office)
    assert author.post(f"/approvals/{post}/cancel").status_code == 409
    browser(office.first).post(f"/approvals/{post}/approve")
    final = browser(office.final)
    final.post(f"/approvals/{post}/approve")
    assert final.post(f"/approvals/{post}/cancel").status_code == 409
    assert browser(office.first).post(f"/approvals/{post}/cancel").status_code == 409


def test_reject_needs_reason_and_closes_document(office: Office) -> None:
    author, post = submitted(office)
    first = browser(office.first)
    assert first.post(f"/approvals/{post}/reject", data={"comment": "  "}).status_code == 400
    response = first.post(f"/approvals/{post}/reject", data={"comment": "금액 재확인"})
    assert response.status_code == 200 and "금액 재확인" in response.text
    closed = state(post)
    assert closed.status == "rejected" and closed.retain_until is not None
    assert first.post(f"/approvals/{post}/approve").status_code == 409
    assert author.post(f"/posts/{post}/delete").status_code == 403
    assert author.get(f"/approvals/{post}").status_code == 200


def test_withdraw_within_24_hours_keeps_document(office: Office) -> None:
    author, post = submitted(office)
    browser(office.first).post(f"/approvals/{post}/approve")
    assert author.post(f"/approvals/{post}/withdraw").status_code == 200
    assert state(post).status == "withdrawn"
    assert "기안 취소" in author.get(f"/channels/{office.channel.id}").text
    assert steps(post)[1].status == "approved"
    assert browser(office.final).post(f"/approvals/{post}/approve").status_code == 409


def test_withdraw_closes_after_24_hours(office: Office) -> None:
    author, post = submitted(office)

    async def age() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(Approval)
                .where(Approval.post_id == post)
                .values(submitted_at=datetime.now(UTC) - timedelta(hours=24, seconds=1))
            )
            await session.commit()

    run(age())
    assert author.post(f"/approvals/{post}/withdraw").status_code == 409


def test_only_drafter_withdraws_and_not_after_final(office: Office) -> None:
    author, post = submitted(office)
    assert browser(office.first).post(f"/approvals/{post}/withdraw").status_code == 409
    browser(office.first).post(f"/approvals/{post}/approve")
    browser(office.final).post(f"/approvals/{post}/approve")
    assert author.post(f"/approvals/{post}/withdraw").status_code == 409


def test_reference_submission_starts_new_document(office: Office) -> None:
    author = browser(office.drafter)
    response = author.post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "원본", "body": "원본 본문"},
        files=[("files", ("a.txt", io.BytesIO(b"x")))],
    )
    old = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    author.post(f"/approvals/{old}/submit")
    browser(office.first).post(f"/approvals/{old}/reject", data={"comment": "다시"})

    page = author.get(f"/channels/{office.channel.id}/approvals/new?reference={old}")
    assert "원본 본문" in page.text
    new = draft(author, office, body="고친 본문", reference_post_id=old)
    assert state(new).reference_post_id == old and state(new).status == "draft"

    async def attachments() -> list[Attachment]:
        async with SessionLocal() as session:
            return list(await session.scalars(select(Attachment).order_by(Attachment.id)))

    first, copied = run(attachments())
    assert copied.post_id == new and copied.storage_key != first.storage_key
    assert author.post(f"/approvals/{new}/submit").status_code == 200
    assert state(old).status == "rejected"


def test_reference_requires_view_permission(office: Office) -> None:
    _, post = submitted(office)
    outsider = add_user("outsider")
    grant(office.channel, outsider, write="allow")
    signed_in = browser(outsider)
    assert (
        signed_in.get(f"/channels/{office.channel.id}/approvals/new?reference={post}").status_code
        == 404
    )
    response = signed_in.post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "x", "body": "y", "reference_post_id": post},
    )
    assert response.status_code == 404


# F-60: 대결과 재지정은 없다. 잘못된 승인선은 반려나 기안 취소 뒤 재기안으로 고친다.


def test_running_line_cannot_be_reassigned(office: Office) -> None:
    substitute = add_user("substitute")
    _, post = submitted(office)
    response = browser(office.first).post(
        f"/approvals/{post}/reassign", data={"step_no": 2, "email": substitute.email}
    )
    assert response.status_code in (404, 405)
    assert approvers_of(post)[2] == office.final.id
    assert "다음 승인자 변경" not in browser(office.first).get(f"/approvals/{post}").text


@pytest.mark.parametrize("closing", ["reject", "withdraw"])
def test_wrong_line_is_fixed_by_redrafting(office: Office, closing: str) -> None:
    correct = add_user("correct")
    author, old = submitted(office)
    if closing == "reject":
        browser(office.first).post(f"/approvals/{old}/reject", data={"comment": "승인선 오류"})
    else:
        author.post(f"/approvals/{old}/withdraw")
    browser(office.manager).post(
        f"/channels/{office.channel.id}/approval-lines",
        data={
            "department_id": office.team.id,
            "approvers": f"{correct.email}\n{office.final.email}",
        },
    )
    new = draft(author, office, body="재기안", reference_post_id=old)
    assert author.post(f"/approvals/{new}/submit").status_code == 200
    assert approvers_of(new) == [office.drafter.id, correct.id, office.final.id]
    assert approvers_of(old)[1] == office.first.id


# 개수와 내 할 일


def test_counts_follow_the_turn(office: Office) -> None:
    _, post = submitted(office)
    assert run(todo_count(office.first.id)) == 1
    assert run(todo_count(office.final.id)) == 0
    first = browser(office.first)
    first.post(f"/approvals/{post}/approve")
    assert run(todo_count(office.first.id)) == 0
    assert run(todo_count(office.final.id)) == 1
    first.post(f"/approvals/{post}/cancel")
    assert run(todo_count(office.first.id)) == 1
    assert run(todo_count(office.final.id)) == 0


def test_result_notice_counts_until_confirmed(office: Office) -> None:
    author, post = submitted(office)
    browser(office.first).post(f"/approvals/{post}/reject", data={"comment": "사유"})
    assert run(todo_count(office.drafter.id)) == 1
    page = author.get("/todo")
    assert "결재 결과" in page.text and "반려" in page.text

    other = browser(office.reader)
    assert other.post("/notifications/1/read").status_code == 404
    assert author.post("/notifications/1/read").status_code == 200
    assert run(todo_count(office.drafter.id)) == 0


def test_todo_page_lists_every_box(office: Office) -> None:
    reviewer = add_user("reviewer")
    _, post = submitted(office, reviewer_ids=[reviewer.id])
    assert "부스 비용" in browser(office.first).get("/todo").text
    assert "부스 비용" in browser(reviewer).get("/todo").text
    assert "부스 비용" in browser(office.drafter).get("/todo").text
    first = browser(office.first)
    first.post(f"/approvals/{post}/approve")
    assert '내가 처리한 결재 <span class="muted">1건' in first.get("/todo").text


# 게시


@pytest.fixture
def finished(office: Office):
    talk = add_channel(add_group(office.manager, "소통"), "공지")
    grant(talk, office.drafter, **FULL)
    author, post = submitted(office)
    browser(office.first).post(f"/approvals/{post}/approve")
    browser(office.final).post(f"/approvals/{post}/approve")
    return author, post, talk


def test_publish_copies_document_for_people_without_approval_access(
    office: Office, finished
) -> None:
    author, post, talk = finished
    audience = add_user("audience")
    grant(talk, audience, read="allow")
    assert (
        author.post(f"/approvals/{post}/publish", data={"channel_id": talk.id}).status_code == 200
    )
    page = browser(audience).get(f"/channels/{talk.id}").text
    assert "부스 비용" in page and "본문" in page and "최종 final" in page
    assert browser(audience).get(f"/approvals/{post}").status_code == 404
    assert state(post).published_post_id is not None
    assert "approval.publish" in audit_actions()


def test_publish_needs_write_on_target_and_talk_type(office: Office, finished) -> None:
    author, post, talk = finished
    locked = add_channel(add_group(office.manager, "잠긴"), "잠긴 채널")
    assert (
        author.post(f"/approvals/{post}/publish", data={"channel_id": locked.id}).status_code == 403
    )
    assert (
        author.post(
            f"/approvals/{post}/publish", data={"channel_id": office.channel.id}
        ).status_code
        == 403
    )


def test_publish_only_by_line_members_and_once(office: Office, finished) -> None:
    author, post, talk = finished
    data = {"channel_id": talk.id}
    assert browser(office.reader).post(f"/approvals/{post}/publish", data=data).status_code == 409
    assert author.post(f"/approvals/{post}/publish", data=data).status_code == 200
    assert author.post(f"/approvals/{post}/publish", data=data).status_code == 409


def test_unfinished_document_cannot_be_published(office: Office) -> None:
    talk = add_channel(add_group(office.manager, "소통"), "공지")
    grant(talk, office.drafter, **FULL)
    author, post = submitted(office)
    assert (
        author.post(f"/approvals/{post}/publish", data={"channel_id": talk.id}).status_code == 409
    )


def test_publish_attachments_are_optional(office: Office) -> None:
    talk = add_channel(add_group(office.manager, "소통"), "공지")
    grant(talk, office.drafter, **FULL)
    author = browser(office.drafter)
    response = author.post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "x", "body": "y"},
        files=[("files", ("a.txt", io.BytesIO(b"x")))],
    )
    post = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    author.post(f"/approvals/{post}/submit")
    browser(office.first).post(f"/approvals/{post}/approve")
    browser(office.final).post(f"/approvals/{post}/approve")
    author.post(
        f"/approvals/{post}/publish", data={"channel_id": talk.id, "include_attachments": "true"}
    )
    page = author.get(f"/channels/{talk.id}").text
    assert "a.txt" in page


# 승인선 관리


def test_only_group_manager_saves_line(office: Office) -> None:
    url = f"/channels/{office.channel.id}/approval-lines"
    data = {"department_id": office.team.id, "approvers": office.final.email}
    assert browser(office.reader).post(url, data=data).status_code == 403
    assert browser(office.manager).post(url, data=data).status_code == 200
    assert "approval.line_change" in audit_actions()


@pytest.mark.parametrize("approvers", ["없는@test.local", "first@test.local\nfirst@test.local"])
def test_line_rejects_unknown_or_repeated_people(office: Office, approvers: str) -> None:
    response = browser(office.manager).post(
        f"/channels/{office.channel.id}/approval-lines",
        data={"department_id": office.team.id, "approvers": approvers},
    )
    assert response.status_code == 400


def test_settings_page_shows_line_and_saves_defaults(office: Office) -> None:
    talk = add_channel(add_group(office.manager, "소통"), "공지")
    manager = browser(office.manager)
    assert "first → final" in manager.get(f"/channels/{office.channel.id}/settings").text
    response = manager.post(
        f"/channels/{office.channel.id}/approval-settings",
        data={"template": "틀", "default_publish_channel_id": talk.id},
    )
    assert response.status_code == 200


# 동시 처리


def together(*calls):
    with ThreadPoolExecutor(len(calls)) as pool:
        return [future.result() for future in [pool.submit(call) for call in calls]]


def test_double_approval_counts_once(office: Office) -> None:
    _, post = submitted(office)
    first = browser(office.first)
    results = together(*[lambda: first.post(f"/approvals/{post}/approve") for _ in range(4)])
    assert sorted(result.status_code for result in results) == [200, 409, 409, 409]
    assert state(post).current_step == 2


def test_cancel_and_next_approval_only_one_wins(office: Office) -> None:
    for _ in range(10):
        _, post = submitted(office)
        first, final = browser(office.first), browser(office.final)
        first.post(f"/approvals/{post}/approve")
        cancelled, approved = together(
            lambda post=post, first=first: first.post(f"/approvals/{post}/cancel"),
            lambda post=post, final=final: final.post(f"/approvals/{post}/approve"),
        )
        assert sorted((cancelled.status_code, approved.status_code)) == [200, 409]
        current = state(post)
        if approved.status_code == 200:
            assert current.status == "approved"
        else:
            assert current.status == "in_progress" and current.current_step == 1


def test_final_approval_and_withdraw_only_one_wins(office: Office) -> None:
    for _ in range(10):
        author, post = submitted(office)
        browser(office.first).post(f"/approvals/{post}/approve")
        final = browser(office.final)
        approved, withdrawn = together(
            lambda post=post, final=final: final.post(f"/approvals/{post}/approve"),
            lambda post=post, author=author: author.post(f"/approvals/{post}/withdraw"),
        )
        assert sorted((approved.status_code, withdrawn.status_code)) == [200, 409]


def test_submit_twice_creates_one_line(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office)
    results = together(*[lambda: author.post(f"/approvals/{post}/submit") for _ in range(3)])
    assert sorted(result.status_code for result in results) == [200, 409, 409]
    assert len(steps(post)) == 3


# 다시 읽으며 찾은 경계


def test_title_only_edit_is_saved(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office, body="본문")
    author.post(f"/posts/{post}/edit", data={"body": "본문", "title": "제목만 고침"})
    assert state(post).title == "제목만 고침"


def test_deleted_document_cannot_be_referenced(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office, body="지운 내용")
    author.post(f"/posts/{post}/delete")
    url = f"/channels/{office.channel.id}/approvals"
    assert author.get(f"{url}/new?reference={post}").status_code == 404
    response = author.post(url, data={"title": "x", "body": "y", "reference_post_id": post})
    assert response.status_code == 404


def test_only_approval_documents_can_be_referenced(office: Office) -> None:
    talk = add_channel(add_group(office.manager, "소통"), "공지")
    grant(talk, office.drafter, **FULL)
    author = browser(office.drafter)
    created = author.post(f"/channels/{talk.id}/posts", data={"body": "일반 글"})
    post = int(created.text.split('id="post-')[1].split('"')[0])
    url = f"/channels/{office.channel.id}/approvals"
    assert author.get(f"{url}/new?reference={post}").status_code == 404


def test_deleted_draft_leaves_my_list(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office)
    assert "부스 비용" in author.get("/todo").text
    author.post(f"/posts/{post}/delete")
    assert "부스 비용" not in author.get("/todo").text


# 승인자 비활성과 폐기


def retire(user: User, status: str = "inactive") -> None:
    async def change() -> None:
        async with SessionLocal() as session:
            (await session.get(User, user.id)).status = status
            await session.commit()

    run(change())


def test_manager_discards_when_current_approver_is_inactive(office: Office) -> None:
    author, post = submitted(office)
    retire(office.first)
    manager = browser(office.manager)
    response = manager.post(f"/approvals/{post}/discard", data={"reason": "1차 승인자 퇴사"})
    assert response.status_code == 200
    closed = state(post)
    assert closed.status == "discarded" and closed.retain_until is not None
    assert closed.discard_reason == "1차 승인자 퇴사"
    assert "approval.discard" in audit_actions()
    page = author.get(f"/approvals/{post}").text
    assert "폐기" in page and "1차 승인자 퇴사" in page and "참조 상신" in page
    assert run(todo_count(office.drafter.id)) == 1
    assert author.post(f"/posts/{post}/delete").status_code == 403
    assert browser(office.final).post(f"/approvals/{post}/approve").status_code == 409


def test_later_inactive_approver_also_allows_discard(office: Office) -> None:
    _, post = submitted(office)
    retire(office.final)
    response = browser(office.manager).post(f"/approvals/{post}/discard", data={"reason": "퇴사"})
    assert response.status_code == 200


def test_discard_needs_inactive_approver(office: Office) -> None:
    _, post = submitted(office)
    manager = browser(office.manager)
    assert manager.post(f"/approvals/{post}/discard", data={"reason": "x"}).status_code == 409
    retire(office.first)
    retire(office.first, "active")
    assert manager.post(f"/approvals/{post}/discard", data={"reason": "x"}).status_code == 409
    assert state(post).status == "in_progress"


def test_passed_inactive_approver_does_not_allow_discard(office: Office) -> None:
    _, post = submitted(office)
    browser(office.first).post(f"/approvals/{post}/approve")
    retire(office.first)
    response = browser(office.manager).post(f"/approvals/{post}/discard", data={"reason": "x"})
    assert response.status_code == 409


def test_discard_needs_reason(office: Office) -> None:
    _, post = submitted(office)
    retire(office.first)
    response = browser(office.manager).post(f"/approvals/{post}/discard", data={"reason": " "})
    assert response.status_code == 400


@pytest.mark.parametrize("role", ["drafter", "final", "reader"])
def test_only_group_manager_discards(office: Office, role: str) -> None:
    _, post = submitted(office)
    retire(office.first)
    signed_in = browser(getattr(office, role))
    response = signed_in.post(f"/approvals/{post}/discard", data={"reason": "x"})
    assert response.status_code == 403


def test_manager_of_other_group_cannot_discard(office: Office) -> None:
    _, post = submitted(office)
    retire(office.first)
    other = add_user("other-manager")
    add_group(other, "다른 그룹")
    response = browser(other).post(f"/approvals/{post}/discard", data={"reason": "x"})
    assert response.status_code == 403


def test_manager_gets_no_content_access_from_stalled_list(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office, body="민감한 본문")
    author.post(f"/approvals/{post}/submit")
    retire(office.first)
    manager = browser(office.manager)
    assert "부스 비용" in manager.get("/todo").text
    assert run(todo_count(office.manager.id)) == 1
    settings = manager.get(f"/channels/{office.channel.id}/settings").text
    assert "부스 비용" in settings and "민감한 본문" not in settings
    assert manager.get(f"/approvals/{post}").status_code == 404

    manager.post(f"/approvals/{post}/discard", data={"reason": "퇴사"})
    assert run(todo_count(office.manager.id)) == 0
    assert "멈춘 결재" not in manager.get("/todo").text


def test_discarded_document_can_be_resubmitted(office: Office) -> None:
    author, old = submitted(office)
    retire(office.first)
    browser(office.manager).post(f"/approvals/{old}/discard", data={"reason": "퇴사"})
    retire(office.first, "active")
    new = draft(author, office, body="다시", reference_post_id=old)
    assert author.post(f"/approvals/{new}/submit").status_code == 200


# 기안 취소 시간 설정


def test_withdraw_window_comes_from_settings(office: Office, monkeypatch) -> None:
    author, post = submitted(office)

    async def age(hours: int) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(Approval)
                .where(Approval.post_id == post)
                .values(submitted_at=datetime.now(UTC) - timedelta(hours=hours))
            )
            await session.commit()

    run(age(30))
    monkeypatch.setattr(get_settings(), "withdraw_hours", 2)
    response = author.post(f"/approvals/{post}/withdraw")
    assert response.status_code == 409 and "2시간" in response.json()["detail"]
    monkeypatch.setattr(get_settings(), "withdraw_hours", 48)
    assert "기안 취소</button>" in author.get(f"/approvals/{post}").text
    assert author.post(f"/approvals/{post}/withdraw").status_code == 200


def test_withdraw_hours_read_from_environment(monkeypatch) -> None:
    assert Settings(_env_file=None).withdraw_hours == 24
    monkeypatch.setenv("BARO_WITHDRAW_HOURS", "72")
    assert Settings(_env_file=None).withdraw_hours == 72


@pytest.mark.parametrize("value", ["0", "-1", "하루"])
def test_invalid_withdraw_hours_stop_startup(monkeypatch, value: str) -> None:
    monkeypatch.setenv("BARO_WITHDRAW_HOURS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# 승인선 편집 화면


def test_approver_search_finds_active_users_with_department(office: Office) -> None:
    team = add_department("재무팀")
    target = add_user("search-target")
    join(target, team)
    retire(add_user("search-retired"))
    manager = browser(office.manager)
    url = f"/channels/{office.channel.id}/approver-candidates"
    expected = [
        {
            "id": target.id,
            "name": "search-target",
            "email": "search-target@test.local",
            "department": "재무팀",
        }
    ]
    assert manager.get(f"{url}?q=search").json() == expected
    assert manager.get(f"{url}?q=target@test").json() == expected
    assert manager.get(f"{url}?q=재무").json() == expected
    assert manager.get(f"{url}?q=%25").json() == []


def test_concurrent_user_shows_primary_department(office: Office) -> None:
    user = add_user("dual")
    first, second = add_department("가나팀"), add_department("하하팀")

    async def memberships() -> None:
        async with SessionLocal() as session:
            session.add(UserDepartment(user_id=user.id, department_id=first.id))
            session.add(UserDepartment(user_id=user.id, department_id=second.id, is_primary=True))
            await session.commit()

    run(memberships())
    url = f"/channels/{office.channel.id}/approver-candidates?q=dual"
    assert browser(office.manager).get(url).json()[0]["department"] == "하하팀"


def test_saved_line_shows_approver_departments(office: Office) -> None:
    join(office.first, add_department("재무팀"))
    page = browser(office.manager).get(f"/channels/{office.channel.id}/settings").text
    assert "first (재무팀) → final" in page


def test_approver_search_is_for_group_managers_only(office: Office) -> None:
    url = f"/channels/{office.channel.id}/approver-candidates"
    assert browser(office.reader).get(url).status_code == 403


def test_manager_without_read_can_search_approvers(office: Office) -> None:
    manager = browser(office.manager)
    assert manager.get(f"/channels/{office.channel.id}").status_code == 404
    url = f"/channels/{office.channel.id}/approver-candidates?q=first"
    assert manager.get(url).json()[0]["email"] == office.first.email


def test_settings_page_carries_saved_line_for_editing(office: Office) -> None:
    page = browser(office.manager).get(f"/channels/{office.channel.id}/settings").text
    assert "line-picker" in page and office.first.email in page
    assert f'data-department="{office.team.id}"' in page


def test_line_data_is_escaped_in_attribute(office: Office) -> None:
    run(_rename_user(office.first, "a'><script>x</script>"))
    page = browser(office.manager).get(f"/channels/{office.channel.id}/settings").text
    assert "<script>x" not in page


async def _rename_user(user: User, name: str) -> None:
    async with SessionLocal() as session:
        (await session.get(User, user.id)).name = name
        await session.commit()


# 검토자 선택


def test_reviewer_search_flags_line_members_and_hides_drafter(office: Office) -> None:
    add_user("candidate")
    author = browser(office.drafter)
    post = draft(author, office)
    rows = author.get(f"/approvals/{post}/reviewer-candidates").json()
    blocked = {row["name"]: row["blocked"] for row in rows}
    assert blocked["candidate"] == "" and blocked["first"] == "승인선에 있음"
    assert blocked["final"] == "승인선에 있음" and "drafter" not in blocked


def test_reviewer_search_only_for_drafter_before_submit(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office)
    url = f"/approvals/{post}/reviewer-candidates"
    assert browser(office.reader).get(url).status_code == 404
    author.post(f"/approvals/{post}/submit")
    assert author.get(url).status_code == 403
    assert browser(office.first).get(url).status_code == 403


def test_submit_page_no_longer_lists_every_user(office: Office) -> None:
    add_user("everyone-list")
    author = browser(office.drafter)
    page = author.get(f"/approvals/{draft(author, office)}").text
    assert "reviewer-picker" in page and "everyone-list" not in page

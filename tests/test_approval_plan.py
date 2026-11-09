"""결재 작성 화면의 결재선 수정과 참조자 지정.

기안자는 결재선을 자유롭게 고친다. 참조자는 상신된 뒤 그 문서만 읽고 쓰레드에 의견을 남긴다.
"""

import json
import re
from html import unescape

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Approval, ApprovalReference, ApprovalReview, AuditLog, User
from app.services.delivery import todo_count
from tests.conftest import add_department, add_user, browser, grant, join, run
from tests.test_approvals import Office, approvers_of, draft, make_office, set_line, state


@pytest.fixture(name="office")
def office_fixture() -> Office:
    return make_office()


def plan_data(**lists: list[User]) -> dict[str, object]:
    data: dict[str, object] = {"plan": "1"}
    for role, field in (
        ("line", "line_ids"),
        ("reviewers", "reviewer_ids"),
        ("references", "reference_ids"),
    ):
        data[field] = [user.id for user in lists.get(role, [])]
    return data


def planned_draft(signed_in, office: Office, **lists: list[User]):
    return signed_in.post(
        f"/channels/{office.channel.id}/approvals",
        data={"title": "부스 비용", "body": "본문", **plan_data(**lists)},
    )


def post_of(response) -> int:
    assert response.status_code == 200, response.text
    return int(response.headers["HX-Redirect"].rsplit("/", 1)[1])


def references_of(post: int) -> dict[int, object]:
    async def load():
        async with SessionLocal() as session:
            approval = await session.scalar(select(Approval).where(Approval.post_id == post))
            rows = await session.scalars(
                select(ApprovalReference).where(ApprovalReference.approval_id == approval.id)
            )
            return {row.user_id: row.read_at for row in rows}

    return run(load())


def reviewers_of(post: int) -> set[int]:
    async def load():
        async with SessionLocal() as session:
            approval = await session.scalar(select(Approval).where(Approval.post_id == post))
            rows = await session.scalars(
                select(ApprovalReview.reviewer_id).where(ApprovalReview.approval_id == approval.id)
            )
            return set(rows)

    return run(load())


def planner_json(page: str) -> dict[str, list[dict]]:
    found = re.search(r"data-plan='([^']*)'", page)
    assert found, "결재선 편집기가 없습니다."
    return json.loads(unescape(found.group(1)))


def test_new_page_prefills_registered_line(office: Office) -> None:
    page = browser(office.drafter).get(f"/channels/{office.channel.id}/approvals/new").text
    data = planner_json(page)
    assert [row["name"] for row in data["line"]] == ["first", "final"]
    assert data["reviewers"] == [] and data["references"] == []
    assert f'data-people="/channels/{office.channel.id}/approval-people"' in page
    assert 'name="plan" value="1"' in page


def test_custom_line_reviewers_and_references_are_kept_until_submit(office: Office) -> None:
    other, reviewer, watcher = add_user("other"), add_user("reviewer"), add_user("watcher")
    author = browser(office.drafter)
    post = post_of(
        planned_draft(
            author,
            office,
            line=[other, office.first, office.final],
            reviewers=[reviewer],
            references=[watcher],
        )
    )
    assert state(post).plan == {
        "line": [other.id, office.first.id, office.final.id],
        "reviewers": [reviewer.id],
        "references": [watcher.id],
    }
    page = author.get(f"/approvals/{post}").text
    assert "other → first → final · 참조: watcher" in page
    # 상신 화면도 저장한 지정으로 채운다.
    data = planner_json(page)
    assert [row["name"] for row in data["line"]] == ["other", "first", "final"]
    assert [row["name"] for row in data["references"]] == ["watcher"]

    assert author.post(f"/approvals/{post}/submit").status_code == 200
    assert approvers_of(post) == [office.drafter.id, other.id, office.first.id, office.final.id]
    assert reviewers_of(post) == {reviewer.id}
    assert set(references_of(post)) == {watcher.id}
    log = run(_submit_log(post))
    assert log.detail["references"] == [watcher.id] and log.detail["line_changed"] is True


async def _submit_log(post: int) -> AuditLog:
    async with SessionLocal() as session:
        return await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "approval.submit",
                AuditLog.detail["post_id"].as_integer() == post,
            )
        )


def test_submit_form_overrides_saved_plan(office: Office) -> None:
    other = add_user("other")
    author = browser(office.drafter)
    post = draft(author, office)
    response = author.post(f"/approvals/{post}/submit", data=plan_data(line=[office.final, other]))
    assert response.status_code == 200, response.text
    assert approvers_of(post) == [office.drafter.id, office.final.id, other.id]
    log = run(_submit_log(post))
    assert log.detail["line_changed"] is True


def test_unchanged_line_is_recorded_as_default(office: Office) -> None:
    author = browser(office.drafter)
    post = draft(author, office)
    author.post(f"/approvals/{post}/submit", data=plan_data(line=[office.first, office.final]))
    assert run(_submit_log(post)).detail["line_changed"] is False


@pytest.mark.parametrize(
    ("lists", "message"),
    [
        ({"line": []}, "승인자를 한 명 이상 지정해 주세요."),
        ({"line": ["drafter", "final"]}, "기안자는 결재선에 넣을 수 없습니다."),
        ({"line": ["first", "drafter"]}, "기안자는 최종 승인자가 될 수 없습니다."),
        ({"line": ["first", "first"]}, "결재선에 같은 사람이 두 번 있습니다."),
        (
            {"line": ["first"], "reviewers": ["first"]},
            "기안자와 승인자는 검토자로 지정할 수 없습니다.",
        ),
        ({"line": ["first"], "references": ["drafter"]}, "참조자로 지정할 수 없습니다."),
        (
            {"line": ["first"], "reviewers": ["final"], "references": ["final"]},
            "참조자로 지정할 수 없습니다.",
        ),
    ],
)
def test_plan_rules(office: Office, lists: dict[str, list[str]], message: str) -> None:
    people = {"drafter": office.drafter, "first": office.first, "final": office.final}
    response = planned_draft(
        browser(office.drafter),
        office,
        **{role: [people[name] for name in names] for role, names in lists.items()},
    )
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_plan_size_is_limited(office: Office) -> None:
    crowd = [add_user(f"person{number}") for number in range(21)]
    response = planned_draft(browser(office.drafter), office, line=[office.first], references=crowd)
    assert response.status_code == 400


def test_without_registered_line_drafter_may_submit_alone(office: Office) -> None:
    loner = add_user("loner")
    join(loner, add_department("새 팀"))
    grant(office.channel, loner, read="allow", write="allow")
    signed_in = browser(loner)
    post = post_of(planned_draft(signed_in, office))
    assert signed_in.post(f"/approvals/{post}/submit").status_code == 200
    assert approvers_of(post) == [loner.id, loner.id]


def test_registered_final_drafter_keeps_self_as_final(office: Office) -> None:
    boss = add_department("본부")
    join(office.drafter, boss)
    set_line(office.channel, boss, office.first, office.drafter)
    author = browser(office.drafter)
    response = author.post(
        f"/channels/{office.channel.id}/approvals",
        data={
            "title": "x",
            "body": "y",
            "department_id": boss.id,
            **plan_data(line=[office.first, office.drafter]),
        },
    )
    post = post_of(response)
    assert author.post(f"/approvals/{post}/submit").status_code == 200
    assert approvers_of(post) == [office.drafter.id, office.first.id, office.drafter.id]


def test_person_inactive_after_saving_blocks_submit(office: Office) -> None:
    watcher = add_user("watcher")
    author = browser(office.drafter)
    post = post_of(planned_draft(author, office, line=[office.final], references=[watcher]))
    run(_retire(watcher))
    response = author.post(f"/approvals/{post}/submit")
    assert response.status_code == 409
    assert state(post).status == "draft"


async def _retire(user: User) -> None:
    async with SessionLocal() as session:
        (await session.get(User, user.id)).status = "inactive"
        await session.commit()


def test_reference_reads_and_comments_only_after_submit(office: Office) -> None:
    watcher = add_user("watcher")
    author = browser(office.drafter)
    post = post_of(planned_draft(author, office, line=[office.first], references=[watcher]))
    viewer = browser(watcher)
    assert viewer.get(f"/approvals/{post}").status_code == 404

    author.post(f"/approvals/{post}/submit")
    page = viewer.get(f"/approvals/{post}").text
    assert "참조 watcher" in page
    reply = viewer.post(f"/posts/{post}/replies", data={"body": "확인했습니다"})
    assert reply.status_code == 200, reply.text
    # 승인과 검토는 할 수 없다.
    assert viewer.post(f"/approvals/{post}/approve").status_code in (403, 409)
    assert viewer.post(f"/approvals/{post}/review", data={"result": "pass"}).status_code in (
        403,
        409,
    )
    # 채널 전체가 아니라 그 문서만 본다.
    other = draft(author, office)
    author.post(f"/approvals/{other}/submit")
    assert viewer.get(f"/approvals/{other}").status_code == 404
    assert viewer.get(f"/channels/{office.channel.id}").status_code == 404


def test_reference_shows_in_todo_until_opened(office: Office) -> None:
    watcher = add_user("watcher")
    author = browser(office.drafter)
    post = post_of(planned_draft(author, office, line=[office.first], references=[watcher]))
    viewer = browser(watcher)
    assert run(todo_count(watcher.id)) == 0
    author.post(f"/approvals/{post}/submit")
    assert run(todo_count(watcher.id)) == 1
    todo = viewer.get("/todo").text
    assert "참조 문서" in todo and f'href="/approvals/{post}"' in todo

    viewer.get(f"/approvals/{post}")
    assert references_of(post)[watcher.id] is not None
    assert run(todo_count(watcher.id)) == 0
    assert "참조 문서" not in viewer.get("/todo").text


def test_custom_approver_gets_only_that_document(office: Office) -> None:
    outsider = add_user("outsider")
    author = browser(office.drafter)
    post = post_of(planned_draft(author, office, line=[outsider]))
    author.post(f"/approvals/{post}/submit")
    approver = browser(outsider)
    assert approver.get(f"/approvals/{post}").status_code == 200
    assert approver.post(f"/approvals/{post}/approve").status_code == 200
    assert state(post).status == "approved"
    other = draft(author, office)
    author.post(f"/approvals/{other}/submit")
    assert approver.get(f"/approvals/{other}").status_code == 404


def test_people_search_needs_write_and_hides_self(office: Office) -> None:
    add_user("candidate")
    url = f"/channels/{office.channel.id}/approval-people"
    rows = browser(office.drafter).get(f"{url}?q=").json()
    names = {row["name"] for row in rows}
    assert "candidate" in names and "drafter" not in names
    assert browser(office.reader).get(url).status_code == 403


def test_default_line_follows_department(office: Office) -> None:
    other = add_department("다른 팀")
    alternate = add_user("alternate")
    join(office.drafter, other)
    set_line(office.channel, other, alternate, office.final)
    signed_in = browser(office.drafter)
    url = f"/channels/{office.channel.id}/default-line"
    rows = signed_in.get(f"{url}?department_id={other.id}").json()
    assert [row["name"] for row in rows] == ["alternate", "final"]
    foreign = add_department("남의 팀")
    assert signed_in.get(f"{url}?department_id={foreign.id}").status_code == 400

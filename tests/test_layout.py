"""공통 화면 틀: 상단 접속자 정보, 분류별 채널 목록, 채널 안 화면의 채널 목록."""

import re

from app.models import UserDepartment
from tests.conftest import (
    FULL,
    _add,
    add_channel,
    add_department,
    add_group,
    add_user,
    admin_browser,
    browser,
    grant,
    join,
    run,
)


def sidebar(page: str) -> str:
    match = re.search(r'<aside class="sidebar">.*?</aside>', page, re.S)
    assert match, "채널 목록이 없습니다."
    return match.group(0)


def test_user_bar_shows_name_department_and_email() -> None:
    sales = add_department("영업팀")
    hq = add_department("본사")
    user = add_user("hong")
    join(user, hq)
    run(_add(UserDepartment(user_id=user.id, department_id=sales.id, is_primary=True)))
    page = browser(user).get("/").text
    bar = re.search(r'<header class="user-bar">.*?</header>', page, re.S).group(0)
    assert "<strong>hong</strong>" in bar
    assert '<span class="user-dept">영업팀</span>' in bar
    assert '<span class="user-email">hong@test.local</span>' in bar


def test_user_bar_without_department_and_for_admin() -> None:
    page = browser(add_user("plain")).get("/more").text
    assert '<span class="user-dept">소속 없음</span>' in page
    page = admin_browser(add_user("admin")).get("/admin/org").text
    assert '<span class="user-dept">시스템 관리자</span>' in page


def test_sidebar_groups_channels_under_categories() -> None:
    owner = add_user("owner")
    group = add_group(owner)
    talk = add_channel(group, "일반")
    doc = add_channel(group, "규정집", "doc")
    for channel in (talk, doc):
        grant(channel, owner, **FULL)
    nav = sidebar(browser(owner).get(f"/channels/{talk.id}").text)
    talk_section = re.search(
        r'<section class="nav-category"[^>]*>\s*<h3[^>]*>소통</h3>.*?</section>', nav, re.S
    )
    doc_section = re.search(
        r'<section class="nav-category"[^>]*>\s*<h3[^>]*>문서</h3>.*?</section>', nav, re.S
    )
    assert "# 일반" in talk_section.group(0) and "규정집" not in talk_section.group(0)
    assert "# 규정집" in doc_section.group(0)


def test_document_and_approval_pages_keep_channel_sidebar() -> None:
    owner = add_user("owner")
    group = add_group(owner, "영업")
    talk = add_channel(group, "일반")
    doc = add_channel(group, "규정집", "doc")
    approval = add_channel(group, "지출결의", "approval")
    for channel in (talk, doc, approval):
        grant(channel, owner, **FULL)
    signed_in = browser(owner)
    created = signed_in.post(f"/channels/{doc.id}/docs", data={"title": "휴가", "body": "본문"})
    doc_post = int(created.headers["HX-Redirect"].rsplit("/", 1)[1])
    created = signed_in.post(
        f"/channels/{approval.id}/approvals", data={"title": "x", "body": "본문"}
    )
    approval_post = int(created.headers["HX-Redirect"].rsplit("/", 1)[1])

    for url, current in (
        (f"/posts/{doc_post}", doc),
        (f"/posts/{doc_post}/revisions", doc),
        (f"/channels/{doc.id}/docs/new", doc),
        (f"/approvals/{approval_post}", approval),
        (f"/channels/{approval.id}/approvals/new", approval),
    ):
        page = signed_in.get(url).text
        nav = sidebar(page)
        assert f'href="/channels/{talk.id}"' in nav, url
        assert re.search(rf'<a href="/channels/{current.id}"[^>]*aria-current="page"', nav), url
        # 그룹 관리자에게는 그룹 설정 단추가 보인다.
        assert f'href="/groups/{group.id}/settings"' in nav, url
        assert 'class="rail-item active"' in page, url


def test_favicon_and_brand_assets_are_served() -> None:
    client = browser(add_user("viewer"))
    page = client.get("/").text
    assert 'rel="icon" href="/static/brand/favicon.svg' in page
    assert 'rel="apple-touch-icon"' in page and 'rel="manifest"' in page
    assert 'src="/static/brand/logo.svg' in page
    response = client.http.get("/favicon.ico")
    assert response.status_code == 200 and response.headers["content-type"] == "image/x-icon"
    for name in ("logo.svg", "favicon.svg", "apple-touch-icon.png", "logo-full.png"):
        assert client.http.get(f"/static/brand/{name}").status_code == 200, name


def test_document_and_approval_pages_use_side_thread_pane() -> None:
    owner = add_user("owner")
    group = add_group(owner, "영업")
    doc = add_channel(group, "규정집", "doc")
    approval = add_channel(group, "지출결의", "approval")
    for channel in (doc, approval):
        grant(channel, owner, **FULL)
    signed_in = browser(owner)
    created = signed_in.post(f"/channels/{doc.id}/docs", data={"title": "휴가", "body": "본문"})
    doc_post = int(created.headers["HX-Redirect"].rsplit("/", 1)[1])
    created = signed_in.post(
        f"/channels/{approval.id}/approvals", data={"title": "x", "body": "본문"}
    )
    approval_post = int(created.headers["HX-Redirect"].rsplit("/", 1)[1])
    signed_in.post(f"/approvals/{approval_post}/submit")

    for url, channel, post in (
        (f"/posts/{doc_post}", doc, doc_post),
        (f"/approvals/{approval_post}", approval, approval_post),
    ):
        page = signed_in.get(url).text
        assert f'sse-connect="/events/{channel.id}"' in page, url
        assert f'data-auto-thread="/posts/{post}/thread"' in page, url
        assert '<aside class="thread-pane" id="thread"' in page, url
        assert 'class="thread-resizer" role="separator"' in page, url
        # 쓰레드는 본문 아래가 아니라 오른쪽 창에 열린다.
        assert page.count('id="thread"') == 1, url
    # 작성 화면에는 쓰레드 창이 없다.
    assert 'id="thread"' not in signed_in.get(f"/channels/{doc.id}/docs/new").text

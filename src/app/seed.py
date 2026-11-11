"""개발용 샘플 데이터. 사람과 회사는 모두 가상이다."""

import io
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Approval,
    ApprovalLineTemplate,
    ApprovalStep,
    Attachment,
    Channel,
    Department,
    Poll,
    PollOption,
    PollVote,
    Post,
    PostRevision,
    Reaction,
    User,
    WorkCommand,
)
from app.services.admin import ADMIN_NAME
from app.services.audit import record_audit
from app.services.bulk_import import GROUP_SPECS, ORG_SPECS, import_groups, import_org
from app.services.channel_import import CHANNEL_SPECS, import_channels
from app.services.spreadsheet import read_workbook
from app.services.storage import ensure_bucket, mime_for, put_file

# seed_data/sample_org.xlsx의 초기 비밀번호와 같다.
SAMPLE_PASSWORD = "qlalfqjsgh-1"  # 한글 자판으로 '비밀번호-1'
EMAIL_DOMAIN = "baro.example"
# samples/todo_api.py의 기본 주소와 토큰.
# BARO_INTEGRATION_ALLOWED_HOSTS에 127.0.0.1:8100이 있어야 한다.
SAMPLE_TODO_URL = "http://127.0.0.1:8100/todos"
SAMPLE_TODO_TOKEN = "dev-todo-token"

# 소속·사용자, 그룹·그룹 관리자, 채널·권한은 엑셀 파일에 둔다.
# 관리 화면의 일괄 등록과 같은 코드로 읽는다.
SEED_DIR = Path(__file__).resolve().parent / "seed_data"
SEED_FILES = (
    (SEED_DIR / "sample_org.xlsx", ORG_SPECS, import_org),
    (SEED_DIR / "sample_groups.xlsx", GROUP_SPECS, import_groups),
    (SEED_DIR / "sample_channels.xlsx", CHANNEL_SPECS, import_channels),
)


def sample_png(width: int = 320, height: int = 160) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\0" + bytes((49 + y // 4, 130, 246)) * width for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@dataclass
class Seeder:
    session: AsyncSession
    now: datetime

    async def add(self, row):
        self.session.add(row)
        await self.session.flush()
        return row

    async def post(
        self,
        channel: Channel,
        author: User,
        body: str,
        hours_ago: float,
        parent: Post | None = None,
    ) -> Post:
        return await self.add(
            Post(
                channel_id=channel.id,
                parent_post_id=parent.id if parent else None,
                author_id=author.id,
                body_md=body,
                created_at=self.now - timedelta(hours=hours_ago),
            )
        )

    async def react(self, post: Post, emoji: str, *users: User) -> None:
        for user in users:
            await self.add(Reaction(post_id=post.id, user_id=user.id, emoji=emoji))

    async def attach(self, post: Post, name: str, content: bytes) -> None:
        key = f"attachments/{uuid4().hex}"
        mime = mime_for(name)
        await put_file(key, io.BytesIO(content), mime)
        await self.add(
            Attachment(
                post_id=post.id, file_name=name, storage_key=key, size=len(content), mime=mime
            )
        )


async def seed(session: AsyncSession) -> None:
    """빈 DB에만 넣는다."""
    if await session.scalar(select(func.count()).select_from(User)):
        raise RuntimeError("이미 사용자가 있습니다. 샘플 데이터는 빈 DB에만 넣습니다.")
    await ensure_bucket()
    db = Seeder(session, datetime.now(UTC))

    admin = await db.add(
        User(name=ADMIN_NAME, email=get_settings().admin_email or f"admin@{EMAIL_DOMAIN}")
    )
    for path, specs, importer in SEED_FILES:
        report = await importer(session, admin, read_workbook(path.read_bytes(), specs), True)
        if report.errors:
            raise RuntimeError(f"{path.name}: " + " / ".join(report.errors))

    departments = {item.name: item for item in await session.scalars(select(Department))}
    users = {
        item.email.split("@")[0]: item
        for item in await session.scalars(select(User).where(User.id != admin.id))
    }
    channels = {item.name: item for item in await session.scalars(select(Channel))}

    u = users
    notice = await db.post(
        channels["공지"],
        u["hr1"],
        "## 바로채널 오픈 안내\n\n오늘부터 사내 소통은 **바로채널**에서 진행합니다.\n\n"
        "- 채널 권한은 소속 기준으로 열립니다\n- 궁금한 점은 답글로 남겨 주세요",
        50,
    )
    notice.pinned_at = db.now - timedelta(hours=49)
    notice.pinned_by = u["hr1"].id
    await db.react(notice, "👍", u["sa1"], u["sa2"], u["pd1"], u["qa1"])
    await db.react(notice, "🎉", u["pd2"], u["sa4"])
    await db.post(channels["공지"], u["sa2"], "모바일에서도 쓸 수 있나요?", 49, notice)
    await db.post(
        channels["공지"], u["hr1"], "네, 휴대폰 브라우저에서 그대로 쓰시면 됩니다.", 48, notice
    )

    holiday = await db.post(
        channels["공지"],
        u["hr1"],
        "연말 휴무 일정은 **12월 24일**부터입니다. 팀별 비상 연락망을 확인해 주세요.",
        20,
    )
    await db.attach(holiday, "연말_휴무_안내.txt", "연말 휴무 안내 샘플 문서입니다.\n".encode())

    lunch = await db.post(
        channels["자유게시판"], u["qa1"], "회사 앞에 새로 생긴 칼국숫집 맛있어요 😄", 30
    )
    await db.react(lunch, "❤️", u["sa4"], u["pd2"])
    await db.post(channels["자유게시판"], u["sa4"], "내일 같이 가요!", 29, lunch)

    edited = await db.post(channels["자유게시판"], u["fi1"], "사내 동호회 회원을 모집합니다.", 10)
    await db.add(
        PostRevision(
            post_id=edited.id, version=1, body_md="동호회 모집합니다", edited_by=u["fi1"].id
        )
    )
    edited.edited_at = db.now - timedelta(hours=9)

    removed = await db.post(channels["자유게시판"], u["pd2"], "잘못 올린 글입니다.", 8)
    removed.deleted_at = db.now - timedelta(hours=7)
    removed.deleted_by = u["pd2"].id
    record_audit(
        session,
        actor_id=u["pd2"].id,
        action="post.delete",
        target_type="post",
        target_id=removed.id,
        channel_id=removed.channel_id,
    )

    await db.post(channels["건의함"], u["sa2"], "휴게실에 정수기를 한 대 더 놓아 주세요.", 26)
    await db.post(channels["건의함"], u["pd2"], "회의실 예약이 어려워요.", 12)

    team = await db.post(
        channels["영업 일반"],
        u["sa1"],
        "이번 달 목표\n\n| 팀 | 목표 |\n| --- | --- |\n| 영업1팀 | 12건 |\n| 영업2팀 | 9건 |",
        28,
    )
    team.pinned_at = db.now - timedelta(hours=27)
    team.pinned_by = u["sa1"].id
    await db.react(team, "✅", u["sa2"], u["sa4"])
    await db.post(channels["영업 일반"], u["sa2"], "1팀 제안서 초안 공유드립니다.", 6)

    meeting = await db.post(
        channels["고객 미팅"],
        u["sa2"],
        "내일 10시 고객사 미팅입니다. 부스 시안 확인 부탁드려요.",
        5,
    )
    await db.attach(meeting, "부스_시안.png", sample_png())
    await db.post(
        channels["고객 미팅"],
        u["pd2"],
        "전시용 샘플 제품은 생산팀에서 준비해 두겠습니다.",
        4,
        meeting,
    )

    plan = await db.post(
        channels["생산 일반"],
        u["pd1"],
        "이번 주 생산 계획입니다.\n\n| 라인 | 품목 | 수량 |\n| --- | --- | --- |\n"
        "| 1라인 | A형 | 1,200개 |\n| 2라인 | B형 | 800개 |",
        24,
    )
    await db.react(plan, "👀", u["pd2"], u["qa1"])
    await db.post(
        channels["설비 장애"], u["pd2"], "2라인 컨베이어 정지는 원인을 찾아 조치했습니다.", 3
    )
    standard = await db.post(
        channels["작업 표준"], u["qa1"], "## 출하 검사\n\n- 외관 확인\n- 치수 측정 3회", 20
    )
    standard.title = "출하 검사 기준"

    # 투표와 문서. 일정 조율은 전사가 읽고 인사팀이 올린다.
    workshop = await db.post(
        channels["일정 조율"], u["hr1"], "하반기 워크숍 날짜를 골라 주세요.", 6
    )
    await db.add(Poll(post_id=workshop.id, multi_select=True, closes_at=db.now + timedelta(days=3)))
    for number, label in enumerate(["12월 4일", "12월 11일", "12월 18일"], start=1):
        option = await db.add(PollOption(post_id=workshop.id, position=number, label=label))
        for voter in {1: ["sa2", "qa1"], 2: ["sa1"], 3: []}[number]:
            await db.add(PollVote(post_id=workshop.id, user_id=u[voter].id, option_id=option.id))
    rule = await db.post(
        channels["전사 규정"],
        u["hr1"],
        "## 연차\n\n- 입사 1년 후 15일\n- 반차는 4시간 단위",
        40,
    )
    rule.title = "휴가 규정"
    await db.add(
        PostRevision(
            post_id=rule.id,
            version=1,
            title="휴가 규정",
            body_md="## 연차\n\n- 입사 1년 후 15일",
            edited_by=u["hr1"].id,
        )
    )
    rule.edited_at = db.now - timedelta(hours=30)

    # 업무 채널은 samples/todo_api.py의 샘플 할 일 시스템을 부른다.
    # 그 서버를 따로 띄워야 쓸 수 있다.
    await db.add(
        WorkCommand(
            channel_id=channels["할 일"].id,
            name="add",
            label="할 일 등록",
            description="샘플 할 일 시스템에 할 일을 만듭니다",
            screen="todo",
            page="index",
            endpoint_url=SAMPLE_TODO_URL,
            api_token=SAMPLE_TODO_TOKEN,
            created_by=u["sa1"].id,
        )
    )
    await db.add(
        WorkCommand(
            channel_id=channels["할 일"].id,
            name="find",
            label="할 일 찾기",
            description="샘플 할 일 시스템에서 찾아 골라 공유합니다",
            screen="todo",
            page="find",
            endpoint_url=SAMPLE_TODO_URL,
            api_token=SAMPLE_TODO_TOKEN,
            created_by=u["sa1"].id,
        )
    )

    # 영업1팀은 자기 승인선을, 영업2팀은 상위 소속인 영업본부의 승인선을 쓴다.
    expense = channels["지출결의"]
    expense.approval_template_md = "## 지출 내용\n\n- 금액:\n- 사유:\n"
    for department, line in (
        ("영업1팀", ["sa1", "fi1"]),
        ("영업본부", ["fi1"]),
    ):
        for number, login in enumerate(line, start=1):
            await db.add(
                ApprovalLineTemplate(
                    channel_id=expense.id,
                    department_id=departments[department].id,
                    step_no=number,
                    approver_id=u[login].id,
                )
            )
    booth = await db.post(
        expense, u["sa2"], "## 지출 내용\n\n- 금액: 1,200,000원\n- 사유: 전시회 부스 임차", 2
    )
    draft = await db.add(
        Approval(
            post_id=booth.id,
            drafter_id=u["sa2"].id,
            department_id=departments["영업1팀"].id,
            title="전시회 부스 비용",
            status="in_progress",
            current_step=1,
            submitted_at=db.now - timedelta(hours=2),
        )
    )
    await db.add(
        ApprovalStep(
            approval_id=draft.id,
            step_no=0,
            step_type="draft",
            approver_id=u["sa2"].id,
            status="approved",
            acted_at=db.now - timedelta(hours=2),
        )
    )
    await db.add(ApprovalStep(approval_id=draft.id, step_no=1, approver_id=u["sa1"].id))
    await db.add(ApprovalStep(approval_id=draft.id, step_no=2, approver_id=u["fi1"].id))

    record_audit(
        session,
        actor_id=None,
        action="system.seed",
        target_type="system",
        target_id=0,
        users=len(users) + 1,
    )
    await session.commit()

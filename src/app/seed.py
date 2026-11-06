"""개발용 샘플 데이터. 사람과 회사는 모두 가상이다."""

import io
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import (
    Attachment,
    Channel,
    Department,
    GroupManager,
    PermissionGrant,
    Post,
    PostRevision,
    Reaction,
    User,
    UserDepartment,
    WorkspaceGroup,
)
from app.services.admin import ADMIN_NAME
from app.services.audit import record_audit
from app.services.storage import ensure_bucket, mime_for, put_file

SAMPLE_PASSWORD = "baro-sample-1"
EMAIL_DOMAIN = "baro.example"

TREE = {
    "경영지원본부": ["인사팀", "재무팀"],
    "영업본부": ["영업1팀", "영업2팀"],
    "개발본부": ["개발팀", "디자인팀"],
}
PEOPLE = [
    ("이서연", "seoyeon", "인사팀"),
    ("박준호", "junho", "재무팀"),
    ("정민재", "minjae", "영업본부"),
    ("김하늘", "haneul", "영업1팀"),
    ("강지훈", "jihoon", "영업1팀"),
    ("송예린", "yerin", "영업2팀"),
    ("임태현", "taehyun", "개발본부"),
    ("문수빈", "subin", "개발팀"),
    ("배지아", "jia", "디자인팀"),
]
GROUPS = {
    "전사": (
        "seoyeon",
        [
            ("공지", "talk"),
            ("자유게시판", "talk"),
            ("건의함", "talk"),
            ("일정 조율", "vote"),
            ("전사 규정", "doc"),
        ],
    ),
    "영업": ("minjae", [("영업 일반", "talk"), ("고객 미팅", "talk"), ("지출결의", "approval")]),
    "개발": ("taehyun", [("개발 일반", "talk"), ("장애 공유", "talk"), ("설계 문서", "doc")]),
}
FULL = {"read": "allow", "write": "allow", "reply_read": "allow", "reply_write": "allow"}
REPLY = {"read": "allow", "reply_read": "allow", "reply_write": "allow"}


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

    async def grant(self, scope, subject, **values: str) -> None:
        await self.add(
            PermissionGrant(
                scope_type="channel" if isinstance(scope, Channel) else "group",
                scope_id=scope.id,
                subject_type="user" if isinstance(subject, User) else "department",
                subject_id=subject.id,
                **{f"can_{key}": value for key, value in values.items()},
            )
        )

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
    """빈 DB에만 넣는다. 시스템 관리자는 초기 PIN 상태로 만든다."""
    if await session.scalar(select(func.count()).select_from(User)):
        raise RuntimeError("이미 사용자가 있습니다. 샘플 데이터는 빈 DB에만 넣습니다.")
    await ensure_bucket()
    db = Seeder(session, datetime.now(UTC))

    admin = await db.add(
        User(name=ADMIN_NAME, email=get_settings().admin_email or f"admin@{EMAIL_DOMAIN}")
    )
    departments: dict[str, Department] = {}
    for parent_name, children in TREE.items():
        parent = await db.add(Department(name=parent_name))
        departments[parent_name] = parent
        for child in children:
            departments[child] = await db.add(Department(name=child, parent_id=parent.id))

    password_hash = hash_password(SAMPLE_PASSWORD)
    users: dict[str, User] = {}
    for name, login, department in PEOPLE:
        user = await db.add(
            User(name=name, email=f"{login}@{EMAIL_DOMAIN}", password_hash=password_hash)
        )
        users[login] = user
        await db.add(
            UserDepartment(
                user_id=user.id, department_id=departments[department].id, is_primary=True
            )
        )

    groups: dict[str, WorkspaceGroup] = {}
    channels: dict[str, Channel] = {}
    for name, (manager, rows) in GROUPS.items():
        group = await db.add(WorkspaceGroup(name=name, created_by=admin.id))
        groups[name] = group
        await db.add(GroupManager(group_id=group.id, user_id=users[manager].id))
        for order, (channel_name, kind) in enumerate(rows, start=1):
            channels[channel_name] = await db.add(
                Channel(group_id=group.id, type=kind, name=channel_name, sort_order=order)
            )

    # 전사는 모두 읽고 답글을 쓸 수 있고, 공지는 인사팀만 쓴다.
    for root in TREE:
        await db.grant(groups["전사"], departments[root], **REPLY)
        await db.grant(channels["자유게시판"], departments[root], write="allow")
        # 건의함은 쓰기만 준다. 쓴 사람은 자기 글만 본다.
        await db.grant(channels["건의함"], departments[root], read="deny", write="allow")
    await db.grant(channels["공지"], departments["인사팀"], write="allow")
    await db.grant(channels["건의함"], departments["인사팀"], read="allow", write="allow")

    await db.grant(groups["영업"], departments["영업본부"], **FULL)
    await db.grant(groups["개발"], departments["개발본부"], **FULL)
    # 기획서 판정 예시: 인턴은 사용자 거부, 타 부서 협업자는 사용자 허용.
    await db.grant(channels["영업 일반"], users["jihoon"], write="deny")
    await db.grant(
        channels["고객 미팅"], users["subin"], read="allow", reply_read="allow", reply_write="allow"
    )

    u = users
    notice = await db.post(
        channels["공지"],
        u["seoyeon"],
        "## 바로채널 오픈 안내\n\n오늘부터 사내 소통은 **바로채널**에서 진행합니다.\n\n"
        "- 채널 권한은 소속 기준으로 열립니다\n- 궁금한 점은 답글로 남겨 주세요",
        50,
    )
    notice.pinned_at = db.now - timedelta(hours=49)
    notice.pinned_by = u["seoyeon"].id
    await db.react(notice, "👍", u["minjae"], u["haneul"], u["taehyun"], u["jia"])
    await db.react(notice, "🎉", u["subin"], u["yerin"])
    await db.post(channels["공지"], u["haneul"], "모바일에서도 쓸 수 있나요?", 49, notice)
    await db.post(
        channels["공지"], u["seoyeon"], "네, 휴대폰 브라우저에서 그대로 쓰시면 됩니다.", 48, notice
    )

    holiday = await db.post(
        channels["공지"],
        u["seoyeon"],
        "연말 휴무 일정은 **12월 24일**부터입니다. 팀별 비상 연락망을 확인해 주세요.",
        20,
    )
    await db.attach(holiday, "연말_휴무_안내.txt", "연말 휴무 안내 샘플 문서입니다.\n".encode())

    lunch = await db.post(
        channels["자유게시판"], u["jia"], "회사 앞에 새로 생긴 칼국숫집 맛있어요 😄", 30
    )
    await db.react(lunch, "❤️", u["yerin"], u["subin"])
    await db.post(channels["자유게시판"], u["yerin"], "내일 같이 가요!", 29, lunch)

    edited = await db.post(channels["자유게시판"], u["junho"], "사내 동호회 회원을 모집합니다.", 10)
    await db.add(
        PostRevision(
            post_id=edited.id, version=1, body_md="동호회 모집합니다", edited_by=u["junho"].id
        )
    )
    edited.edited_at = db.now - timedelta(hours=9)

    removed = await db.post(channels["자유게시판"], u["subin"], "잘못 올린 글입니다.", 8)
    removed.deleted_at = db.now - timedelta(hours=7)
    removed.deleted_by = u["subin"].id
    record_audit(
        session,
        actor_id=u["subin"].id,
        action="post.delete",
        target_type="post",
        target_id=removed.id,
        channel_id=removed.channel_id,
    )

    await db.post(channels["건의함"], u["haneul"], "휴게실에 정수기를 한 대 더 놓아 주세요.", 26)
    await db.post(channels["건의함"], u["subin"], "회의실 예약이 어려워요.", 12)

    team = await db.post(
        channels["영업 일반"],
        u["minjae"],
        "이번 달 목표\n\n| 팀 | 목표 |\n| --- | --- |\n| 영업1팀 | 12건 |\n| 영업2팀 | 9건 |",
        28,
    )
    team.pinned_at = db.now - timedelta(hours=27)
    team.pinned_by = u["minjae"].id
    await db.react(team, "✅", u["haneul"], u["yerin"])
    await db.post(channels["영업 일반"], u["haneul"], "1팀 제안서 초안 공유드립니다.", 6)

    meeting = await db.post(
        channels["고객 미팅"],
        u["haneul"],
        "내일 10시 고객사 미팅입니다. 부스 시안 확인 부탁드려요.",
        5,
    )
    await db.attach(meeting, "부스_시안.png", sample_png())
    await db.post(channels["고객 미팅"], u["subin"], "데모 환경은 준비해 두겠습니다.", 4, meeting)

    release = await db.post(
        channels["개발 일반"],
        u["taehyun"],
        "이번 주 배포 일정입니다.\n\n```\n수요일 14:00 스테이징\n목요일 10:00 운영\n```",
        24,
    )
    await db.react(release, "👀", u["subin"], u["jia"])
    await db.post(channels["장애 공유"], u["subin"], "로그인 지연은 원인을 찾아 해결했습니다.", 3)

    record_audit(
        session,
        actor_id=None,
        action="system.seed",
        target_type="system",
        target_id=0,
        users=len(users) + 1,
    )
    await session.commit()

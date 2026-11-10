from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class WorkCommand(Base):
    """업무 채널의 명령어. templates/works/{screen}/{page}.html 화면을 열고, 화면이 부르는 API는
    연동 주소로 중계한다."""

    __tablename__ = "work_command"
    __table_args__ = (UniqueConstraint("channel_id", "name", name="name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"), index=True)
    name: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(300), default="")
    # 화면 폴더 이름과 그 안의 시작 화면. 화면은 코드와 함께 배포한다.
    screen: Mapped[str] = mapped_column(String(40))
    page: Mapped[str] = mapped_column(String(40))
    endpoint_url: Mapped[str] = mapped_column(String(500))
    # 화면에 다시 보여주지 않는다. 암호화 저장은 2차(F-101).
    api_token: Mapped[str] = mapped_column(String(200), default="")
    # 서버 훅 서명 키. 비어 있으면 훅을 받지 않는다. 발급할 때 한 번만 보여준다.
    webhook_secret: Mapped[str] = mapped_column(String(64), default="", server_default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkMessage(Base):
    """업무 화면이나 서버 훅이 채널에 남긴 메시지. 본문은 post에 있고, 고치지 않는다."""

    __tablename__ = "work_message"
    __table_args__ = (UniqueConstraint("command_id", "idempotency_key", name="idempotency"),)

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    command_id: Mapped[int] = mapped_column(ForeignKey("work_command.id"), index=True)
    text: Mapped[str] = mapped_column(String(1000))
    # [{"label": "상태", "value": "할 일"}]
    fields: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list)
    # 상세 화면에 넘길 값. 없으면 상세 보기가 없다.
    detail_params: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    link_label: Mapped[str] = mapped_column(String(100), default="", server_default="")
    link_url: Mapped[str | None] = mapped_column(String(500))
    # 서버 훅으로 남긴 메시지만 외부가 알려준 이름이 있다.
    actor_name: Mapped[str] = mapped_column(String(100), default="", server_default="")
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

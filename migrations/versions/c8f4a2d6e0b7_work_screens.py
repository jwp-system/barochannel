"""work v3: screen-based commands and append-only messages

명령어는 templates/works의 화면을 열고, 채널에는 사용자가 남긴 메시지만 쌓는다.
기존 업무 기록(work_record)은 메시지로 바꾸고, 사본과 변경 기록 테이블은 없앤다.
변경 기록 답글은 일반 답글로 남는다.

Revision ID: c8f4a2d6e0b7
Revises: a3d9e0f1b2c5
Create Date: 2026-11-10 21:00:00.000000

"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8f4a2d6e0b7"
down_revision: str | Sequence[str] | None = "a3d9e0f1b2c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~.-])")


def plain(value: str) -> str:
    return MARKDOWN_SPECIAL.sub(r"\\\1", value)


def upgrade() -> None:
    op.add_column(
        "work_command", sa.Column("screen", sa.String(length=40), server_default="", nullable=False)
    )
    op.add_column(
        "work_command", sa.Column("page", sa.String(length=40), server_default="", nullable=False)
    )
    # 지금까지의 명령어는 모두 샘플 할 일 시스템이다.
    op.execute(
        "UPDATE work_command SET screen = 'todo',"
        " page = CASE WHEN kind = 'search' THEN 'find' ELSE 'index' END"
    )
    op.alter_column("work_command", "screen", server_default=None)
    op.alter_column("work_command", "page", server_default=None)
    op.drop_constraint(op.f("ck_work_command_kind"), "work_command", type_="check")
    op.drop_column("work_command", "kind")

    op.create_table(
        "work_message",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=1000), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detail_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("link_label", sa.String(length=100), server_default="", nullable=False),
        sa.Column("link_url", sa.String(length=500), nullable=True),
        sa.Column("actor_name", sa.String(length=100), server_default="", nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["work_command.id"],
            name=op.f("fk_work_message_command_id_work_command"),
        ),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"], name=op.f("fk_work_message_post_id_post")),
        sa.PrimaryKeyConstraint("post_id", name=op.f("pk_work_message")),
        sa.UniqueConstraint("command_id", "idempotency_key", name="idempotency"),
    )
    op.create_index(op.f("ix_work_message_command_id"), "work_message", ["command_id"])

    bind = op.get_bind()
    records = bind.execute(
        sa.text(
            "SELECT post_id, command_id, request_id, external_id, title, status, external_url,"
            " summary, synced_at FROM work_record"
        )
    ).mappings()
    message = sa.table(
        "work_message",
        sa.column("post_id"),
        sa.column("command_id"),
        sa.column("text"),
        sa.column("fields", postgresql.JSONB),
        sa.column("detail_params", postgresql.JSONB),
        sa.column("link_label"),
        sa.column("link_url"),
        sa.column("idempotency_key"),
        sa.column("created_at"),
    )
    for row in list(records):
        fields = [{"label": "상태", "value": row["status"]}] if row["status"] else []
        fields += [item for item in row["summary"] or [] if item.get("value")]
        fields = [
            {"label": str(item["label"])[:30], "value": str(item["value"])[:200]}
            for item in fields[:6]
        ]
        text = str(row["title"])[:1000]
        bind.execute(
            message.insert().values(
                post_id=row["post_id"],
                command_id=row["command_id"],
                text=text,
                fields=fields,
                detail_params={"id": row["external_id"]},
                link_label="외부 시스템에서 열기" if row["external_url"] else "",
                link_url=row["external_url"],
                idempotency_key=row["request_id"],
                created_at=row["synced_at"],
            )
        )
        body = " · ".join([text] + [f"{item['label']}: {item['value']}" for item in fields])
        bind.execute(
            sa.text("UPDATE post SET title = NULL, body_md = :body WHERE id = :id"),
            {"body": plain(body), "id": row["post_id"]},
        )

    op.drop_index(op.f("ix_work_change_record_post_id"), table_name="work_change")
    op.drop_table("work_change")
    op.drop_index(op.f("ix_work_record_command_id"), table_name="work_record")
    op.drop_table("work_record")


def downgrade() -> None:
    """메시지는 되돌릴 수 없어 지운다. 글(post)은 일반 글로 남는다."""
    op.create_table(
        "work_record",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("command_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("external_url", sa.String(length=500), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("submitted", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["command_id"], ["work_command.id"], name=op.f("fk_work_record_command_id_work_command")
        ),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"], name=op.f("fk_work_record_post_id_post")),
        sa.PrimaryKeyConstraint("post_id", name=op.f("pk_work_record")),
        sa.UniqueConstraint("command_id", "request_id", name="request"),
    )
    op.create_index(op.f("ix_work_record_command_id"), "work_record", ["command_id"])
    op.create_table(
        "work_change",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("record_post_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(length=100), server_default="", nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("action_key", sa.String(length=50), nullable=False),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('edit', 'action', 'external')", name=op.f("ck_work_change_kind")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["user.id"], name=op.f("fk_work_change_actor_id_user")
        ),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"], name=op.f("fk_work_change_post_id_post")),
        sa.ForeignKeyConstraint(
            ["record_post_id"], ["post.id"], name=op.f("fk_work_change_record_post_id_post")
        ),
        sa.PrimaryKeyConstraint("post_id", name=op.f("pk_work_change")),
    )
    op.create_index(op.f("ix_work_change_record_post_id"), "work_change", ["record_post_id"])
    op.drop_index(op.f("ix_work_message_command_id"), table_name="work_message")
    op.drop_table("work_message")
    op.add_column(
        "work_command",
        sa.Column("kind", sa.String(length=10), server_default="create", nullable=False),
    )
    op.execute("UPDATE work_command SET kind = 'search' WHERE page = 'find'")
    op.create_check_constraint(
        op.f("ck_work_command_kind"), "work_command", "kind IN ('create', 'search')"
    )
    op.drop_column("work_command", "page")
    op.drop_column("work_command", "screen")

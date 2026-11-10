"""work channel

Revision ID: e6a4b9c1d2f3
Revises: c3d8e5f17b42
Create Date: 2026-11-10 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6a4b9c1d2f3"
down_revision: str | Sequence[str] | None = "c3d8e5f17b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_channel_type"), "channel", type_="check")
    op.create_check_constraint(
        op.f("ck_channel_type"), "channel", "type IN ('talk', 'vote', 'doc', 'approval', 'work')"
    )
    op.create_table(
        "work_command",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("endpoint_url", sa.String(length=500), nullable=False),
        sa.Column("api_token", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["channel.id"], name=op.f("fk_work_command_channel_id_channel")),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], name=op.f("fk_work_command_created_by_user")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_command")),
        sa.UniqueConstraint("channel_id", "name", name="name"),
    )
    op.create_index(op.f("ix_work_command_channel_id"), "work_command", ["channel_id"])
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
        sa.ForeignKeyConstraint(["command_id"], ["work_command.id"], name=op.f("fk_work_record_command_id_work_command")),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"], name=op.f("fk_work_record_post_id_post")),
        sa.PrimaryKeyConstraint("post_id", name=op.f("pk_work_record")),
        sa.UniqueConstraint("command_id", "request_id", name="request"),
    )
    op.create_index(op.f("ix_work_record_command_id"), "work_record", ["command_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_work_record_command_id"), table_name="work_record")
    op.drop_table("work_record")
    op.drop_index(op.f("ix_work_command_channel_id"), table_name="work_command")
    op.drop_table("work_command")
    op.drop_constraint(op.f("ck_channel_type"), "channel", type_="check")
    op.create_check_constraint(
        op.f("ck_channel_type"), "channel", "type IN ('talk', 'vote', 'doc', 'approval')"
    )

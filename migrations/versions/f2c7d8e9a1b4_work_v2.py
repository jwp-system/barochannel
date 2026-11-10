"""work integration v2: search commands and change log

Revision ID: f2c7d8e9a1b4
Revises: e6a4b9c1d2f3
Create Date: 2026-11-10 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2c7d8e9a1b4"
down_revision: str | Sequence[str] | None = "e6a4b9c1d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_command",
        sa.Column("kind", sa.String(length=10), server_default="create", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_work_command_kind"), "work_command", "kind IN ('create', 'search')"
    )
    op.create_table(
        "work_change",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("record_post_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("action_key", sa.String(length=50), nullable=False),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('edit', 'action')", name=op.f("ck_work_change_kind")),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"], name=op.f("fk_work_change_actor_id_user")),
        sa.ForeignKeyConstraint(["post_id"], ["post.id"], name=op.f("fk_work_change_post_id_post")),
        sa.ForeignKeyConstraint(
            ["record_post_id"], ["post.id"], name=op.f("fk_work_change_record_post_id_post")
        ),
        sa.PrimaryKeyConstraint("post_id", name=op.f("pk_work_change")),
    )
    op.create_index(op.f("ix_work_change_record_post_id"), "work_change", ["record_post_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_work_change_record_post_id"), table_name="work_change")
    op.drop_table("work_change")
    op.drop_constraint(op.f("ck_work_command_kind"), "work_command", type_="check")
    op.drop_column("work_command", "kind")

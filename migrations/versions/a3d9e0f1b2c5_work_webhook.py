"""work webhook: signing secret and external change log

Revision ID: a3d9e0f1b2c5
Revises: f2c7d8e9a1b4
Create Date: 2026-11-10 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d9e0f1b2c5"
down_revision: str | Sequence[str] | None = "f2c7d8e9a1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_command",
        sa.Column("webhook_secret", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "work_change",
        sa.Column("actor_name", sa.String(length=100), server_default="", nullable=False),
    )
    op.alter_column("work_change", "actor_id", existing_type=sa.Integer(), nullable=True)
    op.drop_constraint(op.f("ck_work_change_kind"), "work_change", type_="check")
    op.create_check_constraint(
        op.f("ck_work_change_kind"), "work_change", "kind IN ('edit', 'action', 'external')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM work_change WHERE kind = 'external'")
    op.drop_constraint(op.f("ck_work_change_kind"), "work_change", type_="check")
    op.create_check_constraint(
        op.f("ck_work_change_kind"), "work_change", "kind IN ('edit', 'action')"
    )
    op.alter_column("work_change", "actor_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("work_change", "actor_name")
    op.drop_column("work_command", "webhook_secret")

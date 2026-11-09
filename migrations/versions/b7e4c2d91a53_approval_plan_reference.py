"""approval plan and references

Revision ID: b7e4c2d91a53
Revises: a01b75e45405
Create Date: 2026-11-09 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e4c2d91a53"
down_revision: str | Sequence[str] | None = "a01b75e45405"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("approval", sa.Column("plan", postgresql.JSONB(), nullable=True))
    op.create_table(
        "approval_reference",
        sa.Column("approval_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["approval_id"], ["approval.id"], name=op.f("fk_approval_reference_approval_id_approval")),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name=op.f("fk_approval_reference_user_id_user")),
        sa.PrimaryKeyConstraint("approval_id", "user_id", name=op.f("pk_approval_reference")),
    )
    op.create_index(op.f("ix_approval_reference_user_id"), "approval_reference", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_approval_reference_user_id"), table_name="approval_reference")
    op.drop_table("approval_reference")
    op.drop_column("approval", "plan")

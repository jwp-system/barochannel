"""channel read marks

Revision ID: c3d8e5f17b42
Revises: b7e4c2d91a53
Create Date: 2026-11-09 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8e5f17b42"
down_revision: str | Sequence[str] | None = "b7e4c2d91a53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_read",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("last_post_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channel.id"], name=op.f("fk_channel_read_channel_id_channel")),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name=op.f("fk_channel_read_user_id_user")),
        sa.PrimaryKeyConstraint("user_id", "channel_id", name=op.f("pk_channel_read")),
    )


def downgrade() -> None:
    op.drop_table("channel_read")

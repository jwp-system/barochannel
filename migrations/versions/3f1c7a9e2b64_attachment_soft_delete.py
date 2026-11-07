"""attachment soft delete

Revision ID: 3f1c7a9e2b64
Revises: 8dec9f82b8c8
Create Date: 2026-11-07 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f1c7a9e2b64"
down_revision: str | Sequence[str] | None = "8dec9f82b8c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("attachment", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("attachment", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_attachment_deleted_by_user"), "attachment", "user", ["deleted_by"], ["id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_attachment_deleted_by_user"), "attachment", type_="foreignkey")
    op.drop_column("attachment", "deleted_by")
    op.drop_column("attachment", "deleted_at")

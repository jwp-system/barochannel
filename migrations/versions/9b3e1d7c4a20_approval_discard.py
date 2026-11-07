"""approval discard

Revision ID: 9b3e1d7c4a20
Revises: f4008e03bad6
Create Date: 2026-11-07 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3e1d7c4a20"
down_revision: str | Sequence[str] | None = "f4008e03bad6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASE = "'draft', 'in_progress', 'approved', 'rejected', 'withdrawn'"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("approval", sa.Column("discard_reason", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_approval_status"), "approval", type_="check")
    op.create_check_constraint(
        op.f("ck_approval_status"), "approval", f"status IN ({BASE}, 'discarded')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 폐기 문서는 보존 대상이므로 지우지 않고 가장 가까운 종료 상태로 남긴다.
    op.execute("UPDATE approval SET status = 'withdrawn' WHERE status = 'discarded'")
    op.drop_constraint(op.f("ck_approval_status"), "approval", type_="check")
    op.create_check_constraint(op.f("ck_approval_status"), "approval", f"status IN ({BASE})")
    op.drop_column("approval", "discard_reason")

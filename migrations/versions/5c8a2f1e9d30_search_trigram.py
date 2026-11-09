"""search trigram index

Revision ID: 5c8a2f1e9d30
Revises: 200bfc99d784
Create Date: 2026-11-09 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c8a2f1e9d30"
down_revision: str | Sequence[str] | None = "200bfc99d784"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """한글 부분 검색(ILIKE)이 전체 스캔이 되지 않게 3글자 단위 색인을 둔다."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_post_body_trgm ON post USING gin (body_md gin_trgm_ops)")
    op.execute("CREATE INDEX ix_post_title_trgm ON post USING gin (title gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_post_title_trgm")
    op.execute("DROP INDEX ix_post_body_trgm")

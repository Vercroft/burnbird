"""Queue repositories for a backfill of their jobs, and record when each was backfilled.

``backfill`` starts empty and ``repository.backfilled_at`` is NULL everywhere, so the first
reconciliation after this revision queues every stored repository once. Downgrading drops both.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14 16:37:03.671142
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "backfill",
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("done_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repository.id"],
            name=op.f("fk_backfill_repository_id_repository"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("repository_id", name=op.f("pk_backfill")),
    )
    op.add_column(
        "repository", sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_column("repository", "backfilled_at")
    op.drop_table("backfill")

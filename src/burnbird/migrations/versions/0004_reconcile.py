"""Add the worker's periodic tasks, and when an installation's repository list was last synced.

``worker_task`` starts empty: a task's row is made the first time a worker claims it. Existing
installations get no sync time, so they count as never synced until the next reconciliation.
Downgrading drops both; the next reconciliation after an upgrade fills them again.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14 13:54:18.334573
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "worker_task",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_worker_task")),
    )
    op.add_column(
        "installation",
        sa.Column("repositories_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_column("installation", "repositories_synced_at")
    op.drop_table("worker_task")

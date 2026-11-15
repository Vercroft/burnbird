"""Record each redelivery asked for, when an account's data was deleted, and backfill generations.

``redelivery_request`` starts empty, ``account.data_deleted_at`` is NULL (no deletion is known
from before this revision), every backfill row starts at generation 0, and no gap is handled
yet (``worker_task.gap_handled_for``). Downgrading drops them all.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15 10:02:04.828465
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "redelivery_request",
        sa.Column("guid", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("guid", name=op.f("pk_redelivery_request")),
    )
    op.create_index(
        op.f("ix_redelivery_request_requested_at"),
        "redelivery_request",
        ["requested_at"],
        unique=False,
    )
    op.add_column(
        "account", sa.Column("data_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "backfill",
        sa.Column("generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "worker_task", sa.Column("gap_handled_for", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_column("worker_task", "gap_handled_for")
    op.drop_column("backfill", "generation")
    op.drop_column("account", "data_deleted_at")
    op.drop_index(op.f("ix_redelivery_request_requested_at"), table_name="redelivery_request")
    op.drop_table("redelivery_request")

"""Store a delivery's event, action and validated fields; let a job have no workflow name.

The new delivery columns are NOT NULL without a default: the table holds no row on any server when
this runs, since nothing has received a webhook before it. Downgrading makes ``job.workflow_name``
NOT NULL again, which fails if a job without a workflow name was stored.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13 22:01:46.042736
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.alter_column("job", "workflow_name", existing_type=sa.Text(), nullable=True)
    op.add_column("webhook_delivery", sa.Column("event", sa.Text(), nullable=False))
    op.add_column("webhook_delivery", sa.Column("action", sa.Text(), nullable=False))
    op.add_column(
        "webhook_delivery",
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "webhook_delivery",
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "webhook_delivery", sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_column("webhook_delivery", "retry_at")
    op.drop_column("webhook_delivery", "attempts")
    op.drop_column("webhook_delivery", "payload")
    op.drop_column("webhook_delivery", "action")
    op.drop_column("webhook_delivery", "event")
    op.alter_column("job", "workflow_name", existing_type=sa.Text(), nullable=False)

"""Store whether a user had admin on each repository they could access at their last sign-in.

Account-wide numbers, the forecast and settings are shown to an organisation user only with admin
on every repository of the installation. Rows from before this revision read false, so no one
gains access until their next sign-in stores GitHub's answer. Downgrading drops the column.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14 15:21:11.454557
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.add_column(
        "user_repository",
        sa.Column("admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_column("user_repository", "admin")

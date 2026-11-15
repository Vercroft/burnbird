"""Store each user's GitHub primary verified email address, and a device label per session.

``user_email`` starts empty: an address is read at the next sign-in. Every stored session has no
device label (NULL); new sessions get one. Downgrading drops both.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15 21:00:56.005690
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "user_email",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(address) <= 254 AND position('@' in address) > 1",
            name=op.f("ck_user_email_address"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_user_email_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_email")),
    )
    op.create_index(op.f("ix_user_email_read_at"), "user_email", ["read_at"], unique=False)
    op.add_column("session", sa.Column("device", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_session_device"), "session", "char_length(device) <= 80")


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_constraint(op.f("ck_session_device"), "session", type_="check")
    op.drop_column("session", "device")
    op.drop_index(op.f("ix_user_email_read_at"), table_name="user_email")
    op.drop_table("user_email")

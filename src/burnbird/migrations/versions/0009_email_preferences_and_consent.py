"""Record each user's email choices, and every change of them in an append-only log.

Both tables start empty: the next sign-in that stores an address writes a user's default choices
and their consent rows. Downgrading drops both.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-15 22:08:55.444665
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "kind IN ('usage_warnings', 'weekly_summary', 'news')"


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "email_consent",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("text_version", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(KINDS, name=op.f("ck_email_consent_kind")),
        sa.CheckConstraint(
            "source IN ('default', 'settings', 'one_click', 'complaint')",
            name=op.f("ck_email_consent_source"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_email_consent_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_consent")),
    )
    op.create_index(op.f("ix_email_consent_user_id"), "email_consent", ["user_id"], unique=False)
    op.create_table(
        "email_preference",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(KINDS, name=op.f("ck_email_preference_kind")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_email_preference_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "kind", name=op.f("pk_email_preference")),
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_table("email_preference")
    op.drop_index(op.f("ix_email_consent_user_id"), table_name="email_consent")
    op.drop_table("email_consent")

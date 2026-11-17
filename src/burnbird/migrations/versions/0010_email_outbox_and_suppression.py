"""Add the email outbox, and mark an address no email is sent to.

``email_outbox`` holds one row per email to send, written in the transaction that decides to send
it; ``dedupe_key`` makes a repeated decision a no-op. ``user_email`` gains ``suppressed_at`` and
``suppressed_reason``, set together or not at all. The table starts empty and no address is
suppressed. Downgrading drops both.

Alembic's autogenerate does not compare CHECK constraints, so the two on ``user_email`` are written
here by hand; the email table tests check that the database refuses what they rule out.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17 14:36:56.832833
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribe_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('service', 'usage_warnings')", name=op.f("ck_email_outbox_kind")
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'sent', 'unknown', 'failed', 'skipped')",
            name=op.f("ck_email_outbox_state"),
        ),
        sa.CheckConstraint(
            "template IN ('run_out_warning')", name=op.f("ck_email_outbox_template")
        ),
        sa.CheckConstraint(
            "octet_length(unsubscribe_sha256) = 32",
            name=op.f("ck_email_outbox_unsubscribe_sha256"),
        ),
        sa.CheckConstraint(
            "(state = 'queued') = (finished_at IS NULL)", name=op.f("ck_email_outbox_finished")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name=op.f("fk_email_outbox_account_id_account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_email_outbox_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_email_outbox_dedupe_key")),
        sa.UniqueConstraint("unsubscribe_sha256", name=op.f("uq_email_outbox_unsubscribe_sha256")),
    )
    op.create_index(
        op.f("ix_email_outbox_account_id"), "email_outbox", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_email_outbox_finished_at"), "email_outbox", ["finished_at"], unique=False
    )
    op.create_index(
        op.f("ix_email_outbox_retry_at"),
        "email_outbox",
        ["retry_at"],
        unique=False,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(op.f("ix_email_outbox_user_id"), "email_outbox", ["user_id"], unique=False)
    op.add_column(
        "user_email", sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("user_email", sa.Column("suppressed_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_user_email_suppressed_reason"),
        "user_email",
        "suppressed_reason IN ('hard_bounce', 'complaint')",
    )
    op.create_check_constraint(
        op.f("ck_user_email_suppressed"),
        "user_email",
        "(suppressed_at IS NULL) = (suppressed_reason IS NULL)",
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_constraint(op.f("ck_user_email_suppressed"), "user_email", type_="check")
    op.drop_constraint(op.f("ck_user_email_suppressed_reason"), "user_email", type_="check")
    op.drop_column("user_email", "suppressed_reason")
    op.drop_column("user_email", "suppressed_at")
    op.drop_index(op.f("ix_email_outbox_user_id"), table_name="email_outbox")
    op.drop_index(
        op.f("ix_email_outbox_retry_at"),
        table_name="email_outbox",
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.drop_index(op.f("ix_email_outbox_finished_at"), table_name="email_outbox")
    op.drop_index(op.f("ix_email_outbox_account_id"), table_name="email_outbox")
    op.drop_table("email_outbox")

"""Create the storage tables.

Revision ID: 0001
Revises: none
Create Date: 2026-11-13 21:02:23.691587
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "account",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("github_plan", sa.Text(), nullable=False),
        sa.Column(
            "github_cycle_day", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "blacksmith_cycle_day", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.CheckConstraint("type IN ('User', 'Organization')", name=op.f("ck_account_type")),
        sa.CheckConstraint(
            "blacksmith_cycle_day BETWEEN 1 AND 31", name=op.f("ck_account_blacksmith_cycle_day")
        ),
        sa.CheckConstraint(
            "github_cycle_day BETWEEN 1 AND 31", name=op.f("ck_account_github_cycle_day")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account")),
    )
    op.create_table(
        "github_user",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_user")),
    )
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_delivery")),
    )
    op.create_index(
        op.f("ix_webhook_delivery_received_at"), "webhook_delivery", ["received_at"], unique=False
    )
    op.create_table(
        "installation",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("suspended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name=op.f("fk_installation_account_id_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_installation")),
        sa.UniqueConstraint("account_id", name=op.f("uq_installation_account_id")),
    )
    op.create_table(
        "label_size",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("size", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name=op.f("fk_label_size_account_id_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "label", name=op.f("pk_label_size")),
    )
    op.create_table(
        "session",
        sa.Column("id_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(id_sha256) = 32", name=op.f("ck_session_id_sha256")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_session_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_sha256", name=op.f("pk_session")),
    )
    op.create_index(op.f("ix_session_expires_at"), "session", ["expires_at"], unique=False)
    op.create_index(op.f("ix_session_user_id"), "session", ["user_id"], unique=False)
    op.create_table(
        "repository",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["installation.id"],
            name=op.f("fk_repository_installation_id_installation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository")),
    )
    op.create_index(
        op.f("ix_repository_installation_id"), "repository", ["installation_id"], unique=False
    )
    op.create_table(
        "user_installation",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["installation.id"],
            name=op.f("fk_user_installation_installation_id_installation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_user_installation_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "installation_id", name=op.f("pk_user_installation")),
    )
    op.create_index(
        op.f("ix_user_installation_installation_id"),
        "user_installation",
        ["installation_id"],
        unique=False,
    )
    op.create_table(
        "job",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("run_attempt", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("labels", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("runner_group_name", sa.Text(), nullable=True),
        sa.Column("runner_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("runner_class", sa.Text(), nullable=True),
        sa.Column("billed_minutes", sa.Numeric(), nullable=True),
        sa.Column("amount", sa.Numeric(), nullable=True),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repository.id"],
            name=op.f("fk_job_repository_id_repository"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", "run_attempt", name=op.f("pk_job")),
    )
    op.create_index(op.f("ix_job_created_at"), "job", ["created_at"], unique=False)
    op.create_index(
        op.f("ix_job_repository_id_completed_at"),
        "job",
        ["repository_id", "completed_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_index(op.f("ix_job_repository_id_completed_at"), table_name="job")
    op.drop_index(op.f("ix_job_created_at"), table_name="job")
    op.drop_table("job")
    op.drop_index(op.f("ix_user_installation_installation_id"), table_name="user_installation")
    op.drop_table("user_installation")
    op.drop_index(op.f("ix_repository_installation_id"), table_name="repository")
    op.drop_table("repository")
    op.drop_index(op.f("ix_session_user_id"), table_name="session")
    op.drop_index(op.f("ix_session_expires_at"), table_name="session")
    op.drop_table("session")
    op.drop_table("label_size")
    op.drop_table("installation")
    op.drop_index(op.f("ix_webhook_delivery_received_at"), table_name="webhook_delivery")
    op.drop_table("webhook_delivery")
    op.drop_table("github_user")
    op.drop_table("account")

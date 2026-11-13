"""Store the repositories a user could access at their last sign-in.

GitHub lists an installation for a user who can access any one of its repositories, so pages that
name a repository need the user's own repository list. Downgrading drops the table; the next
sign-in fills it again.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13 23:02:52.569542
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the schema."""
    op.create_table(
        "user_repository",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repository.id"],
            name=op.f("fk_user_repository_repository_id_repository"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["github_user.id"],
            name=op.f("fk_user_repository_user_id_github_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "repository_id", name=op.f("pk_user_repository")),
    )
    op.create_index(
        op.f("ix_user_repository_repository_id"),
        "user_repository",
        ["repository_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade the schema."""
    op.drop_index(op.f("ix_user_repository_repository_id"), table_name="user_repository")
    op.drop_table("user_repository")

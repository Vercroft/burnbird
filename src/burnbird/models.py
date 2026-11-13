"""The stored tables: accounts, installations, repositories, jobs, users, sessions and settings.

Job metadata only. No token, no raw webhook body and no email address is stored; a session is kept
as the SHA-256 of its ID, never the ID itself. GitHub's own IDs are the primary keys.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names, so a later migration can name what it changes.
NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The declarative base of every table; times are always stored with their time zone."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        str: Text(),
    }


def _github_id() -> Mapped[int]:
    # GitHub assigns the ID; the database never generates one.
    return mapped_column(BigInteger, primary_key=True, autoincrement=False)


def _cycle_day() -> Mapped[int]:
    return mapped_column(SmallInteger, server_default=text("1"))


class Account(Base):
    """A GitHub user or organisation account the App is installed on, with its settings."""

    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint("type IN ('User', 'Organization')", name="type"),
        CheckConstraint("github_cycle_day BETWEEN 1 AND 31", name="github_cycle_day"),
        CheckConstraint("blacksmith_cycle_day BETWEEN 1 AND 31", name="blacksmith_cycle_day"),
    )

    id: Mapped[int] = _github_id()
    login: Mapped[str]
    type: Mapped[str]
    # A plan name of the GitHub provider in providers.toml, chosen by the user.
    github_plan: Mapped[str]
    github_cycle_day: Mapped[int] = _cycle_day()
    blacksmith_cycle_day: Mapped[int] = _cycle_day()


class Installation(Base):
    """One installation of the App; an account has at most one."""

    __tablename__ = "installation"

    id: Mapped[int] = _github_id()
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime]
    suspended: Mapped[bool] = mapped_column(server_default=false())


class Repository(Base):
    """A repository an installation covers."""

    __tablename__ = "repository"

    id: Mapped[int] = _github_id()
    installation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("installation.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str]
    private: Mapped[bool]


class Job(Base):
    """One attempt of one job, classified and billed.

    ``billed_minutes`` is None when the job is not billed (no runner took it, or it has no start or
    completion time). ``amount`` is dollars for a priced runner and allowance units for a weighted
    one, None when the provider is unknown; see ``counting.Billed``.
    """

    __tablename__ = "job"
    __table_args__ = (
        Index(None, "repository_id", "completed_at"),
        Index(None, "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    run_attempt: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    run_id: Mapped[int] = mapped_column(BigInteger)
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repository.id", ondelete="CASCADE")
    )
    workflow_name: Mapped[str]
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text))
    runner_group_name: Mapped[str | None]
    runner_name: Mapped[str | None]
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    conclusion: Mapped[str | None]
    provider: Mapped[str]
    runner_class: Mapped[str | None]
    billed_minutes: Mapped[Decimal | None] = mapped_column(Numeric)
    amount: Mapped[Decimal | None] = mapped_column(Numeric)


class GithubUser(Base):
    """A person who signed in with GitHub."""

    __tablename__ = "github_user"

    id: Mapped[int] = _github_id()
    login: Mapped[str]
    last_sign_in_at: Mapped[datetime]


class UserInstallation(Base):
    """An installation a user could see at their last sign-in."""

    __tablename__ = "user_installation"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), primary_key=True
    )
    installation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("installation.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class UserSession(Base):
    """A signed-in session, stored as the SHA-256 of its ID."""

    __tablename__ = "session"
    __table_args__ = (CheckConstraint("octet_length(id_sha256) = 32", name="id_sha256"),)

    id_sha256: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)


class WebhookDelivery(Base):
    """A webhook delivery seen, by GitHub's delivery ID, so a repeat is refused."""

    __tablename__ = "webhook_delivery"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(index=True)
    processed_at: Mapped[datetime | None]


class LabelSize(Base):
    """The runner size a user chose for one of the account's GitHub larger-runner labels."""

    __tablename__ = "label_size"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(primary_key=True)
    # The id of a larger-runner row in providers.toml.
    size: Mapped[str]

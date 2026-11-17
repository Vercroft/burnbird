"""The stored tables: accounts, installations, repositories, jobs, users, sessions and settings.

Job metadata, and for each signed-in user their GitHub primary verified email address. No token
and no raw webhook body is stored; a session is kept as the SHA-256 of its ID, never the ID itself,
with a coarse label of its device, never the raw header. GitHub's own IDs are the primary keys.
The worker's periodic tasks are stored here too. PRIVACY.md lists every stored item.
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
    Identity,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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


# The kinds of email a user chooses, and where a recorded choice came from.
EMAIL_KINDS: Final = ("usage_warnings", "weekly_summary", "news")
CONSENT_SOURCES: Final = ("default", "settings", "one_click", "complaint")
# The kinds the outbox sends: service email, always sent, and run-out warnings. News is never sent
# through it: the provider takes transactional email only.
OUTBOX_KINDS: Final = ("service", "usage_warnings")
OUTBOX_TEMPLATES: Final = ("run_out_warning",)
OUTBOX_STATES: Final = ("queued", "sent", "unknown", "failed", "skipped")
# Why email to an address stopped: it bounced for good, or the user marked a message as spam.
SUPPRESSION_REASONS: Final = ("hard_bounce", "complaint")

# The longest email address ``user_email`` stores; its CHECK and sign-in's own check share it.
EMAIL_MAX_LENGTH: Final = 254


def storable_address(address: str) -> bool:
    """Return whether ``user_email``'s CHECK accepts the address: not too long, "@" not first."""
    return len(address) <= EMAIL_MAX_LENGTH and address.find("@") > 0


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
    # When its user last asked to delete their data; no job created before it is stored again.
    data_deleted_at: Mapped[datetime | None]


class Installation(Base):
    """One installation of the App; an account has at most one."""

    __tablename__ = "installation"

    id: Mapped[int] = _github_id()
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime]
    suspended: Mapped[bool] = mapped_column(server_default=false())
    # When the hourly reconciliation last stored GitHub's full repository list; None until then.
    repositories_synced_at: Mapped[datetime | None]


class Repository(Base):
    """A repository an installation covers."""

    __tablename__ = "repository"

    id: Mapped[int] = _github_id()
    installation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("installation.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str]
    private: Mapped[bool]
    # When a backfill last stored this repository's jobs since its cycle start; None until then.
    backfilled_at: Mapped[datetime | None]


class Backfill(Base):
    """A repository queued for a backfill of its jobs since the cycle start.

    A worker leases a due row by moving ``retry_at`` forward; ``attempts`` counts the errors that
    put it off, never the rate limits; ``done_until`` is how far it got. See
    ``burnbird.github.backfill``.
    """

    __tablename__ = "backfill"

    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repository.id", ondelete="CASCADE"), primary_key=True
    )
    retry_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    # The end of the last range whose runs were all stored; the next pass starts after it.
    done_until: Mapped[datetime | None]
    # Raised by each re-queue; a pass writes only while the generation it claimed is unchanged.
    generation: Mapped[int] = mapped_column(Integer, server_default=text("0"))


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
    # Nullable in GitHub's workflow job schema.
    workflow_name: Mapped[str | None]
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


class UserRepository(Base):
    """A repository a user could access at their last sign-in, and whether they had admin on it.

    An installation is listed for a user who can access any one of its repositories, so pages that
    name a repository filter by this table, not by ``user_installation``. ``admin`` is GitHub's
    ``permissions.admin`` for the user; it decides account-wide access (``account_access``).
    """

    __tablename__ = "user_repository"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repository.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    admin: Mapped[bool] = mapped_column(server_default=false())


class UserSession(Base):
    """A signed-in session, stored as the SHA-256 of its ID."""

    __tablename__ = "session"
    __table_args__ = (
        CheckConstraint("octet_length(id_sha256) = 32", name="id_sha256"),
        CheckConstraint("char_length(device) <= 80", name="device"),
    )

    id_sha256: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    # A coarse label such as "Firefox 140 on Linux, computer", from ``burnbird.auth.device``.
    device: Mapped[str | None]


def _one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=column)


class UserEmail(Base):
    """A user's GitHub primary verified email address, as read at their last sign-in.

    Only an address GitHub marks both primary and verified is stored, so there is no flag for
    either. ``read_at`` is when a sign-in last read it; one not read again within
    ``retention.EMAIL_FRESHNESS`` is deleted. ``suppressed_at`` and its reason mark an address no
    email is sent to: it bounced for good, or the user marked a message as spam.
    """

    __tablename__ = "user_email"
    __table_args__ = (
        # The same rule as ``storable_address``.
        CheckConstraint(
            f"char_length(address) <= {EMAIL_MAX_LENGTH} AND position('@' in address) > 1",
            name="address",
        ),
        _one_of("suppressed_reason", SUPPRESSION_REASONS),
        CheckConstraint(
            "(suppressed_at IS NULL) = (suppressed_reason IS NULL)", name="suppressed"
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), primary_key=True
    )
    address: Mapped[str]
    read_at: Mapped[datetime] = mapped_column(index=True)
    suppressed_at: Mapped[datetime | None]
    suppressed_reason: Mapped[str | None]


class EmailPreference(Base):
    """Whether a user wants one kind of email; written for every kind when an address is stored."""

    __tablename__ = "email_preference"
    __table_args__ = (_one_of("kind", EMAIL_KINDS),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(primary_key=True)
    enabled: Mapped[bool]
    updated_at: Mapped[datetime]


class EmailConsent(Base):
    """One recorded choice about one kind of email: append-only, never updated.

    ``text_version`` names the wording the user saw (``burnbird.email.consent.TEXT_VERSION``).
    """

    __tablename__ = "email_consent"
    __table_args__ = (_one_of("kind", EMAIL_KINDS), _one_of("source", CONSENT_SOURCES))

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str]
    enabled: Mapped[bool]
    source: Mapped[str]
    text_version: Mapped[str]
    at: Mapped[datetime]


class EmailOutbox(Base):
    """One email to send, written in the transaction that decides to send it.

    ``dedupe_key`` makes a second decision for the same email a no-op. ``params`` holds IDs,
    kinds and dates, never an address or a login. ``outcome`` is a short code (for example
    ``http:400`` or ``suppressed``), never the provider's text. A usage email's unsubscribe link
    is stored as the SHA-256 of its token only, as a session is. A finished row is kept 62 days
    (``retention.OUTBOX_RETENTION``).
    """

    __tablename__ = "email_outbox"
    __table_args__ = (
        _one_of("kind", OUTBOX_KINDS),
        _one_of("template", OUTBOX_TEMPLATES),
        _one_of("state", OUTBOX_STATES),
        CheckConstraint("octet_length(unsubscribe_sha256) = 32", name="unsubscribe_sha256"),
        # A queued row is not finished; a final one says when it finished, so the purge finds it.
        CheckConstraint("(state = 'queued') = (finished_at IS NULL)", name="finished"),
        # The rows a worker may claim.
        Index(None, "retry_at", postgresql_where=text("state = 'queued'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user.id", ondelete="CASCADE"), index=True
    )
    # The account a usage email is about.
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str]
    template: Mapped[str]
    dedupe_key: Mapped[str] = mapped_column(unique=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    retry_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    state: Mapped[str] = mapped_column(server_default=text("'queued'"))
    outcome: Mapped[str | None]
    # When the row reached a final state; the rolling caps and the 62-day purge count from it.
    finished_at: Mapped[datetime | None] = mapped_column(index=True)
    unsubscribe_sha256: Mapped[bytes | None] = mapped_column(LargeBinary, unique=True)
    provider_request_id: Mapped[str | None]


class WebhookDelivery(Base):
    """A webhook delivery we handle, by GitHub's delivery ID, so a repeat is refused.

    ``payload`` holds only the fields that ``burnbird.github.schemas`` validates for the event and
    action, never the received body. A delivery that fails is tried again from ``retry_at`` until
    ``attempts`` reaches the worker's limit.
    """

    __tablename__ = "webhook_delivery"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(index=True)
    processed_at: Mapped[datetime | None]
    event: Mapped[str]
    action: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    retry_at: Mapped[datetime | None]


class LabelSize(Base):
    """The runner size a user chose for one of the account's GitHub larger-runner labels."""

    __tablename__ = "label_size"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(primary_key=True)
    # The id of a larger-runner row in providers.toml.
    size: Mapped[str]


class WorkerTask(Base):
    """A periodic worker task: when it is next due, and when a run last finished all its work.

    A worker claims a run by moving ``next_run_at`` forward in one conditional UPDATE, so two
    workers never run the same task at once; see ``burnbird.github.schedule``.
    """

    __tablename__ = "worker_task"

    name: Mapped[str] = mapped_column(primary_key=True)
    next_run_at: Mapped[datetime]
    last_success_at: Mapped[datetime | None]
    # The last success whose gap a run already handled, so a gap is acted on once (redelivery).
    gap_handled_for: Mapped[datetime | None]


class RedeliveryRequest(Base):
    """A failed webhook delivery Burnbird asked GitHub to redeliver, so it is asked only once."""

    __tablename__ = "redelivery_request"

    guid: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(index=True)
    # When its event was found lost and its repository re-queued, so that happens once.
    requeued_at: Mapped[datetime | None]

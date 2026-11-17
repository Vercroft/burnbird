"""The email tables refuse what the design rules out, so a bug cannot store it."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from burnbird.models import EmailOutbox, GithubUser, UserEmail

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
USER = 7451


@pytest.fixture
def user(session: Session) -> int:
    session.add(GithubUser(id=USER, login="example-recipient", last_sign_in_at=NOW))
    session.flush()
    return USER


def _outbox(**changes: Any) -> EmailOutbox:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": USER,
        "kind": "usage_warnings",
        "template": "run_out_warning",
        "dedupe_key": f"example:{uuid.uuid4()}",
        "params": {},
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=1),
    }
    return EmailOutbox(**(values | changes))


def _address(**changes: Any) -> UserEmail:
    return UserEmail(
        **({"user_id": USER, "address": "a" + "@" + "example.invalid", "read_at": NOW} | changes)
    )


def _refused(session: Session, row: object) -> None:
    with session.begin_nested():
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()


def test_a_queued_email_starts_with_no_attempt_and_no_outcome(session: Session, user: int) -> None:
    row = _outbox()
    session.add(row)
    session.flush()
    session.refresh(row)
    assert (row.state, row.attempts, row.outcome, row.finished_at) == ("queued", 0, None, None)


@pytest.mark.parametrize(
    "row",
    [
        # News is never sent through the outbox: the provider takes transactional email only.
        lambda: _outbox(kind="news"),
        lambda: _outbox(template="newsletter"),
        lambda: _outbox(state="sending"),
        lambda: _outbox(unsubscribe_sha256=bytes(31)),
        # A queued row is not finished, and a final one always says when it finished.
        lambda: _outbox(finished_at=NOW),
        lambda: _outbox(state="sent"),
    ],
    ids=[
        "news",
        "unknown template",
        "unknown state",
        "short hash",
        "queued but finished",
        "final but not finished",
    ],
)
def test_the_outbox_refuses_what_the_design_rules_out(
    session: Session, user: int, row: Callable[[], EmailOutbox]
) -> None:
    _refused(session, row())


def test_one_dedupe_key_is_stored_once(session: Session, user: int) -> None:
    session.add(_outbox(dedupe_key="example:once"))
    session.flush()
    _refused(session, _outbox(dedupe_key="example:once"))


def test_one_unsubscribe_hash_is_stored_once(session: Session, user: int) -> None:
    session.add(_outbox(unsubscribe_sha256=bytes(32)))
    session.flush()
    _refused(session, _outbox(unsubscribe_sha256=bytes(32)))


@pytest.mark.parametrize(
    "changes",
    [
        {"suppressed_at": NOW},
        {"suppressed_reason": "hard_bounce"},
        {"suppressed_at": NOW, "suppressed_reason": "soft_bounce"},
    ],
    ids=["time without reason", "reason without time", "unknown reason"],
)
def test_a_suppression_needs_both_its_time_and_a_known_reason(
    session: Session, user: int, changes: dict[str, Any]
) -> None:
    _refused(session, _address(**changes))


@pytest.mark.parametrize("reason", ["hard_bounce", "complaint"])
def test_a_suppression_with_its_time_and_reason_is_stored(
    session: Session, user: int, reason: str
) -> None:
    session.add(_address(suppressed_at=NOW, suppressed_reason=reason))
    session.flush()

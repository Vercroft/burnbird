import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.models import WebhookDelivery
from burnbird.worker import run_once


def test_one_pass_processes_the_due_deliveries(connection: Connection, session: Session) -> None:
    now = datetime.now(UTC)
    for n in (1, 2):
        session.execute(
            insert(WebhookDelivery).values(
                id=uuid.UUID(int=n),
                received_at=now,
                event="installation",
                action="unsuspend",
                payload={"installation": {"id": 9999}},
            )
        )
    session.flush()
    sessions = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    assert run_once(sessions, now) == 2
    assert run_once(sessions, now) == 0

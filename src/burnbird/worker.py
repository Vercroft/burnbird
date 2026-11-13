"""The worker command's work: one pass over everything that is due."""

from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from burnbird.github import events

# Bounds one pass, so a burst of deliveries cannot keep a pass running without end.
DELIVERIES_PER_PASS: Final = 1000


def run_once(sessions: sessionmaker[Session], now: datetime) -> int:
    """Run one pass and return how many webhook deliveries it took."""
    return events.process_pending(sessions, now, DELIVERIES_PER_PASS)

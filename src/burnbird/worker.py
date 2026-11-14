"""The worker command's work: one pass over everything that is due.

A pass takes the stored webhook deliveries first, then the periodic tasks that are due. Each step
is an isolation point: a failure is logged and the next step still runs, so one broken step never
stops the others. The loop that repeats passes belongs to the worker command.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from burnbird.github import events, reconcile
from burnbird.github.client import GithubApp

logger = logging.getLogger(__name__)

# Bounds one pass, so a burst of deliveries cannot keep a pass running without end.
DELIVERIES_PER_PASS: Final = 1000


def run_once(sessions: sessionmaker[Session], github: GithubApp, now: datetime) -> int:
    """Run one pass and return how many webhook deliveries it took."""
    taken = _step(
        "deliveries", lambda: events.process_pending(sessions, now, DELIVERIES_PER_PASS), 0
    )
    _step("reconcile", lambda: reconcile.run(sessions, github, now), False)
    return taken


def _step[T](name: str, work: Callable[[], T], failed: T) -> T:
    try:
        return work()
    except Exception:
        logger.exception("worker: the %s step failed", name)
        return failed

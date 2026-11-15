"""The worker's daily purge: everything past its time limit is deleted once a day.

The run is claimed through ``burnbird.github.schedule`` like the hourly tasks, so two workers purge
once, and the purges run in one transaction (``retention.run_purges``): a failure deletes nothing,
and the next run is a day later.
"""

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from burnbird import retention
from burnbird.github import schedule

TASK: Final = "purge"
INTERVAL: Final = timedelta(days=1)


def run(sessions: sessionmaker[Session], now: datetime) -> bool:
    """Run the purge if it is due; return whether a run was claimed."""
    claimed = schedule.claim(sessions, TASK, now, INTERVAL)
    if claimed is None:
        return False
    with sessions.begin() as session:
        retention.run_purges(session, now)
    schedule.finish(sessions, claimed, complete=True)
    return True

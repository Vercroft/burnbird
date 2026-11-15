"""The hourly redelivery: ask GitHub, once each, to redeliver webhook deliveries that failed.

"GitHub does not automatically redeliver failed deliveries", and keeps them for "the past 3 days"
(https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).
So every hour the App's deliveries since an hour before the last complete run (at most 3 days
back) are listed from ``GET /app/hook/deliveries`` and grouped by GUID, which "is constant across
redeliveries of the same delivery"
(https://docs.github.com/en/webhooks/using-webhooks/automatically-redelivering-failed-deliveries-for-a-github-app-webhook).

An attempt succeeded only with a status code of 200 to 399; a timed-out one has no usable code and
counts as failed. A GUID is asked for, by its newest attempt, only when no attempt succeeded, none
is a redelivery already, none was refused by us with 400 or 413 (the same body would be refused
again), it never reached us, and its installation is stored. Its record in
``redelivery_request`` is written before the request, so it is asked once, even by two workers; a
request that a rate limit stopped removes its record again, so it is asked on a later run. At most
``MAX_REQUESTS`` are asked a run, one second apart (the client spaces its POSTs); past that the
run is not complete, so the next run keeps the same window.

What stays lost is filled by the backfill (``burnbird.github.backfill.requeue``), from the cycle
start, once per cause: a job event that will not be sent again (we refused it with 400 or 413,
our request for it failed with anything but a rate limit, or its redelivery failed too; marked
in ``redelivery_request``), and every repository after a gap whose window would reach past
GitHub's 3 days (marked in the task's ``gap_handled_for``). A run that stays incomplete never
re-queues the same cause again. The order of the list is not documented, so paging stops only at
a page whose every delivery is older than the window.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import AwareDatetime, ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from burnbird.github import backfill, schedule, schemas
from burnbird.github.client import GithubAnswerError, GithubApp, RateLimitedError
from burnbird.models import (
    Installation,
    RedeliveryRequest,
    Repository,
    WebhookDelivery,
    WorkerTask,
)

logger = logging.getLogger(__name__)

TASK: Final = "redeliver"
INTERVAL: Final = timedelta(hours=1)
# GitHub keeps deliveries this long, and the window reaches back this far before a last success.
KEPT: Final = timedelta(days=3)
OVERLAP: Final = timedelta(hours=1)
MAX_REQUESTS: Final = 50
# What our own endpoint refused for the request itself: the same body would be refused again.
NEVER_ASKED: Final = frozenset({400, 413})


class _Delivery(schemas.Payload):
    id: int
    guid: uuid.UUID
    delivered_at: AwareDatetime
    redelivery: bool
    status_code: int | None = None
    event: str
    installation_id: int | None = None
    repository_id: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 400


def run(sessions: sessionmaker[Session], github: GithubApp, now: datetime) -> bool:
    """Run the redelivery if it is due; return whether a run was claimed."""
    claimed = schedule.claim(sessions, TASK, now, INTERVAL)
    if claimed is None:
        return False
    try:
        complete = redeliver(sessions, github, now, claimed.last_success_at)
    except RateLimitedError as err:
        logger.warning("redeliver: rate limited; put off")
        schedule.finish(sessions, claimed, complete=False, resume_at=err.resume_at)
        return True
    schedule.finish(sessions, claimed, complete=complete)
    return True


def redeliver(
    sessions: sessionmaker[Session],
    github: GithubApp,
    now: datetime,
    last_success_at: datetime | None,
) -> bool:
    """Ask for each failed delivery in the window once; return whether everything was done."""
    window = now - KEPT
    if last_success_at is not None:
        window = max(window, last_success_at - OVERLAP)
        # The window with its hour of overlap would start before GitHub's 3 days: some
        # deliveries may be gone, so everything is backfilled again, once for this gap.
        if now - last_success_at > KEPT - OVERLAP:
            _requeue_everything(sessions, last_success_at)
    groups: dict[uuid.UUID, list[_Delivery]] = defaultdict(list)
    for delivery in _deliveries(github, window):
        groups[delivery.guid].append(delivery)
    with sessions.begin() as session:
        reached = set(
            session.scalars(select(WebhookDelivery.id).where(WebhookDelivery.id.in_(groups)))
        )
        installations = set(session.scalars(select(Installation.id)))
        lost = {
            guid: attempts[0].repository_id
            for guid, attempts in groups.items()
            if _lost(attempts, last_success_at) and attempts[0].repository_id is not None
        }
        # Each lost event re-queues its repository once, however many runs still see it.
        first_seen = set(_mark_requeued(session, lost, now))
        repositories = {repository for guid, repository in lost.items() if guid in first_seen}
        if repositories:
            requeued = backfill.requeue(session, repositories)
            logger.info("redeliver: %d repositories with a lost job event re-queued", requeued)
        session.execute(
            delete(RedeliveryRequest).where(RedeliveryRequest.requested_at < now - KEPT)
        )
    askable = [
        max(attempts, key=lambda a: a.delivered_at)
        for guid, attempts in groups.items()
        if guid not in reached and _askable(attempts, installations)
    ]
    asked = 0
    for delivery in sorted(askable, key=lambda a: a.delivered_at):
        if asked == MAX_REQUESTS:
            logger.info("redeliver: %d deliveries left for the next run", len(askable) - asked)
            return False
        if _ask(sessions, github, delivery, now):
            asked += 1
    return True


def _deliveries(github: GithubApp, window: datetime) -> Iterable[_Delivery]:
    """Every delivery from ``window`` on, stopping at the first page wholly older than it."""
    found: list[_Delivery] = []
    for page in github.pages("app/hook/deliveries"):
        try:
            if not isinstance(page, list):
                raise GithubAnswerError("GET /app/hook/deliveries answered a page not as a list")
            parsed = [_Delivery.model_validate(item) for item in page]
        except ValidationError:
            # Pydantic's message would repeat the values; the call is enough.
            raise GithubAnswerError(
                "GET /app/hook/deliveries answered a delivery not as documented"
            ) from None
        inside = [delivery for delivery in parsed if delivery.delivered_at >= window]
        found.extend(inside)
        if not inside:
            break
    return found


def _askable(attempts: Sequence[_Delivery], installations: set[int]) -> bool:
    return (
        not any(a.succeeded for a in attempts)
        and not any(a.redelivery for a in attempts)
        and not any(a.status_code in NEVER_ASKED for a in attempts)
        and attempts[0].installation_id in installations
    )


def _lost(attempts: Sequence[_Delivery], last_success_at: datetime | None) -> bool:
    """Return whether a job event failed and will not be sent again.

    Either we refused it with 400 or 413, so it is never asked for, or its redelivery failed
    since the last complete run.
    """
    return (
        attempts[0].event == "workflow_job"
        and not any(a.succeeded for a in attempts)
        and (
            any(a.status_code in NEVER_ASKED for a in attempts)
            or any(
                a.redelivery and (last_success_at is None or a.delivered_at > last_success_at)
                for a in attempts
            )
        )
    )


def _ask(
    sessions: sessionmaker[Session], github: GithubApp, delivery: _Delivery, now: datetime
) -> bool:
    """Ask for one redelivery, once; return whether a request was sent."""
    with sessions.begin() as session:
        recorded = session.execute(
            insert(RedeliveryRequest)
            .values(guid=delivery.guid, requested_at=now)
            .on_conflict_do_nothing(index_elements=[RedeliveryRequest.guid])
            .returning(RedeliveryRequest.guid)
        ).first()
    if recorded is None:
        return False
    try:
        github.post(f"app/hook/deliveries/{delivery.id}/attempts")
    except RateLimitedError:
        # Not done: remove the record, so a later run asks again.
        with sessions.begin() as session:
            session.execute(
                delete(RedeliveryRequest).where(RedeliveryRequest.guid == delivery.guid)
            )
        raise
    except GithubAnswerError as err:
        # Refused, failed or never answered (a network error): it will not be resent.
        logger.warning(
            "redeliver: delivery %d answered %s; not asked again", delivery.id, err.status
        )
        _requeue_unsent(sessions, delivery, now)
    return True


def _requeue_unsent(sessions: sessionmaker[Session], delivery: _Delivery, now: datetime) -> None:
    """Re-queue the repository of a job event whose redelivery request failed, once."""
    if delivery.event != "workflow_job" or delivery.repository_id is None:
        return
    with sessions.begin() as session:
        if _mark_requeued(session, [delivery.guid], now):
            requeued = backfill.requeue(session, [delivery.repository_id])
            logger.info("redeliver: %d repositories with an unsent job event re-queued", requeued)


def _mark_requeued(
    session: Session, lost: Collection[uuid.UUID], now: datetime
) -> list[uuid.UUID]:
    """Mark the lost events not marked yet, and return them; a GUID is marked only once."""
    if not lost:
        return []
    marked = session.execute(
        insert(RedeliveryRequest)
        .values([{"guid": guid, "requested_at": now, "requeued_at": now} for guid in lost])
        .on_conflict_do_update(
            index_elements=[RedeliveryRequest.guid],
            set_={"requeued_at": now},
            where=RedeliveryRequest.requeued_at.is_(None),
        )
        .returning(RedeliveryRequest.guid)
    )
    return list(marked.scalars())


def _requeue_everything(sessions: sessionmaker[Session], last_success_at: datetime) -> None:
    """After a gap longer than GitHub keeps deliveries, backfill every repository again, once."""
    with sessions.begin() as session:
        # The task's row exists once a run was claimed; made here otherwise, so the mark holds.
        handled = session.execute(
            insert(WorkerTask)
            .values(name=TASK, next_run_at=last_success_at, gap_handled_for=last_success_at)
            .on_conflict_do_update(
                index_elements=[WorkerTask.name],
                set_={"gap_handled_for": last_success_at},
                where=or_(
                    WorkerTask.gap_handled_for.is_(None),
                    WorkerTask.gap_handled_for != last_success_at,
                ),
            )
            .returning(WorkerTask.name)
        ).first()
        if handled is None:
            return
        repositories: list[Any] = list(
            session.scalars(
                select(Repository.id)
                .join(Installation, Installation.id == Repository.installation_id)
                .where(Installation.suspended.is_(False))
            )
        )
        requeued = backfill.requeue(session, repositories)
    logger.warning("redeliver: a gap longer than %s; %d repositories re-queued", KEPT, requeued)

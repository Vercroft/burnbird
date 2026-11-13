"""Which provider ran a job, on which runner size, and what a minute of it costs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fnmatch import fnmatchcase

from burnbird.billing.providers import (
    BLACKSMITH,
    GITHUB,
    SELF_HOSTED,
    Kind,
    RunnerClass,
    load_table,
)

UNKNOWN = "unknown"

# The runner group that jobs on GitHub's standard hosted runners report. It is observed in the jobs
# API, not stated on the docs pages read: this repository's own CI runs on 2026-11-12 return
# runner_group_name "GitHub Actions", runner_name "GitHub Actions <runner id>" and labels
# ["ubuntu-latest"]. The docs say only that "Runner groups can only have larger runners or
# self-hosted runners as members":
# https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job
GITHUB_HOSTED_GROUP = "GitHub Actions"


@dataclass(frozen=True, slots=True)
class Classification:
    """Where a job ran and the cost of one minute of it.

    A GitHub or self-hosted minute has a price in US dollars; a Blacksmith minute has a weight, in
    units of its free allowance. ``runner`` is None only when the provider is unknown.
    """

    provider: str
    runner: RunnerClass | None
    usd_per_minute: Decimal | None
    weight: Decimal | None


def classify(
    labels: Sequence[str],
    runner_group_name: str | None,
    private: bool,
    label_sizes: Mapping[str, str] | None = None,
) -> Classification:
    """Classify one job by its labels and runner group; first match wins.

    ``private`` is the repository's private flag. ``label_sizes`` is the user's setting: a GitHub
    larger runner's label mapped to the id of its row in the provider table. The order is a
    Blacksmith label, the self-hosted label, a GitHub standard label in the GitHub-hosted runner
    group, a GitHub larger runner, and otherwise unknown, which is returned like any other result.
    """
    runners = load_table().runners
    if (row := _first(_rows(runners, BLACKSMITH), labels)) is not None:
        return Classification(BLACKSMITH, row, None, row.weight)
    if (row := _first(_rows(runners, SELF_HOSTED), labels)) is not None:
        return Classification(SELF_HOSTED, row, row.usd_per_minute, None)
    standard = _rows(runners, GITHUB, Kind.STANDARD)
    if runner_group_name == GITHUB_HOSTED_GROUP and (row := _first(standard, labels)) is not None:
        # Standard runners are free in public repositories; the size is still returned.
        return Classification(GITHUB, row, row.usd_per_minute if private else Decimal(0), None)
    if (
        row := _larger(_rows(runners, GITHUB, Kind.LARGER), labels, label_sizes or {})
    ) is not None:
        # "Larger runners are not free for public repositories."
        # https://docs.github.com/en/billing/reference/actions-runner-pricing
        return Classification(GITHUB, row, row.usd_per_minute, None)
    return Classification(UNKNOWN, None, None, None)


def _larger(
    larger: tuple[RunnerClass, ...], labels: Sequence[str], label_sizes: Mapping[str, str]
) -> RunnerClass | None:
    # GitHub names its macOS larger runners itself, so those labels identify them:
    # https://docs.github.com/en/actions/reference/runners/larger-runners
    # A Linux or Windows larger runner gets "a workflow label that matches the runner name", a name
    # chosen when the runner is created:
    # https://docs.github.com/en/actions/how-tos/manage-runners/larger-runners/use-larger-runners
    # No documented job field tells it from a self-hosted runner, so only the user's label_sizes
    # setting places it. A setting that names no larger row places nothing.
    if (row := _first(larger, labels)) is not None:
        return row
    by_id = {row.id: row for row in larger}
    return next(
        (by_id[label_sizes[label]] for label in labels if label_sizes.get(label) in by_id), None
    )


def _rows(
    runners: tuple[RunnerClass, ...], provider: str, kind: Kind | None = None
) -> tuple[RunnerClass, ...]:
    return tuple(row for row in runners if row.provider == provider and row.kind == kind)


def _first(runners: tuple[RunnerClass, ...], labels: Sequence[str]) -> RunnerClass | None:
    return next(
        (
            row
            for row in runners
            if any(fnmatchcase(label, pattern) for label in labels for pattern in row.labels)
        ),
        None,
    )

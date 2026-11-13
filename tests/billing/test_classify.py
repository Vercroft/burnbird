from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from burnbird.billing.classify import UNKNOWN, Classification, classify
from burnbird.billing.providers import RunnerClass, load_table

# Invented jobs. Standard GitHub-hosted jobs carry the values the jobs API returns for them.
HOSTED = "GitHub Actions"
GROUP = "Default"


@dataclass(frozen=True, slots=True)
class Job:
    labels: tuple[str, ...]
    runner_group_name: str | None
    label_sizes: Mapping[str, str] = field(default_factory=dict)


def hosted(label: str) -> Job:
    return Job((label,), HOSTED)


def mapped(label: str, row_id: str) -> Job:
    return Job((label,), "build-runners", {label: row_id})


def other(*labels: str) -> Job:
    return Job(labels, GROUP)


FIXTURES: dict[tuple[str, str], Job] = {
    ("github", "actions_linux_slim"): hosted("ubuntu-slim"),
    ("github", "actions_linux"): hosted("ubuntu-latest"),
    ("github", "actions_linux_arm"): hosted("ubuntu-24.04-arm"),
    ("github", "actions_windows"): hosted("windows-2025-vs2026"),
    ("github", "actions_windows_arm"): hosted("windows-11-arm"),
    ("github", "actions_macos"): hosted("macos-15-intel"),
    ("github", "linux_2_core_advanced"): mapped("linux-2core-advanced", "linux_2_core_advanced"),
    ("github", "linux_4_core"): mapped("ubuntu-24.04-4core", "linux_4_core"),
    ("github", "linux_8_core"): mapped("ubuntu-24.04-8core", "linux_8_core"),
    ("github", "linux_16_core"): mapped("ubuntu-24.04-16core", "linux_16_core"),
    ("github", "linux_32_core"): mapped("ubuntu-24.04-32core", "linux_32_core"),
    ("github", "linux_64_core"): mapped("ubuntu-24.04-64core", "linux_64_core"),
    ("github", "linux_96_core"): mapped("ubuntu-24.04-96core", "linux_96_core"),
    ("github", "windows_4_core"): mapped("windows-2025-4core", "windows_4_core"),
    ("github", "windows_8_core"): mapped("windows-2025-8core", "windows_8_core"),
    ("github", "windows_16_core"): mapped("windows-2025-16core", "windows_16_core"),
    ("github", "windows_32_core"): mapped("windows-2025-32core", "windows_32_core"),
    ("github", "windows_64_core"): mapped("windows-2025-64core", "windows_64_core"),
    ("github", "windows_96_core"): mapped("windows-2025-96core", "windows_96_core"),
    ("github", "macos_l"): hosted("macos-26-large"),
    ("github", "linux_2_core_arm"): mapped("ubuntu-24.04-2core-arm", "linux_2_core_arm"),
    ("github", "linux_4_core_arm"): mapped("ubuntu-24.04-4core-arm", "linux_4_core_arm"),
    ("github", "linux_8_core_arm"): mapped("ubuntu-24.04-8core-arm", "linux_8_core_arm"),
    ("github", "linux_16_core_arm"): mapped("ubuntu-24.04-16core-arm", "linux_16_core_arm"),
    ("github", "linux_32_core_arm"): mapped("ubuntu-24.04-32core-arm", "linux_32_core_arm"),
    ("github", "linux_64_core_arm"): mapped("ubuntu-24.04-64core-arm", "linux_64_core_arm"),
    ("github", "windows_2_core_arm"): mapped("windows-11-2core-arm", "windows_2_core_arm"),
    ("github", "windows_4_core_arm"): mapped("windows-11-4core-arm", "windows_4_core_arm"),
    ("github", "windows_8_core_arm"): mapped("windows-11-8core-arm", "windows_8_core_arm"),
    ("github", "windows_16_core_arm"): mapped("windows-11-16core-arm", "windows_16_core_arm"),
    ("github", "windows_32_core_arm"): mapped("windows-11-32core-arm", "windows_32_core_arm"),
    ("github", "windows_64_core_arm"): mapped("windows-11-64core-arm", "windows_64_core_arm"),
    ("github", "macos_xl"): hosted("xcode-27-xlarge"),
    ("github", "linux_4_core_gpu"): mapped("gpu-linux-4core", "linux_4_core_gpu"),
    ("github", "windows_4_core_gpu"): mapped("gpu-windows-4core", "windows_4_core_gpu"),
    ("blacksmith", "linux-x64-2vcpu"): other("blacksmith-2vcpu-ubuntu-2404"),
    ("blacksmith", "linux-x64-4vcpu"): other("blacksmith-4vcpu-ubuntu-2604"),
    ("blacksmith", "linux-x64-8vcpu"): other("blacksmith-8vcpu-ubuntu-2204"),
    ("blacksmith", "linux-x64-16vcpu"): other("blacksmith-16vcpu-ubuntu-2404"),
    ("blacksmith", "linux-x64-32vcpu"): other("blacksmith-32vcpu-ubuntu-2404"),
    ("blacksmith", "linux-arm-2vcpu"): other("blacksmith-2vcpu-ubuntu-2404-arm"),
    ("blacksmith", "linux-arm-4vcpu"): other("blacksmith-4vcpu-ubuntu-2604-arm"),
    ("blacksmith", "linux-arm-8vcpu"): other("blacksmith-8vcpu-ubuntu-2204-arm"),
    ("blacksmith", "linux-arm-16vcpu"): other("blacksmith-16vcpu-ubuntu-2404-arm"),
    ("blacksmith", "linux-arm-32vcpu"): other("blacksmith-32vcpu-ubuntu-2404-arm"),
    ("blacksmith", "windows-x64-2vcpu"): other("blacksmith-2vcpu-windows-2025"),
    ("blacksmith", "windows-x64-4vcpu"): other("blacksmith-4vcpu-windows-2025"),
    ("blacksmith", "windows-x64-8vcpu"): other("blacksmith-8vcpu-windows-2025"),
    ("blacksmith", "windows-x64-16vcpu"): other("blacksmith-16vcpu-windows-2025"),
    ("blacksmith", "windows-x64-32vcpu"): other("blacksmith-32vcpu-windows-2025"),
    ("blacksmith", "macos-6vcpu"): other("blacksmith-6vcpu-macos-latest"),
    ("blacksmith", "macos-12vcpu"): other("blacksmith-12vcpu-macos-15"),
    ("self-hosted", "self-hosted"): other("self-hosted", "linux", "x64"),
}

ROWS = load_table().runners


def run(job: Job, private: bool = True) -> Classification:
    return classify(job.labels, job.runner_group_name, private, job.label_sizes)


def row(provider: str, row_id: str) -> RunnerClass:
    return next(r for r in ROWS if (r.provider, r.id) == (provider, row_id))


@pytest.mark.parametrize("runner", ROWS, ids=lambda r: f"{r.provider}/{r.id}")
def test_every_row_has_a_fixture_that_classifies_to_it(runner: RunnerClass) -> None:
    job = FIXTURES.get((runner.provider, runner.id))
    assert job is not None, "add a fixture job for this row"
    assert run(job) == Classification(
        runner.provider, runner, runner.usd_per_minute, runner.weight
    )


def test_every_fixture_names_a_row() -> None:
    assert set(FIXTURES) == {(r.provider, r.id) for r in ROWS}


@pytest.mark.parametrize("labels", [(), ("my-own-runner",), ("Self-Hosted",)])
def test_unknown_labels_are_returned_as_unknown(labels: tuple[str, ...]) -> None:
    assert run(Job(labels, GROUP)) == Classification(UNKNOWN, None, None, None)


def test_a_standard_label_outside_the_hosted_group_is_unknown() -> None:
    assert run(other("ubuntu-latest")).provider == UNKNOWN


def test_a_standard_runner_is_free_on_a_public_repository_only() -> None:
    linux = row("github", "actions_linux")
    job = hosted("ubuntu-latest")
    assert run(job, private=True) == Classification("github", linux, Decimal("0.006"), None)
    assert run(job, private=False) == Classification("github", linux, Decimal(0), None)


def test_a_mapped_larger_runner_is_charged_on_a_public_repository() -> None:
    job = mapped("ubuntu-24.04-16core", "linux_16_core")
    assert run(job, private=False) == Classification(
        "github", row("github", "linux_16_core"), Decimal("0.042"), None
    )


def test_an_unmapped_larger_runner_is_unknown() -> None:
    assert run(other("ubuntu-24.04-16core")).provider == UNKNOWN


def test_a_macos_larger_label_needs_no_mapping() -> None:
    assert run(other("macos-15-xlarge"), private=False) == Classification(
        "github", row("github", "macos_xl"), Decimal("0.102"), None
    )


@pytest.mark.parametrize("size", ["actions_linux", "linux-x64-2vcpu", "no-such-size"])
def test_a_mapping_that_names_no_larger_row_gives_unknown(size: str) -> None:
    job = Job(("team-runner",), GROUP, {"team-runner": size})
    assert run(job).provider == UNKNOWN


@pytest.mark.parametrize(
    ("labels", "provider"),
    [
        (("self-hosted", "blacksmith-2vcpu-ubuntu-2404"), "blacksmith"),
        (("self-hosted", "ubuntu-latest"), "self-hosted"),
    ],
)
def test_the_first_matching_step_wins(labels: tuple[str, ...], provider: str) -> None:
    assert run(Job(labels, HOSTED)).provider == provider

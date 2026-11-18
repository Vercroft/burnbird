"""`Gate summary` is the one CI check the ruleset requires, so it must judge every other CI job.

Each job's own check name can then change without a ruleset change. Three ways the gate could
stop meaning anything each have a test: a job left out of its `needs` runs with nothing requiring
it to pass, a verdict that accepts a failure lets every job fail behind a green check, and an `if:`
on another job could skip that job on pull requests, which the gate counts as passed.

There is no YAML library here, so ci.yml is read as text, as tests/test_images.py reads the
workflows.
"""

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
GATE = "gate-summary"
# The only job with a condition: the gate, always.
CONDITIONS = {GATE: "always()"}

JOB = re.compile(r"^  ([a-z][a-z0-9_-]*):\n", re.MULTILINE)
VERDICT = re.compile(r"<<'PY'\n(.*?)\n *PY\n", re.DOTALL)


def _jobs() -> dict[str, str]:
    """Each job's id and the text of its block."""
    parts = JOB.split(CI.read_text().split("\njobs:\n", 1)[1])
    jobs = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
    assert len(jobs) > 1, f"{CI} read as {len(jobs)} job(s); its shape changed"
    return jobs


def _field(block: str, key: str) -> str | None:
    """A job-level key's value, or None."""
    match = re.search(rf"^    {key}: (.+)$", block, re.MULTILINE)
    return match[1] if match else None


def _needs() -> list[str]:
    needs = _field(_jobs()[GATE], "needs")
    assert needs is not None
    assert needs.startswith("[")
    assert needs.endswith("]"), "write the gate's needs as one [a, b] list"
    return [job.strip() for job in needs[1:-1].split(",")]


def _verdict(results: dict[str, str]) -> int:
    """Run the gate's own step, as CI does, on the needs results given."""
    match = VERDICT.search(_jobs()[GATE])
    assert match is not None, "the gate's verdict is no longer a python3 <<'PY' step"
    needs = {job: {"result": result, "outputs": {}} for job, result in results.items()}
    env = {**os.environ, "NEEDS_JSON": json.dumps(needs)}
    return subprocess.run(  # noqa: S603  # runs the gate's own step with this Python, no shell
        [sys.executable, "-I", "-c", textwrap.dedent(match[1])],
        env=env,
        timeout=30,
        check=False,
        capture_output=True,
    ).returncode


def test_the_gate_is_named_for_the_ruleset_and_always_runs() -> None:
    gate = _jobs().get(GATE)
    assert gate is not None, f"ci.yml has no `{GATE}` job; the ruleset requires its check"
    assert _field(gate, "name") == "Gate summary", "the ruleset requires the check by this name"
    # Without always(), one failed job skips the gate, and a skipped required check is not red.
    assert _field(gate, "if") == "always()"


def test_the_gate_needs_every_other_job() -> None:
    missing = sorted(set(_jobs()) - {GATE} - set(_needs()))
    assert not missing, f"`{GATE}` does not need {missing}, so nothing requires them to pass"


def test_only_the_gate_has_a_condition() -> None:
    conditions = {job: _field(block, "if") for job, block in _jobs().items()}
    assert {job: c for job, c in conditions.items() if c is not None} == CONDITIONS


@pytest.mark.parametrize("bad", ["failure", "cancelled"])
def test_the_gate_fails_when_any_job_did_not_pass(bad: str) -> None:
    results = dict.fromkeys(_needs(), "success")
    results["python"] = bad
    assert _verdict(results) != 0


def test_the_gate_fails_when_it_judges_no_job() -> None:
    assert _verdict({}) != 0


def test_the_gate_passes_when_every_job_passed_or_was_skipped() -> None:
    results = dict.fromkeys(_needs(), "success")
    results["repository-checks"] = "skipped"
    assert _verdict(results) == 0

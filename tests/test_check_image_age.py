"""Tests for check-image-age.py. No network: registry answers are fakes.

The script is found next to this file (`tools/`) or in `../scripts/` (vendored).
`CHECK_IMAGE_AGE_SCRIPT` points at another copy; the mutation proof uses it.
"""

import importlib.util
import json
import os
import shutil
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

NAME = "check-image-age.py"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _script_path() -> Path:
    override = os.environ.get("CHECK_IMAGE_AGE_SCRIPT")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    for candidate in (here / NAME, here.parent / "scripts" / NAME):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(NAME)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_image_age", _script_path())
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cia() -> ModuleType:
    return _load()


def _config(created: str) -> dict[str, object]:
    return {"created": created, "architecture": "amd64", "os": "linux"}


def _raw(*created: str) -> str:
    """What `imagetools inspect --format '{{json .Image}}'` prints for an index."""
    return json.dumps({f"linux/p{i}": _config(c) for i, c in enumerate(created)})


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


# ---------- finding references ----------


def test_dockerfile_from_lines(cia: ModuleType) -> None:
    lines = [
        "# syntax=docker/dockerfile:1",
        f"FROM --platform=$BUILDPLATFORM node:24-alpine@{DIGEST_A} AS build",
        "FROM build AS test",
        "FROM scratch",
        "FROM ${BASE}",
        "FROM python:3.14-slim",
    ]
    found = cia.refs_in_file("Dockerfile", lines)
    assert found == {f"node:24-alpine@{DIGEST_A}", "python:3.14-slim"}


def test_from_is_read_only_in_dockerfiles(cia: ModuleType) -> None:
    assert cia.refs_in_file("report.sql", ["FROM users"]) == set()
    assert cia.refs_in_file("Dockerfile.theme", ["FROM debian:13"]) == {"debian:13"}
    assert cia.refs_in_file("app.Dockerfile", ["FROM debian:13"]) == {"debian:13"}
    assert cia.refs_in_file("Containerfile", ["FROM debian:13"]) == {"debian:13"}


def test_copy_from_image_but_not_stage(cia: ModuleType) -> None:
    lines = [
        "FROM debian:13 AS builder",
        "COPY --from=builder /a /b",
        "COPY --from=nginx:1.29 /c /d",
    ]
    assert cia.refs_in_file("Dockerfile", lines) == {"debian:13", "nginx:1.29"}


def test_digest_pins_are_found_in_any_file(cia: ModuleType) -> None:
    compose = [f'    image: "mcr.microsoft.com/playwright:v1.65.0-noble@{DIGEST_A}"  # tag']
    jsonl = [f'{{"base": "ghcr.io/astral-sh/uv@{DIGEST_B}", "port": 5000}}']
    assert cia.refs_in_file("compose.yaml", compose) == {
        f"mcr.microsoft.com/playwright:v1.65.0-noble@{DIGEST_A}"
    }
    assert cia.refs_in_file("images.json", jsonl) == {f"ghcr.io/astral-sh/uv@{DIGEST_B}"}


def test_registry_with_port_and_other_hashes(cia: ModuleType) -> None:
    line = [
        f"registry.local:5000/team/app:1@{DIGEST_A}",
        "integrity sha512-abc",
        "pkg@sha256:short",
    ]
    assert cia.refs_in_file("notes.txt", line) == {f"registry.local:5000/team/app:1@{DIGEST_A}"}


# ---------- reading the age ----------


def test_newest_platform_counts(cia: ModuleType) -> None:
    raw = _raw("2026-11-01T00:00:00Z", "2026-11-05T19:18:22.22515658Z", "2026-10-01T00:00:00Z")
    assert cia.newest_created(raw) == datetime(2026, 11, 5, 19, 18, 22, 225156, tzinfo=UTC)


def test_single_platform_shape(cia: ModuleType) -> None:
    raw = json.dumps(_config("2026-11-03T02:22:26.516762893Z"))
    assert cia.newest_created(raw) == datetime(2026, 11, 3, 2, 22, 26, 516762, tzinfo=UTC)


def test_placeholder_and_missing_dates_are_ignored(cia: ModuleType) -> None:
    raw = json.dumps(
        {
            "a": _config("1970-01-01T00:00:00Z"),
            "b": {"architecture": "arm64"},
            "c": _config("junk"),
        }
    )
    assert cia.newest_created(raw) is None
    mixed = json.dumps(
        {"a": _config("1970-01-01T00:00:00Z"), "b": _config("2026-10-01T00:00:00Z")}
    )
    assert cia.newest_created(mixed) == datetime(2026, 10, 1, tzinfo=UTC)


def test_unreadable_output_is_unknown(cia: ModuleType) -> None:
    assert cia.newest_created("not json") is None
    assert cia.newest_created("[]") is None


# ---------- judging ----------


class FakeRegistry:
    """A Registry that answers from a dict and remembers what it was asked."""

    def __init__(self, cia: ModuleType, answers: dict[str, str]) -> None:
        self.cia, self.answers = cia, answers
        self.calls: list[str] = []

    def inspect(self, ref: str) -> str:
        self.calls.append(ref)
        if ref not in self.answers:
            raise self.cia.InspectError("registry said no")
        return self.answers[ref]


def _check(
    cia: ModuleType,
    refs: list[str],
    answers: dict[str, str],
    allows: list[object] | None = None,
) -> tuple[dict[str, object], list[str]]:
    registry = FakeRegistry(cia, answers)
    checker = cia.ImageChecker(
        registry, cia.AllowList(tuple(allows or ())), NOW, timedelta(days=7)
    )
    return {f.ref: f.status for f in checker.check(refs)}, registry.calls


def test_age_boundary(cia: ModuleType) -> None:
    refs = ["old", "edge", "young"]
    answers = {"old": _raw(_ago(8)), "edge": _raw(_ago(7)), "young": _raw(_ago(6.99))}
    by_ref = _check(cia, refs, answers)[0]
    assert by_ref == {
        "old": cia.Status.OK,
        "edge": cia.Status.OK,
        "young": cia.Status.YOUNG,
    }


def test_one_young_platform_makes_the_image_young(cia: ModuleType) -> None:
    answers = {"x": _raw(_ago(30), _ago(1))}
    assert _check(cia, ["x"], answers)[0] == {"x": cia.Status.YOUNG}


def test_unknown_age_and_registry_errors_fail(cia: ModuleType) -> None:
    answers = {"epoch": _raw("1970-01-01T00:00:00Z")}
    by_ref = _check(cia, ["epoch", "gone"], answers)[0]
    assert by_ref == {"epoch": cia.Status.UNKNOWN, "gone": cia.Status.UNKNOWN}


def test_each_reference_is_inspected_once(cia: ModuleType) -> None:
    _, calls = _check(cia, ["x", "x", "y"], {"x": _raw(_ago(9)), "y": _raw(_ago(9))})
    assert sorted(calls) == ["x", "y"]


# ---------- exceptions ----------


def _allow(cia: ModuleType, image: str, until: date) -> object:
    return cia.Allow(image=image, reason="CVE-2026-0001 (CISA KEV)", until=until)


def test_allow_lets_a_young_image_through_until_its_date(cia: ModuleType) -> None:
    answers = {"x": _raw(_ago(1))}
    soon = NOW.date() + timedelta(days=5)
    assert _check(cia, ["x"], answers, [_allow(cia, "x", soon)])[0] == {"x": cia.Status.ALLOWED}
    gone = NOW.date() - timedelta(days=1)
    assert _check(cia, ["x"], answers, [_allow(cia, "x", gone)])[0] == {"x": cia.Status.YOUNG}


def test_allow_matches_the_digest_not_the_tag(cia: ModuleType) -> None:
    soon = NOW.date() + timedelta(days=5)
    allows = [_allow(cia, f"node@{DIGEST_A}", soon)]
    ref_same = f"node:24@{DIGEST_A}"
    ref_other = f"node:24@{DIGEST_B}"
    answers = {ref_same: _raw(_ago(1)), ref_other: _raw(_ago(1))}
    by_ref = _check(cia, [ref_same, ref_other], answers, allows)[0]
    assert by_ref == {ref_same: cia.Status.ALLOWED, ref_other: cia.Status.YOUNG}


def test_unused_exceptions_are_reported(cia: ModuleType) -> None:
    soon = NOW.date() + timedelta(days=5)
    used, spare = _allow(cia, f"node@{DIGEST_A}", soon), _allow(cia, f"node@{DIGEST_B}", soon)
    allows = cia.AllowList((used, spare))
    assert allows.unused([f"node:24@{DIGEST_A}"]) == [spare]
    assert allows.unused([]) == [used, spare]


def _write_allow(path: Path, image: str, reason: str, until: str) -> Path:
    path.write_text(f'[[allow]]\nimage = "{image}"\nreason = "{reason}"\nuntil = {until}\n')
    return path


def test_exceptions_file_is_validated(cia: ModuleType, tmp_path: Path) -> None:
    today = NOW.date()
    ok = _write_allow(
        tmp_path / "a.toml", "x", "CVE-2026-0001 KEV", str(today + timedelta(days=30))
    )
    assert [a.image for a in cia.AllowList.load(ok, today).entries] == ["x"]
    assert cia.AllowList.load(tmp_path / "missing.toml", today).entries == ()
    far = _write_allow(tmp_path / "b.toml", "x", "CVE-2026-0001", str(today + timedelta(days=31)))
    with pytest.raises(cia.CheckError, match="30 days"):
        cia.AllowList.load(far, today)
    why = _write_allow(tmp_path / "c.toml", "x", "because", str(today + timedelta(days=3)))
    with pytest.raises(cia.CheckError, match="CVE"):
        cia.AllowList.load(why, today)
    url = _write_allow(tmp_path / "d.toml", "x", "https://vendor.example/advisory", str(today))
    assert len(cia.AllowList.load(url, today).entries) == 1
    (tmp_path / "e.toml").write_text('[[allow]]\nimage = "x"\n')
    with pytest.raises(cia.CheckError):
        cia.AllowList.load(tmp_path / "e.toml", today)


# ---------- the command, on a throwaway git repository ----------

FAKE_DOCKER = """#!/bin/sh
# usage: docker buildx imagetools inspect REF --format '{{json .Image}}'
ref="$4"
file="$FAKE_ANSWERS/$(printf '%s' "$ref" | tr '/:@' '___')"
if [ -f "$file" ]; then cat "$file"; else echo "not found" >&2; exit 1; fi
"""


class Repo:
    """A throwaway repository under tmp_path, isolated from every git config of the machine."""

    def __init__(self, cia: ModuleType, root: Path, answers: Path) -> None:
        self.cia = cia
        self.root = root
        self.answers = answers
        self.git("init", "-q", "-b", "main")
        top = self.git("rev-parse", "--show-toplevel").strip()
        # The guard: never act on any repository but the throwaway one.
        assert Path(top).resolve() == root.resolve()

    def git(self, *args: str) -> str:
        settings = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
        identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
        done = self.cia.Git(self.root).run(*settings, *identity, *args)
        assert done.returncode == 0, done.stderr
        return str(done.stdout)

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def answer(self, ref: str, raw: str) -> None:
        (self.answers / ref.replace("/", "_").replace(":", "_").replace("@", "_")).write_text(raw)


@pytest.fixture
def repo(cia: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "answers").mkdir()
    (tmp_path / "work").mkdir()
    git_dir = Path(shutil.which("git") or "/usr/bin/git").parent
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{git_dir}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("FAKE_ANSWERS", str(tmp_path / "answers"))
    return Repo(cia, tmp_path / "work", tmp_path / "answers")


def _run(cia: ModuleType, repo: Repo, *extra: str) -> int:
    code: int = cia.main(["--root", str(repo.root), "--base", "main", "--head", "feature", *extra])
    return code


def _base_and_feature(repo: Repo, old_ref: str, new_ref: str) -> None:
    # An unchanged reference that nobody can answer for: it must never be asked.
    repo.write("compose.yaml", f"image: ghcr.io/example/unchanged@sha256:{'c' * 64}\n")
    repo.write("Dockerfile", f"FROM {old_ref}\n")
    repo.commit("base")
    repo.git("switch", "-q", "-c", "feature")
    repo.write("Dockerfile", f"FROM {new_ref}\n")
    repo.commit("bump")


def test_command_fails_on_an_added_young_image(
    cia: ModuleType, repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    old, new = f"node:24@{DIGEST_A}", f"node:24@{DIGEST_B}"
    _base_and_feature(repo, old, new)
    repo.answer(new, _raw(_ago_now(1)))
    assert _run(cia, repo) == 1
    assert new in capsys.readouterr().out


def test_command_passes_an_old_image_and_ignores_unchanged_ones(
    cia: ModuleType, repo: Repo
) -> None:
    old, new = f"node:24@{DIGEST_A}", f"node:24@{DIGEST_B}"
    _base_and_feature(repo, old, new)
    repo.answer(new, _raw(_ago_now(30)))
    assert _run(cia, repo) == 0


def test_command_uses_the_exceptions_file_of_the_head(cia: ModuleType, repo: Repo) -> None:
    old, new = f"node:24@{DIGEST_A}", f"node:24@{DIGEST_B}"
    _base_and_feature(repo, old, new)
    repo.answer(new, _raw(_ago_now(1)))
    (repo.root / ".github").mkdir()
    until = date.today() + timedelta(days=10)
    _write_allow(repo.root / ".github" / "dependency-age-exceptions.toml", f"node@{DIGEST_B}",
                 "CVE-2026-0001 (CISA KEV)", str(until))  # fmt: skip
    repo.commit("exception")
    assert _run(cia, repo) == 0


def test_command_reports_a_bad_setup_as_status_2(cia: ModuleType, repo: Repo) -> None:
    repo.write("Dockerfile", "FROM debian:13\n")
    repo.commit("base")
    assert _run(cia, repo) == 2  # no branch called feature


def test_command_fails_closed_when_the_registry_cannot_be_asked(
    cia: ModuleType, repo: Repo
) -> None:
    _base_and_feature(repo, f"node:24@{DIGEST_A}", f"node:24@{DIGEST_B}")
    assert _run(cia, repo) == 1  # the fake docker has no answer


def _ago_now(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")

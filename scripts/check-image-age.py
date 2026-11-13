"""Fail when a pull request adds a container image younger than the minimum age.

Reads every container image reference the head commit has and the merge base does not: a reference
pinned by digest (`name@sha256:...`) in any text file, and the images a Dockerfile names in `FROM`
or `COPY --from`. For each one it asks the registry for the image's own build time
(`docker buildx imagetools inspect REF --format '{{json .Image}}'`, the `created` field of every
platform; the newest counts). An image younger than 7 days, with no usable date (missing, or the
1970 placeholder of a reproducible build) or that the registry cannot answer for, fails.

An emergency exception is an entry in `.github/dependency-age-exceptions.toml` (see `load_allows`).

Exit status: 0 every added image is old enough or allowed, 1 at least one is too young or unknown,
2 the command, git or the exceptions file is wrong.

Standard library only, so a repository vendors this file byte for byte (standard section 7B.13).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Final, Protocol, Self

MIN_AGE_DAYS: Final = 7
MAX_ALLOW_DAYS: Final = 30
DEFAULT_ALLOWS: Final = ".github/dependency-age-exceptions.toml"
PLACEHOLDER_BEFORE: Final = datetime(1990, 1, 1, tzinfo=UTC)  # reproducible builds report 1970
COMMAND_TIMEOUT_SECONDS: Final = 60
WORKERS: Final = 8

# git grep -E picks the lines; the Python patterns below read them.
CANDIDATE_LINES: Final = r"@sha256:[0-9a-f]{64}|^[[:space:]]*FROM[[:space:]]|--from="
DIGEST_REF: Final = re.compile(
    r"(?<![\w./:@-])((?:[A-Za-z0-9.-]+:[0-9]+/)?[A-Za-z0-9][A-Za-z0-9._/-]*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?@sha256:[0-9a-f]{64})"
)
FROM_LINE: Final = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
COPY_FROM: Final = re.compile(r"--from=([^\s\"']+)")
IMAGE_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
ADVISORY: Final = re.compile(r"CVE-\d{4}-\d{4,}|GHSA-[0-9a-z-]{14}|https?://\S+")


class CheckError(Exception):
    """The command line, git or the exceptions file is wrong (exit status 2)."""


class InspectError(Exception):
    """The registry could not be asked about one image."""


class Status(Enum):
    """The outcome for one image."""

    OK = auto()
    ALLOWED = auto()
    YOUNG = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class Finding:
    """The outcome for one image reference, with the words to print."""

    ref: str
    status: Status
    detail: str

    @property
    def failed(self) -> bool:
        """True when the image is too young or its age is unknown."""
        return self.status in {Status.YOUNG, Status.UNKNOWN}

    def render(self) -> str:
        """Return the line to print."""
        return f"{'FAIL' if self.failed else 'ok  '} {self.ref}: {self.detail}"


def is_dockerfile(path: str) -> bool:
    """Tell whether a path names a Dockerfile (`Dockerfile*`, `*.Dockerfile`, `Containerfile*`)."""
    name = Path(path).name.lower()
    return name.startswith(("dockerfile", "containerfile")) or name.endswith(".dockerfile")


def digest_of(ref: str) -> str | None:
    """Return the `sha256:...` part of a reference, or None for a tag-only reference."""
    return ref.split("@", 1)[1] if "@" in ref else None


def refs_in_file(path: str, lines: Iterable[str]) -> set[str]:
    """Return the image references of one file, given its lines in order."""
    found: set[str] = set()
    stages: set[str] = set()
    dockerfile = is_dockerfile(path)
    for line in lines:
        found.update(DIGEST_REF.findall(line))
        if not dockerfile:
            continue
        match = FROM_LINE.match(line)
        if match:
            image, stage = match.group(1), match.group(2)
            if (
                image.lower() not in stages
                and image.lower() != "scratch"
                and IMAGE_NAME.match(image)
            ):
                found.add(image)
            if stage:
                stages.add(stage.lower())
        for image in COPY_FROM.findall(line):
            known = image.lower() in stages
            if not known and IMAGE_NAME.match(image) and re.search(r"[:/@]", image):
                found.add(image)
    return found


def newest_created(raw: str) -> datetime | None:
    """Read the newest usable build time from `imagetools inspect` output, or None."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    configs: list[object]
    if isinstance(data, dict) and ("created" in data or "architecture" in data):
        configs = [data]  # one platform
    elif isinstance(data, dict):
        configs = list(data.values())  # an index: one entry per platform
    else:
        return None
    times: list[datetime] = []
    for config in configs:
        created = config.get("created") if isinstance(config, dict) else None
        if not isinstance(created, str):
            continue
        try:
            moment = datetime.fromisoformat(created)
        except ValueError:
            continue
        if moment.tzinfo is not None and moment >= PLACEHOLDER_BEFORE:
            times.append(moment)
    return max(times) if times else None


@dataclass(frozen=True, slots=True)
class Allow:
    """One emergency exception: an image let through while it is too young, until a date."""

    image: str
    reason: str
    until: date

    def covers(self, ref: str) -> bool:
        """Tell whether this exception is for the reference (the same text, or the same digest)."""
        mine, theirs = digest_of(self.image), digest_of(ref)
        return self.image == ref or (mine is not None and mine == theirs)


@dataclass(frozen=True, slots=True)
class AllowList:
    """The exceptions of a repository, read from its exceptions file."""

    entries: tuple[Allow, ...] = ()

    @classmethod
    def load(cls, path: Path, today: date) -> Self:
        """Read the exceptions file. Each `[[allow]]` has `image`, `reason` and `until`.

        `until` is at most 30 days ahead and `reason` names a CVE, a GHSA id or an advisory URL.
        A missing file means no exceptions.
        """
        if not path.is_file():
            return cls()
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise CheckError(f"{path}: {error}") from error
        return cls(tuple(cls._entry(path, raw, today) for raw in data.get("allow", [])))

    @staticmethod
    def _entry(path: Path, raw: dict[str, object], today: date) -> Allow:
        image, reason, until = raw.get("image"), raw.get("reason"), raw.get("until")
        if not (isinstance(image, str) and isinstance(reason, str) and isinstance(until, date)):
            raise CheckError(f"{path}: each [[allow]] needs image, reason and until (a date)")
        if not ADVISORY.search(reason):
            raise CheckError(
                f"{path}: the reason for {image} must name a CVE, GHSA id or advisory URL"
            )
        if until > today + timedelta(days=MAX_ALLOW_DAYS):
            raise CheckError(f"{path}: {image} is allowed for more than {MAX_ALLOW_DAYS} days")
        return Allow(image, reason, until)

    def match(self, ref: str, today: date) -> Allow | None:
        """Return the exception that lets this image through today, if there is one."""
        return next((a for a in self.entries if a.until >= today and a.covers(ref)), None)

    def unused(self, refs: Iterable[str]) -> list[Allow]:
        """Return the exceptions that cover none of the references."""
        refs = list(refs)
        return [a for a in self.entries if not any(a.covers(ref) for ref in refs)]


class Registry(Protocol):
    """Anything that can say what the registry knows about an image."""

    def inspect(self, ref: str) -> str:
        """Return the `{{json .Image}}` text of the image, or raise InspectError."""
        ...


class Program:
    """An executable found on PATH, run in one directory without a shell."""

    def __init__(self, name: str, cwd: Path) -> None:
        """Find the program on PATH; fail with CheckError if it is not there."""
        found = shutil.which(name)
        if found is None:
            raise CheckError(f"{name} was not found on PATH")
        self._path, self._cwd = found, cwd

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the program with the arguments; fail with CheckError if it cannot start or hangs."""
        try:
            return subprocess.run(  # noqa: S603  # path from shutil.which, no shell, fixed arguments
                [self._path, *args],
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CheckError(f"{self._path}: {error}") from error


class DockerRegistry:
    """Asks the registry through `docker buildx imagetools inspect` (a Registry)."""

    def __init__(self, cwd: Path) -> None:
        """Find docker on PATH."""
        self._docker = Program("docker", cwd)

    def inspect(self, ref: str) -> str:
        """Return the image's configs as JSON text."""
        try:
            done = self._docker.run(
                "buildx", "imagetools", "inspect", ref, "--format", "{{json .Image}}"
            )
        except CheckError as error:
            raise InspectError(str(error)) from error
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines()
            raise InspectError(lines[-1] if lines else "failed")
        return done.stdout


class Git:
    """One repository: finds the pull request's base and reads image references from commits."""

    def __init__(self, root: Path) -> None:
        """Find git on PATH and work in the repository at root."""
        self._git = Program("git", root)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run git in the repository."""
        return self._git.run(*args)

    def commit(self, rev: str) -> str:
        """Return the commit id of a revision."""
        done = self.run("rev-parse", "--verify", f"{rev}^{{commit}}")
        if done.returncode != 0:
            raise CheckError(f"{rev} is not a commit")
        return done.stdout.strip()

    def merge_base(self, base: str, head: str) -> str:
        """Return the commit where head left base (the base side of a pull request)."""
        done = self.run("merge-base", base, head)
        if done.returncode != 0:
            raise CheckError(f"{base} and {head} share no history (fetch the full history)")
        return done.stdout.strip()

    def image_refs(self, commit: str) -> set[str]:
        """Return every image reference in the tree of a commit (one `git grep` for the tree)."""
        done = self.run("grep", "-I", "-z", "-n", "-E", CANDIDATE_LINES, commit, "--")
        if done.returncode == 1:
            return set()
        if done.returncode != 0:
            raise CheckError(f"git grep failed: {done.stderr.strip()}")
        per_file: dict[str, list[str]] = {}
        for record in done.stdout.split("\n"):
            where, _, rest = record.partition("\0")
            _, _, line = rest.partition("\0")
            if where:
                per_file.setdefault(where.removeprefix(f"{commit}:"), []).append(line)
        found: set[str] = set()
        for path, lines in per_file.items():
            found |= refs_in_file(path, lines)
        return found


class ImageChecker:
    """Decides, for each image, whether it is old enough, using a registry and the exceptions."""

    def __init__(
        self, registry: Registry, allows: AllowList, now: datetime, min_age: timedelta
    ) -> None:
        """Use this registry and these exceptions to judge images as of `now`."""
        self._registry, self._allows, self._now, self._min_age = registry, allows, now, min_age

    def judge(self, ref: str) -> Finding:
        """Ask the registry about one image and decide."""
        try:
            created = newest_created(self._registry.inspect(ref))
        except InspectError as error:
            finding = Finding(ref, Status.UNKNOWN, f"the registry did not answer: {error}")
        else:
            finding = self._by_age(ref, created)
        allow = self._allows.match(ref, self._now.date())
        if finding.failed and allow is not None:
            detail = f"{finding.detail}; allowed until {allow.until}: {allow.reason}"
            return Finding(ref, Status.ALLOWED, detail)
        return finding

    def _by_age(self, ref: str, created: datetime | None) -> Finding:
        if created is None:
            return Finding(
                ref, Status.UNKNOWN, "no usable build date (missing or a 1970 placeholder)"
            )
        age = self._now - created
        detail = f"built {created:%Y-%m-%dT%H:%M:%SZ}, {age / timedelta(days=1):.1f} days old"
        return Finding(ref, Status.OK if age >= self._min_age else Status.YOUNG, detail)

    def check(self, refs: Iterable[str]) -> list[Finding]:
        """Judge each distinct reference once, in parallel, in sorted order."""
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(self.judge, sorted(set(refs))))


def run(args: argparse.Namespace) -> int:
    """Run the check for parsed arguments and return the exit status."""
    root: Path = args.root.resolve()
    now = datetime.now(UTC)
    allows = AllowList.load(root / args.allows, now.date())
    git = Git(root)
    head = git.commit(args.head)
    base = git.merge_base(git.commit(args.base), head)
    added = git.image_refs(head) - git.image_refs(base)
    if not added:
        say("no container image added")
        return 0
    checker = ImageChecker(DockerRegistry(root), allows, now, timedelta(days=args.min_age_days))
    findings = checker.check(added)
    for finding in findings:
        say(finding.render())
    for allow in allows.unused(added):
        say(f"note: the exception for {allow.image} matches no added image; remove it")
    failed = sum(finding.failed for finding in findings)
    say(f"{len(findings)} checked, {failed} failed (minimum age {args.min_age_days} days)")
    return 1 if failed else 0


def say(text: str) -> None:
    """Write one line to standard output."""
    sys.stdout.write(f"{text}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the check."""
    parser = argparse.ArgumentParser(
        description="Fail when a pull request adds an image younger than 7 days."
    )
    parser.add_argument(
        "--base", required=True, help="the target branch or commit of the pull request"
    )
    parser.add_argument("--head", default="HEAD", help="the pull request's head (default HEAD)")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="the repository (default: here)"
    )
    parser.add_argument(
        "--allows", default=DEFAULT_ALLOWS, help="the exceptions file, relative to the root"
    )
    parser.add_argument("--min-age-days", type=int, default=MIN_AGE_DAYS)
    try:
        return run(parser.parse_args(argv))
    except CheckError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())

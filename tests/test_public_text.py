"""No tracked file holds an email or IP address that is not on the allowlist below.

The repository is public, so a person's address or a machine's IP must never land in it. Every
tracked file is read (files that are not UTF-8 text are skipped); every email address, and every
IPv4 and IPv6 address that Python's ``ipaddress`` accepts, must be allowed. A failure names the
file, the line and the kind, never the value. Values this file allows are built from parts, so it
does not hold them as literals itself.
"""

import ipaddress
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

import pytest

ROOT: Final = Path(__file__).resolve().parent.parent

_EMAIL: Final = re.compile(
    r"[A-Za-z0-9._%+\-\[\]]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_IPV4: Final = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# A run of hex digits, colons and dots with at least two colons; ``ipaddress`` decides the rest,
# so a time such as 11:59:59 is not an address.
_IPV6: Final = re.compile(r"(?<![\w:.])[0-9A-Fa-f.]*:[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*(?![\w:])")

# The project's contact point, and the reserved domain every test address uses (RFC 6761).
ALLOWED_ADDRESSES: Final = frozenset({"hello" + "@" + "burnbird.racingcrow.com"})
ALLOWED_DOMAIN: Final = "example.invalid"
# GitHub's own bot identities, which the commit policy must name, in that file only.
ALLOWED_IN_FILE: Final = {
    ".github/scripts/commit_policy.py": frozenset(
        {
            "49699333+dependabot[bot]" + "@" + "users.noreply.github.com",
            "noreply" + "@" + "github.com",
        }
    ),
}
# Loopback, and the documentation ranges of RFC 5737 and RFC 3849.
ALLOWED_NETWORKS: Final = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.1/32",
        "::1/128",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "2001:db8::/32",
    )
)


@dataclass(frozen=True, slots=True)
class Hit:
    """A value that is not allowed: where it is and what kind it is, never the value."""

    path: str
    line: int
    kind: str

    @override
    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} not on the allowlist"


def tracked_files(root: Path) -> list[str]:
    """The repository's tracked files, read with git isolated from the machine's own settings."""
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }

    program = shutil.which("git")
    assert program is not None, "git is not on PATH"

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603  # path from shutil.which, no shell, fixed arguments
            [program, "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
            timeout=30,
        ).stdout

    # The guard: read no repository but this one.
    assert Path(git("rev-parse", "--show-toplevel").strip()).resolve() == root.resolve()
    return [name for name in git("ls-files", "-z").split("\0") if name]


def scan(root: Path, files: Iterable[str]) -> list[Hit]:
    """Every value in these files under ``root`` that is not allowed."""
    hits: list[Hit] = []
    for name in files:
        try:
            text = (root / name).read_text(encoding="utf-8")
        except UnicodeDecodeError, IsADirectoryError, FileNotFoundError:
            continue
        hits.extend(scan_text(name, text))
    return hits


def scan_text(path: str, text: str) -> Iterator[Hit]:
    """Every value in one file's text that is not allowed."""
    for number, line in enumerate(text.splitlines(), start=1):
        for address in _EMAIL.findall(line):
            if not _address_allowed(path, address):
                yield Hit(path, number, "email address")
        for kind, pattern in (("IPv4 address", _IPV4), ("IPv6 address", _IPV6)):
            for candidate in pattern.findall(line):
                if not _ip_allowed(candidate):
                    yield Hit(path, number, kind)


def _address_allowed(path: str, address: str) -> bool:
    domain = address.rpartition("@")[2].lower()
    return (
        address in ALLOWED_ADDRESSES
        or domain == ALLOWED_DOMAIN
        or domain.endswith("." + ALLOWED_DOMAIN)
        or address in ALLOWED_IN_FILE.get(path, frozenset())
    )


def _ip_allowed(candidate: str) -> bool:
    # An IPv6 form needs two groups: "::" and "::2" are Python slices far more often.
    if ":" in candidate and len([group for group in candidate.split(":") if group]) < 2:
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        # Not an address: a time, a version or a regular expression.
        return True
    return any(address in network for network in ALLOWED_NETWORKS)


def test_no_tracked_file_holds_an_address_that_is_not_allowed() -> None:
    hits = scan(ROOT, tracked_files(ROOT))
    assert not hits, "\n".join(map(str, hits))


# ---------- the scan itself finds what it must ----------


def _planted_address(domain: str) -> str:
    return "someone" + "@" + domain


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"write to {_planted_address('example.com')} today", "email address"),
        (f"<{_planted_address('mail.example.org')}>", "email address"),
        ("server " + "10.0.0." + "12" + " answered", "IPv4 address"),
        ("http://" + "172.16.4." + "9" + ":8080/", "IPv4 address"),
        ("listen " + "fd00::" + "12", "IPv6 address"),
        ("http://[" + "fe80:" + ":1:" + "2]:443/", "IPv6 address"),
    ],
)
def test_a_planted_value_is_found_by_file_line_and_kind_never_by_value(
    text: str, kind: str
) -> None:
    [hit] = scan_text("docs/planted.md", "first line\n" + text + "\n")
    assert (hit.path, hit.line, hit.kind) == ("docs/planted.md", 2, kind)
    assert text.split()[-1] not in str(hit)
    assert all(part not in str(hit) for part in ("10.0.0", "fd00", "fe80", "someone"))


@pytest.mark.parametrize(
    "text",
    [
        _planted_address("example.invalid"),
        _planted_address("sub.example.invalid"),
        "hello" + "@" + "burnbird.racingcrow.com",
        "127.0.0." + "1",
        "[::" + "1]",
        "192.0.2." + "10",
        "198.51.100." + "10",
        "203.0.113." + "10",
        "2001:db8::" + "10",
        "at 11:59:59 and 2026-11-15T10:00:00Z",
        "version 1.20.1 and pattern (?::[a-z])",
        "a[::2] and a[1:2]",
    ],
)
def test_an_allowed_form_is_not_a_hit(text: str) -> None:
    assert list(scan_text("docs/allowed.md", text)) == []


def test_the_bot_addresses_are_allowed_only_in_the_commit_policy() -> None:
    bot = "noreply" + "@" + "github.com"
    assert list(scan_text(".github/scripts/commit_policy.py", bot)) == []
    assert len(list(scan_text("src/burnbird/web.py", bot))) == 1


def test_a_file_that_is_not_text_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "image.bin").write_bytes(b"\xff\xfe" + ("10.0.0." + "1").encode())
    assert scan(tmp_path, ["image.bin"]) == []

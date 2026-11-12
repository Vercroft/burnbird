"""Check a pull request's commits, title and description against the commit policy.

Default-deny: a commit passes only when every identity, trailer key and link host in it is on an allow list.

- Author, committer and every Co-authored-by must be an approved identity: the lines of the organisation variable
  ALLOWED_COMMIT_IDENTITIES (an email per line, or "Name <email>"; blank lines and lines starting with # are
  ignored), plus Dependabot's own commits. GitHub's own address is accepted as committer only (the merge button and
  web edits), never as author or co-author.
- Every trailer key must be in ALLOWED_TRAILER_KEYS.
- Every link in a commit message, the title or the description must point to a host that the url-host rule of
  .gitleaks.toml allows (its "placeholder hosts" and "reviewed public hosts" lists): one list for the whole
  repository.
- No em dash or en dash in a commit message, the title or the description.

Dependabot writes its own text (release notes from other sites), so on a pull request that Dependabot opened, its own
commits and the title and description are checked for identities only.

CI logs are public, so output names only the commit (or "title" or "description") and the rule that failed, never
text taken from the input.

Usage (CI): BASE_SHA=... HEAD_SHA=... PR_AUTHOR=... PR_TITLE=... PR_BODY=... ALLOWED_COMMIT_IDENTITIES=... \
    python3 .github/scripts/commit_policy.py
"""

import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEPENDABOT_EMAIL: Final = "49699333+dependabot[bot]@users.noreply.github.com"
GITHUB_COMMITTER_EMAIL: Final = "noreply@github.com"
ALLOWED_TRAILER_KEYS: Final = frozenset({"co-authored-by", "signed-off-by"})
DEPENDABOT_LOGIN: Final = "dependabot[bot]"

GITLEAKS_CONFIG: Final = Path(__file__).resolve().parents[2] / ".gitleaks.toml"
LINK_RULE_ID: Final = "url-host"
HOST_LISTS: Final = ("placeholder hosts", "reviewed public hosts")

DASHES: Final = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")  # en dash, em dash
ANGLE_EMAIL: Final = re.compile(r"<([^>]*)>")

FIELD_SEP: Final = "\x1f"
RECORD_SEP: Final = "\x1e"

RULE_DASH: Final = "contains an em dash or en dash"
RULE_LINK: Final = "links to a host that is not reviewed"


class PolicyConfigError(Exception):
    """The link host lists could not be read from .gitleaks.toml."""


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit as the policy sees it."""

    sha: str
    author: str
    committer: str
    trailers: tuple[tuple[str, str], ...]
    message: str


@dataclass(frozen=True, slots=True)
class LinkRule:
    """How links are found, and which hosts they may point to."""

    link: re.Pattern[str]
    allowed_hosts: tuple[re.Pattern[str], ...]

    def bad_link_count(self, text: str) -> int:
        """Count the links in text whose host is on no list."""
        hosts = (match.group(1).lower().rstrip(".") for match in self.link.finditer(text))
        return sum(1 for host in hosts if not any(allowed.search(host) for allowed in self.allowed_hosts))


def load_link_rule(config: Path = GITLEAKS_CONFIG) -> LinkRule:
    """Read the url-host rule and its two host lists from the gitleaks config."""
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        rule = next(rule for rule in data["rules"] if rule.get("id") == LINK_RULE_ID)
        lists = {
            name: allowlist["regexes"]
            for allowlist in rule["allowlists"]
            for name in HOST_LISTS
            if str(allowlist.get("description", "")).startswith(name)
        }
        if set(lists) != set(HOST_LISTS):
            raise PolicyConfigError("a host list is missing")
        return LinkRule(
            link=re.compile(rule["regex"]),
            allowed_hosts=tuple(re.compile(regex) for name in HOST_LISTS for regex in lists[name]),
        )
    except (
        OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError, StopIteration, re.error
    ) as err:
        raise PolicyConfigError(type(err).__name__) from err


def email_of(text: str) -> str:
    """Return the address in "Name <email>" or a bare address, lower case."""
    match = ANGLE_EMAIL.search(text)
    return (match.group(1) if match else text).strip().lower()


def parse_allowed(text: str | None) -> frozenset[str]:
    """Read the approved addresses from the variable's text."""
    lines = (line.strip() for line in (text or "").splitlines())
    return frozenset(email_of(line) for line in lines if line and not line.startswith("#"))


def text_problems(text: str, links: LinkRule) -> list[str]:
    """Return the rules a piece of free text breaks: dashes and link hosts. Never the text itself."""
    problems = []
    if DASHES.search(text):
        problems.append(RULE_DASH)
    problems.extend([RULE_LINK] * links.bad_link_count(text))
    return problems


def commit_problems(
    commit: Commit, allowed: frozenset[str], links: LinkRule, *, dependabot_pr: bool = False
) -> list[str]:
    """Return every rule this commit breaks; Dependabot's own commits on its pull request: identities only."""
    people = allowed | {DEPENDABOT_EMAIL}
    problems = []
    if commit.author.lower() not in people:
        problems.append("author is not approved")
    if commit.committer.lower() not in people | {GITHUB_COMMITTER_EMAIL}:
        problems.append("committer is not approved")
    for key, value in commit.trailers:
        if key.lower() not in ALLOWED_TRAILER_KEYS:
            problems.append("has a trailer key that is not allowed")
        elif key.lower() == "co-authored-by" and email_of(value) not in people:
            problems.append("co-author is not approved")
    if not (dependabot_pr and commit.author.lower() == DEPENDABOT_EMAIL):
        problems.extend(text_problems(commit.message, links))
    return problems


def read_commits(base: str, head: str, cwd: Path | None = None) -> list[Commit]:
    """Read every commit in base..head from git."""
    fields = ["%H", "%ae", "%ce", "%(trailers:only,unfold)", "%B"]
    log = subprocess.run(
        ["git", "log", f"--format={FIELD_SEP.join(fields)}{RECORD_SEP}", f"{base}..{head}"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    commits = []
    for record in log.split(RECORD_SEP):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, author, committer, trailer_text, message = record.split(FIELD_SEP, 4)
        trailers = tuple(
            (key.strip(), value.strip())
            for key, _, value in (line.partition(":") for line in trailer_text.splitlines() if line.strip())
        )
        commits.append(Commit(sha, author.strip(), committer.strip(), trailers, message))
    return commits


def main(config: Path = GITLEAKS_CONFIG) -> int:
    """Run the policy on the pull request described by the environment."""
    base, head = os.environ.get("BASE_SHA", ""), os.environ.get("HEAD_SHA", "")
    if not base or not head:
        print("BASE_SHA and HEAD_SHA are required", file=sys.stderr)
        return 2
    try:
        links = load_link_rule(config)
    except PolicyConfigError:
        print("cannot read the reviewed link hosts from the url-host rule of .gitleaks.toml", file=sys.stderr)
        return 2
    dependabot_pr = os.environ.get("PR_AUTHOR") == DEPENDABOT_LOGIN
    allowed = parse_allowed(os.environ.get("ALLOWED_COMMIT_IDENTITIES"))
    if not allowed and not dependabot_pr:
        print("the organisation variable ALLOWED_COMMIT_IDENTITIES is empty or not visible here", file=sys.stderr)
        return 1
    failures = [
        f"commit {commit.sha[:12]}: {problem}"
        for commit in read_commits(base, head)
        for problem in commit_problems(commit, allowed, links, dependabot_pr=dependabot_pr)
    ]
    if not dependabot_pr:
        for label, variable in (("title", "PR_TITLE"), ("description", "PR_BODY")):
            text = os.environ.get(variable, "")
            failures.extend(f"{label}: {problem}" for problem in text_problems(text, links))
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(f"{len(failures)} commit policy problem(s)", file=sys.stderr)
        return 1
    print("every commit, the title and the description follow the commit policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())

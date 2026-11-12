"""Tests for commit_policy.py: each rule passes a clean case and fails a planted one.

Planted values are built at run time, so none is ever committed: dashes from code points, an unreviewed link host
from parts (a literal link would trip this repository's own leak check). Planted people use the reserved example.com
domain. Tests that run git work only in a throwaway repository (see _ThrowawayRepo).
"""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import commit_policy as policy

APPROVED = "1+approved@users.noreply.github.com"
STRANGER = "stranger@example.com"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
UNREVIEWED_HOST = "unreviewed-host" + "." + "org"
UNREVIEWED_LINK = "https" + "://" + UNREVIEWED_HOST + "/page"
LINKS = policy.load_link_rule()


class _ThrowawayRepo:
    """A git repository in a new temporary folder; refuses to run git anywhere else."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name).resolve()
        self._env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CEILING_DIRECTORIES": str(self.path.parent),
        }
        self.git("init", "--quiet", "--initial-branch=main")

    def git(self, *args: str, author: str = APPROVED, committer: str = APPROVED) -> str:
        """Run git in this repository only, with the given author and committer addresses."""
        top = self.path
        if not str(top).startswith(str(Path(tempfile.gettempdir()).resolve())):
            raise AssertionError("refusing to run git outside a throwaway folder")
        env = self._env | {
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": author,
            "GIT_COMMITTER_NAME": "c",
            "GIT_COMMITTER_EMAIL": committer,
        }
        return subprocess.run(
            ["git", "-C", str(top), *args], env=env, check=True, capture_output=True, text=True, timeout=30
        ).stdout.strip()

    def commit(self, message: str, *, author: str = APPROVED, committer: str = APPROVED) -> str:
        """Make an empty commit and return its SHA."""
        self.git("commit", "--quiet", "--allow-empty", "-m", message, author=author, committer=committer)
        return self.git("rev-parse", "HEAD")

    def close(self) -> None:
        """Delete the folder."""
        self._dir.cleanup()


class ParseAllowed(unittest.TestCase):
    def test_reads_bare_and_named_addresses_and_skips_comments(self) -> None:
        text = f"# approved people\n\n{APPROVED}\nSomeone <Second@Users.Noreply.Github.Com>\n"
        self.assertEqual(
            policy.parse_allowed(text), frozenset({APPROVED, "second@users.noreply.github.com"})
        )

    def test_empty_or_missing_gives_nothing(self) -> None:
        self.assertEqual(policy.parse_allowed(None), frozenset())
        self.assertEqual(policy.parse_allowed("\n# only a comment\n"), frozenset())


class LinkHosts(unittest.TestCase):
    def test_reviewed_host_passes(self) -> None:
        self.assertEqual(policy.text_problems("see https://docs.github.com/en/actions", LINKS), [])

    def test_placeholder_host_passes(self) -> None:
        self.assertEqual(policy.text_problems("see https://example.com/x", LINKS), [])

    def test_unknown_host_fails(self) -> None:
        self.assertEqual(policy.text_problems(f"see {UNREVIEWED_LINK}", LINKS), [policy.RULE_LINK])

    def test_host_that_only_starts_with_a_reviewed_name_fails(self) -> None:
        link = "https" + "://" + "github.com." + UNREVIEWED_HOST + "/"
        self.assertEqual(policy.text_problems(link, LINKS), [policy.RULE_LINK])

    def test_unreadable_config_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.toml"
            with self.assertRaises(policy.PolicyConfigError):
                policy.load_link_rule(missing)
            broken = Path(folder) / "broken.toml"
            broken.write_text('[[rules]]\nid = "url-host"\nregex = "x"\n', encoding="utf-8")
            with self.assertRaises(policy.PolicyConfigError):
                policy.load_link_rule(broken)


class TextProblems(unittest.TestCase):
    def test_clean_text_passes(self) -> None:
        self.assertEqual(policy.text_problems("Fix the forecast: see https://github.com/x/y and docs.", LINKS), [])

    def test_em_dash_fails(self) -> None:
        self.assertEqual(policy.text_problems(f"a {EM_DASH} b", LINKS), [policy.RULE_DASH])

    def test_en_dash_fails(self) -> None:
        self.assertEqual(policy.text_problems(f"1{EN_DASH}2", LINKS), [policy.RULE_DASH])


class CommitProblems(unittest.TestCase):
    allowed = frozenset({APPROVED})

    @staticmethod
    def make(
        *,
        author: str = APPROVED,
        committer: str = APPROVED,
        trailers: tuple[tuple[str, str], ...] = (),
        message: str = "Fix it\n",
    ) -> policy.Commit:
        return policy.Commit("0" * 40, author, committer, trailers, message)

    def problems(self, commit: policy.Commit, *, dependabot_pr: bool = False) -> list[str]:
        return policy.commit_problems(commit, self.allowed, LINKS, dependabot_pr=dependabot_pr)

    def test_approved_commit_passes(self) -> None:
        self.assertEqual(self.problems(self.make()), [])

    def test_unknown_author_fails(self) -> None:
        self.assertEqual(self.problems(self.make(author=STRANGER)), ["author is not approved"])

    def test_unknown_committer_fails(self) -> None:
        self.assertEqual(self.problems(self.make(committer=STRANGER)), ["committer is not approved"])

    def test_unknown_co_author_fails(self) -> None:
        commit = self.make(trailers=(("Co-authored-by", f"S <{STRANGER}>"),))
        self.assertEqual(self.problems(commit), ["co-author is not approved"])

    def test_approved_co_author_passes(self) -> None:
        self.assertEqual(self.problems(self.make(trailers=(("Co-authored-by", f"A <{APPROVED}>"),))), [])

    def test_unlisted_trailer_key_fails(self) -> None:
        commit = self.make(trailers=(("Made-by", "anything"),))
        self.assertEqual(self.problems(commit), ["has a trailer key that is not allowed"])

    def test_github_address_passes_as_committer_only(self) -> None:
        github = policy.GITHUB_COMMITTER_EMAIL
        self.assertEqual(self.problems(self.make(committer=github)), [])
        self.assertEqual(self.problems(self.make(author=github)), ["author is not approved"])
        commit = self.make(trailers=(("Co-authored-by", f"GitHub <{github}>"),))
        self.assertEqual(self.problems(commit), ["co-author is not approved"])

    def test_dependabot_passes_as_author_and_committer(self) -> None:
        dependabot = policy.DEPENDABOT_EMAIL
        self.assertEqual(self.problems(self.make(author=dependabot, committer=dependabot)), [])
        github = policy.GITHUB_COMMITTER_EMAIL
        self.assertEqual(self.problems(self.make(author=dependabot, committer=github)), [])

    def test_dependabot_text_is_skipped_only_on_its_own_pull_request(self) -> None:
        commit = self.make(author=policy.DEPENDABOT_EMAIL, message=f"Bump x {EN_DASH} {UNREVIEWED_LINK}\n")
        self.assertEqual(self.problems(commit, dependabot_pr=True), [])
        self.assertEqual(self.problems(commit, dependabot_pr=False), [policy.RULE_DASH, policy.RULE_LINK])


class Main(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _ThrowawayRepo()
        self.addCleanup(self.repo.close)
        self.base = self.repo.commit("Base")
        self.clean = self.repo.commit("Clean commit\n\nA reason.\n")
        env = {
            "BASE_SHA": self.base,
            "HEAD_SHA": self.clean,
            "PR_AUTHOR": "someone",
            "PR_TITLE": "Clean title",
            "PR_BODY": "Clean body.",
            "ALLOWED_COMMIT_IDENTITIES": APPROVED,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, config: Path = policy.GITLEAKS_CONFIG) -> tuple[int, str]:
        """Run main against the throwaway repository; return its exit code and everything it printed."""
        real = policy.read_commits
        output = io.StringIO()
        with (
            mock.patch.object(policy, "read_commits", lambda b, h: real(b, h, cwd=self.repo.path)),
            contextlib.redirect_stderr(output),
            contextlib.redirect_stdout(output),
        ):
            code = policy.main(config)
        return code, output.getvalue()

    def test_reads_identities_trailers_and_message(self) -> None:
        sha = self.repo.commit(f"Add a thing\n\nWhy it matters.\n\nCo-authored-by: S <{STRANGER}>\n", author=STRANGER)
        commits = policy.read_commits(self.clean, sha, cwd=self.repo.path)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].sha, sha)
        self.assertEqual(commits[0].author, STRANGER)
        self.assertEqual(commits[0].committer, APPROVED)
        self.assertEqual(commits[0].trailers, (("Co-authored-by", f"S <{STRANGER}>"),))
        self.assertIn("Why it matters.", commits[0].message)

    def test_clean_range_passes(self) -> None:
        self.assertEqual(self.run_main()[0], 0)

    def test_planted_commit_fails(self) -> None:
        bad = self.repo.commit(f"Bad {EM_DASH} commit\n", author=STRANGER)
        with mock.patch.dict(os.environ, {"HEAD_SHA": bad}):
            code, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"commit {bad[:12]}: author is not approved", output)

    def test_dash_in_title_fails(self) -> None:
        with mock.patch.dict(os.environ, {"PR_TITLE": f"Title {EN_DASH} dash"}):
            self.assertEqual(self.run_main()[0], 1)

    def test_empty_variable_fails(self) -> None:
        with mock.patch.dict(os.environ, {"ALLOWED_COMMIT_IDENTITIES": ""}):
            self.assertEqual(self.run_main()[0], 1)

    def test_refused_link_host_is_never_printed(self) -> None:
        bad = self.repo.commit(f"Add a link\n\nSee {UNREVIEWED_LINK}\n")
        with mock.patch.dict(os.environ, {"HEAD_SHA": bad, "PR_BODY": f"See {UNREVIEWED_LINK}"}):
            code, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"commit {bad[:12]}: {policy.RULE_LINK}", output)
        self.assertIn(f"description: {policy.RULE_LINK}", output)
        self.assertNotIn(UNREVIEWED_HOST, output)
        self.assertNotIn("unreviewed", output)

    def test_unreadable_config_fails_without_printing_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, output = self.run_main(Path(folder) / "missing.toml")
        self.assertEqual(code, 2)
        self.assertIn("cannot read the reviewed link hosts", output)
        self.assertNotIn("github.com", output)


if __name__ == "__main__":
    unittest.main()

# Contributing to Burnbird

This file says how you can help and which rules we follow in this repository.

## Issues and discussions

We welcome bug reports, ideas and questions. Open an issue for a bug or a concrete request, and a discussion for a
question or an idea. Please search first, in case it is already there.

Never report a security problem in public: follow [SECURITY.md](SECURITY.md).

## Pull requests

We write all the code ourselves, so pull requests are limited to us. Please open an issue instead, and we will pick it
up.

## Rules

We follow the rules in [Engineering Practices](https://github.com/Vercroft/engineering-practices), written down in its
[STANDARD.md](https://github.com/Vercroft/engineering-practices/blob/main/STANDARD.md). This file adds only what is
specific here.

## Commands

Set up once: install [uv](https://docs.astral.sh/uv/), which installs the Python version in `.python-version`;
`uv sync --locked`; `pre-commit install`, whose hook runs the same gitleaks check with `.gitleaks.toml` as the
`gitleaks` CI job; and `VENV="${TMPDIR:-/tmp}/burnbird-venv"; python3 -m venv "$VENV"` for the workflow audit. Each
row is what CI runs, in the job named in the last column.

| To | Run | CI job |
|---|---|---|
| Install the project and its dev tools | `uv sync --locked` | Python |
| Check formatting | `uv run --locked ruff format --check` | Python |
| Lint | `uv run --locked ruff check` | Python |
| Check types | `uv run --locked mypy` | Python |
| Check the Python version agrees everywhere | `uv run --locked pytest tests/test_python_version.py` | Python |
| Run the tests (one test: `uv run --locked pytest tests/test_package.py`) | `uv run --locked pytest` | Python |
| Check every locked package is at least 7 days old | `uv lock --check` | dependency-age |
| Scan for secrets | `pre-commit run --all-files` | gitleaks |
| Audit the workflows | `"$VENV/bin/pip" install --require-hashes -r .github/requirements/zizmor.txt`, then `"$VENV/bin/zizmor" --offline .github/workflows` | zizmor |
| Test the commit policy | `python3 -m unittest discover --start-directory .github/scripts --pattern 'test_*.py'` | Commit policy |
| Check your commits | `BASE_SHA=$(git merge-base origin/main HEAD) HEAD_SHA=HEAD ALLOWED_COMMIT_IDENTITIES=<your noreply address> python3 .github/scripts/commit_policy.py` | Commit policy |
| Check the links | `lychee --offline --no-progress './**/*.md'` | Local links |
| Check the suppression listing | the `Suppressions are listed` step of `.github/workflows/ci.yml` | Suppressions are listed |

The commit policy allows only approved people as author, committer and co-author (the organisation variable
`ALLOWED_COMMIT_IDENTITIES`), only the trailer keys listed at the top of `.github/scripts/commit_policy.py`, only
links to the reviewed hosts in `.gitleaks.toml` (the `url-host` rule's lists), and no em or en dash in commit
messages, the pull request title or its description. Its output names only the commit and the rule, never the text.

## Map

| Path | Holds |
|---|---|
| `README.md` | What Burnbird is |
| `pyproject.toml`, `uv.lock` | The Python project, its dev tools (exact pins) and the lint, type and test settings |
| `.python-version` | The one Python version; `tests/test_python_version.py` keeps the other copies in step |
| `src/burnbird/` | The application package |
| `tests/` | Its tests |
| `LICENSE` | The licence, FSL-1.1-MIT, unchanged except its copyright line |
| `SECURITY.md` | How to report a vulnerability |
| `.gitleaks.toml`, `.pre-commit-config.yaml` | The leak patterns and the commit-time hook |
| `.github/CODEOWNERS` | Who reviews every change |
| `.github/workflows/` | CI (Python, gitleaks, zizmor, suppressions, commit policy), dependency-age and its daily re-run for Dependabot pull requests, links, CodeQL and Scorecard |
| `.github/scripts/` | The commit policy check and its tests |
| `.github/requirements/` | Hash-pinned CI tools (zizmor) |
| `.github/dependabot.yml` | Monthly grouped updates for the Python packages, the actions and the CI tools |
| `.suppressions` | Every lint suppression, one line each (none yet) |

## Boundaries

- Secrets never go in git.
- Every action in a workflow is pinned by full commit SHA with its release in a comment, and the release is at least 7
  days old.
- Python packages are pinned exactly in `pyproject.toml` and locked in `uv.lock`; uv refuses versions younger than 7
  days (`exclude-newer`) and source builds (`no-build`).
- A suppression (`# noqa`, `# type: ignore` or `# zizmor: ignore[rule]`, each with a reason) is listed in
  `.suppressions` as `path: comment`, sorted; the `Suppressions are listed` job fails on any difference.
- Test fixtures never hold a real secret, a real person's address or an em or en dash: build planted values at run
  time.
- `LICENSE` stays the published text, with only its copyright line filled in.
- Call the licence "Fair Source (FSL-1.1-MIT)".

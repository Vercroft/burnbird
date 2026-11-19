# Contributing to Burnbird

Burnbird counts the CI minutes of every GitHub Actions runner and forecasts the day free minutes run out. We follow
the [Engineering Practices standard](https://github.com/Vercroft/engineering-practices/blob/main/STANDARD.md); this
file adds only what is specific here.

- Issues and discussions are welcome: an issue for a bug or a concrete request, a discussion for a question or an idea.
- Search the existing issues and discussions first.
- Pull requests are limited to this repository's collaborators: open an issue instead.
- Report a security problem privately, as [SECURITY.md](SECURITY.md) says, never in public.

## Commands

Set up once: install [uv](https://docs.astral.sh/uv/) (it installs the Python in `.python-version`), then run
`uv sync --locked` and `pre-commit install`. Start the test Postgres and Mailpit as
[docs/run-the-tests-locally.md](docs/run-the-tests-locally.md) says.

| To | Run | CI job |
|---|---|---|
| Install the project and its dev tools | `uv sync --locked` | Python |
| Check formatting | `uv run --locked ruff format --check` | Python |
| Lint | `uv run --locked ruff check` | Python |
| Check types | `uv run --locked mypy` | Python |
| Check the Python version agrees everywhere | `uv run --locked pytest -n 0 tests/test_python_version.py` | Python |
| Run the full tests (in parallel, one worker per CPU) | `uv run --locked pytest` | Python |
| Run one test file in one process (so `-s` shows print output) | `uv run --locked pytest -n 0 tests/test_package.py` | none |
| Check every locked package is at least 7 days old | `uv lock --check` | dependency-age |
| Check every image your branch adds is at least 7 days old (needs Docker with buildx) | `python3 scripts/check-image-age.py --base origin/main` | dependency-age |
| Run every small check (the hooks in `.pre-commit-config.yaml`; CI runs gitleaks as Secret scan / Full history) | `python3 -m venv "${TMPDIR:-/tmp}/checks" && "${TMPDIR:-/tmp}/checks/bin/pip" install --quiet --require-hashes --only-binary :all: -r .github/requirements/pre-commit.txt -r .github/requirements/zizmor.txt && PATH="${TMPDIR:-/tmp}/checks/bin:$PATH" pre-commit run --all-files --hook-stage manual` | Repository checks |
| Check your commits | `BASE_SHA=$(git merge-base origin/main HEAD) HEAD_SHA=HEAD ALLOWED_COMMIT_IDENTITIES=<your noreply address> python3 .github/scripts/commit_policy.py` | Repository checks |
| Check the links between the repository's files | `lychee --offline --no-progress './**/*.md'` | Local links |

`Gate summary` passes only when every CI job passed or was skipped; it is the CI check the ruleset requires.

## Map

| Path | Holds |
|---|---|
| `PRIVACY.md` | What Burnbird stores about people, why, who handles it, how long, and their rights |
| `LICENSE` | The licence, Fair Source (FSL-1.1-MIT) |
| `src/burnbird/` | The application: one folder per feature, shared storage at the root; each module's docstring says what it does |
| `src/burnbird/account/` | The signed-in user's own data: the "Your data" page, the email switch, Delete my data |
| `src/burnbird/auth/` | Sign-in with GitHub, sessions, CSRF and the access filters |
| `src/burnbird/billing/` | The provider table, job classification, minute counting and the run-out forecast |
| `src/burnbird/billing/usage.py` | Each allowance's use per UTC day, its top repositories and its forecast: where the page and the emails read jobs |
| `src/burnbird/email/` | Email choices and consent, the outbox and its sender, the run-out warnings' checks and wording at send, one-click unsubscribe, the provider's webhook |
| `src/burnbird/github/` | The GitHub webhook and its worker step, the App's client, reconciliation, backfill, redelivery |
| `src/burnbird/migrations/` | The Alembic migrations, shipped in the package |
| `src/burnbird/retention.py` | Every deletion and purge, and the lock order (`lock_repositories`) |
| `src/burnbird/web.py`, `src/burnbird/worker.py` | The two processes: the web app and one worker pass |
| `tests/` | The tests, in folders that mirror the feature folders |
| `docs/` | [Run the tests locally](docs/run-the-tests-locally.md), [add a provider](docs/add-a-provider.md), and the [invariants](docs/invariants.md) |
| `pyproject.toml`, `uv.lock`, `.python-version` | The project, its exact pins and tool settings, and the one Python version |
| `scripts/` | The vendored image-age and suppressions checks |
| `.suppressions` | Every lint suppression, one line each |
| `.gitleaks.toml`, `.pre-commit-config.yaml` | The secret and leak rules, and the hooks: every small check |
| `.github/workflows/` | CI and its Gate summary, the secret scan, dependency-age and its re-run, links, CodeQL, Scorecard |
| `.github/scripts/` | The commit policy check and its tests |
| `.github/requirements/` | The hash-pinned CI tools |
| `.github/dependabot.yml` | The monthly grouped updates |

## Boundaries

- Never commit a secret. Read secrets only from the files these settings name: `BURNBIRD_WEBHOOK_SECRET_FILE`,
  `BURNBIRD_GITHUB_CLIENT_SECRET_FILE`, `BURNBIRD_OAUTH_STATE_KEY_FILE`, `BURNBIRD_GITHUB_APP_PRIVATE_KEY_FILE`,
  `BURNBIRD_ZOHO_API_KEY_FILE` and `BURNBIRD_ZOHO_WEBHOOK_KEY_FILE`.
- Never put a real secret, a real person's address or an em or en dash in a test: build planted values at run time,
  with addresses on `example.invalid`.
- Never edit `uv.lock` by hand: run `uv lock`.
- Never edit `scripts/check-image-age.py`, `tests/test_check_image_age.py` or `scripts/check-suppressions.sh`: a new
  version replaces the file whole, with its sha256 in `tests/test_vendored.py`.
- Never change `LICENSE` beyond its copyright line, and call the licence "Fair Source (FSL-1.1-MIT)".
- Never loosen the mass-deletion guard: [invariants, GitHub](docs/invariants.md#github).
- Never loosen the access filters: [invariants, access](docs/invariants.md#access).
- Never add a CSRF exemption without review: [invariants, requests](docs/invariants.md#requests-and-pages).
- Never lock several repositories outside `retention.lock_repositories`:
  [invariants, stored data](docs/invariants.md#stored-data-and-deletion).
- Never store something new about people without updating `PRIVACY.md` in the same pull request.

## Done means

- Every command in the table passes, as CI runs it.
- New behaviour has a test that failed before the change.
- Every document the change makes wrong is updated or deleted in the same pull request.

## Commits and pull requests

- Follow the standard's [section 6](https://github.com/Vercroft/engineering-practices/blob/main/STANDARD.md#6-git-pull-requests-and-merging).
- Write a commit subject as a plain sentence of at most 72 characters, with no prefix.
- The commit policy step of Repository checks enforces identities, trailers, link hosts and dashes;
  `.github/scripts/commit_policy.py` says what it allows.
- List every suppression a pull request adds in its description.

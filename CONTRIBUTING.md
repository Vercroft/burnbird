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

The storage tests need a throwaway Postgres, named in `BURNBIRD_TEST_DATABASE_URL`; without it they fail, never skip.
Start one with Docker, from the image the CI job pins (it is written only there), and stop it when you are done:

```sh
docker run --rm -d --name burnbird-test-postgres -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=burnbird_test \
  -p 127.0.0.1:55432:5432 "$(grep -oE 'postgres:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)"
export BURNBIRD_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/burnbird_test
docker stop burnbird-test-postgres
```

| To | Run | CI job |
|---|---|---|
| Install the project and its dev tools | `uv sync --locked` | Python |
| Check formatting | `uv run --locked ruff format --check` | Python |
| Lint | `uv run --locked ruff check` | Python |
| Check types | `uv run --locked mypy` | Python |
| Check the Python version agrees everywhere | `uv run --locked pytest tests/test_python_version.py` | Python |
| Run the tests, with the test Postgres (one test: `uv run --locked pytest tests/test_package.py`) | `uv run --locked pytest` | Python |
| Check every locked package is at least 7 days old | `uv lock --check` | dependency-age |
| Check every image your branch adds is at least 7 days old (needs Docker with buildx) | `python3 scripts/check-image-age.py --base origin/main` | dependency-age |
| Scan for secrets | `pre-commit run --all-files` | gitleaks |
| Audit the workflows | `"$VENV/bin/pip" install --require-hashes -r .github/requirements/zizmor.txt`, then `"$VENV/bin/zizmor" --offline .github/workflows` | zizmor |
| Test the commit policy | `python3 -m unittest discover --start-directory .github/scripts --pattern 'test_*.py'` | Commit policy |
| Check your commits | `BASE_SHA=$(git merge-base origin/main HEAD) HEAD_SHA=HEAD ALLOWED_COMMIT_IDENTITIES=<your noreply address> python3 .github/scripts/commit_policy.py` | Commit policy |
| Check the links | `lychee --offline --no-progress './**/*.md'` | Local links |
| Check the suppression listing | the `Suppressions are listed` step of `.github/workflows/ci.yml` | Suppressions are listed |

The commit policy allows only approved people as author, committer and co-author (the organisation variable
`ALLOWED_COMMIT_IDENTITIES`), only the trailer keys listed at the top of `.github/scripts/commit_policy.py`, only
links to the hosts in the `url-host` rule's placeholder and reviewed public lists in `.gitleaks.toml`, and no em or en dash in commit
messages, the pull request title or its description. Its output names only the commit and the rule, never the text.

## Map

| Path | Holds |
|---|---|
| `README.md` | What Burnbird is |
| `pyproject.toml`, `uv.lock` | The Python project, its dev tools (exact pins) and the lint, type and test settings |
| `.python-version` | The one Python version; `tests/test_python_version.py` keeps the other copies in step |
| `src/burnbird/` | The application package: one folder per feature, and the shared storage modules at its root |
| `src/burnbird/billing/` | Billing: the provider table, job classification, minute counting and the run-out forecast |
| `src/burnbird/billing/providers.toml` | The provider table: each runner size's label patterns and its price or weight a minute, with the pages each fact comes from and the date they were read |
| `src/burnbird/billing/providers.py` | Loads the provider table and refuses an invalid one, naming the row |
| `src/burnbird/billing/classify.py` | Classifies a job: its provider, runner size and price or weight a minute |
| `src/burnbird/billing/counting.py` | Bills a job: its minutes after the provider's rounding, its dollars or allowance units, and the day it counts on |
| `src/burnbird/billing/forecast.py` | An allowance's cycle, use, pace and run-out date, and its warnings as data |
| `src/burnbird/github/` | GitHub: `router.py` takes `POST /github/webhook` (signature, then the fields of a handled event, stored once); `schemas.py` names the fields kept per event; `signature.py` checks the HMAC; `events.py` is the worker's step that turns stored deliveries into jobs, installations and deletions (an uninstall, removed repositories, a revoked authorization); `config.py` reads the secret files |
| `src/burnbird/auth/` | Sign-in with GitHub: `router.py` takes `/auth/login`, `/auth/callback` and `/auth/logout`; `service.py` holds Authlib's GitHub client (state and the PKCE S256 verifier), the user's lists from GitHub and the one-transaction store; `dependencies.py` gives the signed-in user, the access filters and the CSRF check; `config.py` reads the settings |
| `src/burnbird/logs.py` | The commands' logging: one formatter that redacts token-shaped values, `Authorization` values, OAuth secrets and the callback's code and state |
| `src/burnbird/web.py` | Creates the web app: the body limit and each feature's router, nothing else |
| `src/burnbird/worker.py` | One pass of the worker |
| `src/burnbird/models.py` | The stored tables, webhook deliveries included (their validated fields only, never the body), and each user's installations and repositories from their last sign-in |
| `src/burnbird/database.py` | The database URL from the environment, and an engine whose connections and statements time out |
| `src/burnbird/migrations/` | The Alembic migrations, shipped inside the package; `[tool.alembic]` in `pyproject.toml` points at them |
| `src/burnbird/retention.py` | What is deleted and when: jobs after 90 days, deliveries after 7, expired sessions, "Delete my data", a revoked authorization, an uninstall, and repositories removed from an installation |
| `tests/` | Its tests, in folders that mirror the feature folders (`tests/auth/`, `tests/billing/`, `tests/github/`), each with an `__init__.py` so mypy accepts the same test file name in two folders; `tests/conftest.py` gives the storage tests a migrated Postgres and rolls each test back |
| `scripts/check-image-age.py`, `tests/test_check_image_age.py` | The image-age check and its test, vendored unchanged; `tests/test_vendored.py` holds their recorded sha256 |
| `LICENSE` | The licence, FSL-1.1-MIT, unchanged except its copyright line |
| `SECURITY.md` | How to report a vulnerability |
| `.gitleaks.toml`, `.pre-commit-config.yaml` | The leak patterns and the commit-time hook; the `url-host` rule's last list holds the reviewed hosts only this repository cites |
| `.github/CODEOWNERS` | Who reviews every change |
| `.github/workflows/` | CI (Python with its Postgres service, gitleaks, zizmor, suppressions, commit policy), dependency-age (packages and images) and its daily re-run for Dependabot pull requests, links, CodeQL and Scorecard |
| `.github/scripts/` | The commit policy check and its tests |
| `.github/requirements/` | Hash-pinned CI tools (zizmor) |
| `.github/dependabot.yml` | Monthly grouped updates for the Python packages, the actions, the workflow images and the CI tools; `tests/test_images.py` checks the images are pinned and Postgres majors are held |
| `.suppressions` | Every lint suppression, one line each |

## Adding a provider or a runner size

One pull request:

1. Add rows to `src/burnbird/billing/providers.toml`: one row per runner size, its price or weight written out as a string,
   and the pages its facts come from with the date you read them. A new host also goes in the `url-host` rule's
   repository list in `.gitleaks.toml`.
2. Add one fixture job per new row to `FIXTURES` in `tests/billing/test_classify.py`; the coverage test fails for a row
   without one.
3. A new runner size of a listed provider is data only. A new provider also needs its step in the classification
   order in `src/burnbird/billing/classify.py`, and code changes for a new rounding rule or a new kind of allowance.

## Boundaries

- Secrets never go in git.
- Secrets are read only from files named in the environment: the webhook secret (`BURNBIRD_WEBHOOK_SECRET_FILE`), the
  App's client secret (`BURNBIRD_GITHUB_CLIENT_SECRET_FILE`) and the OAuth state key (`BURNBIRD_OAUTH_STATE_KEY_FILE`,
  32 characters or more). The client ID (`BURNBIRD_GITHUB_CLIENT_ID`) and the site's origin (`BURNBIRD_BASE_URL`,
  `https://<host>` with no path) come from the environment. Tests make random values.
- Endpoints are plain `def`; `async def` only for a dependency that awaits the request body and makes no blocking call,
  and for `/auth/login` and `/auth/callback`, because Authlib's client is async; the callback's database work runs in
  the thread pool.
- `request.session` holds only Authlib's OAuth state, in the signed `__Host-burnbird_oauth` cookie: no code reads or
  writes it except Authlib and the callback, which clears it. The sign-in session is the `__Host-burnbird_session`
  cookie and the `session` table, which stores only its SHA-256.
- Access: everything a page shows (names and numbers alike) is filtered by `allowed_repositories`. GitHub lists an
  installation for anyone who can access one of its repositories, outside collaborators included, so
  `allowed_installations` alone never grants a view or a setting. Account-wide numbers, the forecast and settings wait
  for the account-wide access rule, which is decided before the dashboard is built. A foreign ID answers 404.
- Every POST depends on `csrf_protected`: the `Origin` must be the site's origin exactly, and the form's `csrf_token`
  must be the session's (`csrf_token(request)`).
- No user access token is stored or logged; a sign-in uses it within its one request.
- Every action in a workflow is pinned by full commit SHA with its release in a comment, and the release is at least 7
  days old.
- Python packages are pinned exactly in `pyproject.toml` and locked in `uv.lock`; uv refuses versions younger than 7
  days (`exclude-newer`) and source builds (`no-build`).
- A suppression (`# noqa`, `# type: ignore` or `# zizmor: ignore[rule]`, each with a reason) is listed in
  `.suppressions` as `path: comment`, sorted; the `Suppressions are listed` job fails on any difference.
- Test fixtures never hold a real secret, a real person's address or an em or en dash: build planted values at run
  time.
- The database URL comes only from the environment (`BURNBIRD_DATABASE_URL`; `BURNBIRD_TEST_DATABASE_URL` for the
  tests), never from a file. The tests refuse any database not named `burnbird_test`.
- A schema change is a new migration in `src/burnbird/migrations/versions/` (`uv run --locked alembic revision
  --autogenerate`), never an edit to one already merged; `tests/test_migration.py` fails when the models and the
  migrations differ.
- Every image a workflow uses is pinned as `name:tag@sha256:<digest>` and written once; `tests/test_images.py` fails on
  a new image until it is classified.
- `scripts/check-image-age.py` and `tests/test_check_image_age.py` are never edited here; a new version replaces both
  files unchanged, with its new sha256 in `tests/test_vendored.py`.
- `LICENSE` stays the published text, with only its copyright line filled in.
- Call the licence "Fair Source (FSL-1.1-MIT)".

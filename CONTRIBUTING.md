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
`Secret scan / Full history` CI check; and `VENV="${TMPDIR:-/tmp}/burnbird-venv"; python3 -m venv "$VENV"` for the workflow audit. Each
row is what CI runs, in the job named in the last column.

The storage tests need a throwaway Postgres, named in `BURNBIRD_TEST_DATABASE_URL`; without it they fail, never skip.
Start one with Docker, from the image the CI job pins (it is written only there), and stop it when you are done.
On Docker Engine on Linux, run it on the host network, listening on loopback only: the tests then reach Postgres over
loopback, so a VPN or firewall that blocks traffic to container addresses does not stop them.

```sh
docker run --rm -d --name burnbird-test-postgres --network host -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB=burnbird_test "$(grep -oE 'postgres:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)" \
  -c listen_addresses=127.0.0.1 -c port=55432
export BURNBIRD_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/burnbird_test
docker stop burnbird-test-postgres
```

On Docker Desktop, publish the port instead:

```sh
docker run --rm -d --name burnbird-test-postgres -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=burnbird_test \
  -p 127.0.0.1:55432:5432 "$(grep -oE 'postgres:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)"
```

The email tests need a local Mailpit, named in `BURNBIRD_TEST_SMTP_URL` and `BURNBIRD_TEST_MAILPIT_URL`; without it
they fail, never skip. No test reaches a real mail server or the provider: provider answers come from an httpx
MockTransport. Run Mailpit from the image the CI job pins, listening on loopback only, and stop it when you are done
(on Docker Desktop, publish `127.0.0.1:1025:1025` and `127.0.0.1:8025:8025` instead of the host network):

```sh
docker run --rm -d --name burnbird-test-mailpit --network host \
  -e MP_SMTP_BIND_ADDR=127.0.0.1:1025 -e MP_UI_BIND_ADDR=127.0.0.1:8025 -e MP_DISABLE_VERSION_CHECK=true \
  -e MP_BLOCK_REMOTE_CSS_AND_FONTS=true -e MP_MAX_MESSAGE_SIZE=1 -e MP_MAX_MESSAGES=200 \
  "$(grep -oE 'axllent/mailpit:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)"
export BURNBIRD_TEST_SMTP_URL=smtp://127.0.0.1:1025 BURNBIRD_TEST_MAILPIT_URL=http://localhost:8025
docker stop burnbird-test-mailpit
```

| To | Run | CI job |
|---|---|---|
| Install the project and its dev tools | `uv sync --locked` | Python |
| Check formatting | `uv run --locked ruff format --check` | Python |
| Lint | `uv run --locked ruff check` | Python |
| Check types | `uv run --locked mypy` | Python |
| Check the Python version agrees everywhere | `uv run --locked pytest tests/test_python_version.py` | Python |
| Run the tests, with the test Postgres and Mailpit (one test: `uv run --locked pytest tests/test_package.py`) | `uv run --locked pytest` | Python |
| Check every locked package is at least 7 days old | `uv lock --check` | dependency-age |
| Check every image your branch adds is at least 7 days old (needs Docker with buildx) | `python3 scripts/check-image-age.py --base origin/main` | dependency-age |
| Scan for secrets (CI scans the full history through Vercroft's shared workflow) | `pre-commit run --all-files` | Secret scan / Full history |
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
| `PRIVACY.md` | What Burnbird stores about people, why, who else handles it, how long it is kept, and their rights; every change that stores something new updates it |
| `pyproject.toml`, `uv.lock` | The Python project, its dev tools (exact pins) and the lint, type and test settings |
| `.python-version` | The one Python version; `tests/test_python_version.py` keeps the other copies in step |
| `src/burnbird/` | The application package: one folder per feature, and the shared storage modules at its root |
| `src/burnbird/billing/` | Billing: the provider table, job classification, minute counting and the run-out forecast |
| `src/burnbird/billing/providers.toml` | The provider table: each runner size's label patterns and its price or weight a minute, with the pages each fact comes from and the date they were read |
| `src/burnbird/billing/providers.py` | Loads the provider table and refuses an invalid one, naming the row |
| `src/burnbird/billing/classify.py` | Classifies a job: its provider, runner size and price or weight a minute |
| `src/burnbird/billing/counting.py` | Bills a job: its minutes after the provider's rounding, its dollars or allowance units, and the day it counts on |
| `src/burnbird/billing/forecast.py` | An allowance's cycle, use, pace and run-out date, and its warnings as data |
| `src/burnbird/github/` | GitHub: `router.py` takes `POST /github/webhook` (signature, then the fields of a handled event, stored once); `schemas.py` names the fields kept per event; `signature.py` checks the HMAC; `events.py` is the worker's step that turns stored deliveries into jobs, installations and deletions (an uninstall, removed repositories, a revoked authorization); `client.py` is the App's own API client (the App JWT, installation tokens, paging, retries, rate limits); `reconcile.py` is the hourly reconciliation of the stored installations and repositories with GitHub's lists, which also queues every repository never backfilled; `backfill.py` stores a queued repository's jobs since its cycle start; `redeliver.py` is the hourly redelivery of failed webhook deliveries, which re-queues the backfill for what stays lost; `schedule.py` claims a due run of a periodic task and records how it ended; `config.py` reads the App's client ID and secret files |
| `src/burnbird/auth/` | Sign-in with GitHub: `router.py` takes `/auth/login`, `/auth/callback` and `/auth/logout`; `service.py` holds Authlib's GitHub client (state and the PKCE S256 verifier), the user's lists and primary verified email address from GitHub and the one-transaction store; `device.py` labels a session's device from the request headers; `dependencies.py` gives the signed-in user, the access filters, `account_access` (account-wide access), the CSRF check and a POST's form fields; `config.py` reads the settings |
| `src/burnbird/account/` | The signed-in user's own data: `router.py` takes `GET /you` (the "Your data" page), `POST /you/email` (the run-out warnings switch), `POST /you/delete` (Delete my data) and `GET /you/deleted`; `service.py` reads what the page shows; `templates/` holds its pages |
| `src/burnbird/email/` | Email: `preferences.py` writes each user's email choices once and records every change; `consent.txt` is the consent wording shown next to the switch, and `consent.py` gives it and its version (the start of its SHA-256); `config.py` reads the email settings (the provider with its bare send token, or a loopback Mailpit only); `message.py` renders an email from `templates/` (`layout.html` and `layout.txt` give every email its footer and, for usage email, its one-click unsubscribe) and builds the provider's JSON or a MIME message; `sender.py` makes one send and maps each answer to one outcome (sent, retry, reset, config, failed, or unknown, which is never resent); `webhook_signature.py` checks the provider's webhook signature (HMAC-SHA256 of the raw body, within five minutes), and `webhook.py` is the provider's webhook, served only with its key: a hard bounce pauses the address it names when that is the user's address now, the first pause kept; `outbox.py` queues an email once (its dedupe key) and is the worker's `email-send` step: it leases a queued row, checks it (expiry, address, suppression, choice, access, the rolling 24-hour caps), and sends it at most once while the row stays locked, so Delete my data waits for a send in progress (a worker killed, or a commit lost, after the POST sends it again: no code can tell the POST was made). The provider's daily limit or a configuration error pauses every send. Nothing queues an email yet |
| `src/burnbird/templates.py`, `src/burnbird/templates/` | The one Jinja2 environment for pages and email (autoescape on for `.html`, templates shipped in the package) and the shared base layout |
| `src/burnbird/security_headers.py` | The security headers every answer carries |
| `src/burnbird/logs.py` | The commands' logging: one formatter that redacts token-shaped values, `Authorization` values, OAuth secrets, the callback's code and state and an unsubscribe link's token, and masks an email address to its first character and domain |
| `src/burnbird/web.py` | Creates the web app: the security headers, the body limit and each feature's router, nothing else |
| `src/burnbird/worker.py` | One pass of the worker: the stored deliveries, then the periodic tasks that are due (reconciliation, redelivery), then at most one repository's backfill, then the queued emails, then the daily purge, each step isolated |
| `src/burnbird/purge.py` | The daily purge task: claims the run and calls `retention.run_purges` |
| `src/burnbird/models.py` | The stored tables, webhook deliveries included (their validated fields only, never the body), each user's installations and repositories from their last sign-in, their primary verified email address and whether email to it is paused, the email outbox, each session with its device label, the worker's periodic tasks, the repositories queued for backfill, and the redeliveries asked for |
| `src/burnbird/database.py` | The database URL from the environment, and an engine whose connections and statements time out |
| `src/burnbird/migrations/` | The Alembic migrations, shipped inside the package; `[tool.alembic]` in `pyproject.toml` points at them |
| `src/burnbird/retention.py` | What is deleted and when: jobs after 90 days, deliveries after 7, expired sessions, email addresses no sign-in read in 30 days, finished outbox emails after 62 days (all five through `run_purges`), "Delete my data", a revoked authorization, an uninstall, and repositories removed from an installation; `lock_repositories` writes down the lock order every path follows |
| `tests/test_public_text.py` | Fails on any email or IP address in a tracked file that is not on its allowlist |
| `tests/` | Its tests, in folders that mirror the feature folders (`tests/account/`, `tests/auth/`, `tests/billing/`, `tests/email/`, `tests/github/`), each with an `__init__.py` so mypy accepts the same test file name in two folders; `tests/conftest.py` gives the storage tests a migrated Postgres and rolls each test back |
| `scripts/check-image-age.py`, `tests/test_check_image_age.py` | The image-age check and its test, vendored unchanged; `tests/test_vendored.py` holds their recorded sha256 |
| `LICENSE` | The licence, FSL-1.1-MIT, unchanged except its copyright line |
| `SECURITY.md` | How to report a vulnerability |
| `.gitleaks.toml`, `.pre-commit-config.yaml` | The leak patterns and the commit-time hook; the `url-host` rule's last list holds the reviewed hosts only this repository cites |
| `.github/CODEOWNERS` | Who reviews every change |
| `.github/workflows/` | CI (Python with its Postgres and Mailpit services, zizmor, suppressions, commit policy), the secret scan (Vercroft's shared workflow), dependency-age (packages and images) and its daily re-run for Dependabot pull requests, links, CodeQL and Scorecard |
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
  App's client secret (`BURNBIRD_GITHUB_CLIENT_SECRET_FILE`), the OAuth state key (`BURNBIRD_OAUTH_STATE_KEY_FILE`,
  32 characters or more), the App's RSA private key in PEM (`BURNBIRD_GITHUB_APP_PRIVATE_KEY_FILE`), the email
  provider's bare send token (`BURNBIRD_ZOHO_API_KEY_FILE`) and, only where the email provider's webhook is served,
  its Authentication Key (`BURNBIRD_ZOHO_WEBHOOK_KEY_FILE`). The client ID
  (`BURNBIRD_GITHUB_CLIENT_ID`, also the App JWT's issuer) and the site's origin (`BURNBIRD_BASE_URL`,
  `https://<host>` with no path) come from the environment. Tests make random values and keys.
- Endpoints are plain `def`; `async def` only for a dependency that awaits the request body and makes no blocking call,
  and for `/auth/login` and `/auth/callback`, because Authlib's client is async; the callback's database work runs in
  the thread pool.
- `request.session` holds only Authlib's OAuth state, in the signed `__Host-burnbird_oauth` cookie: no code reads or
  writes it except Authlib and the callback, which clears it. The sign-in session is the `__Host-burnbird_session`
  cookie and the `session` table, which stores only its SHA-256.
- Access: everything a page shows about repositories (names and numbers alike) is filtered by `allowed_repositories`.
  GitHub lists an installation for anyone who can access one of its repositories, outside collaborators included, so
  `allowed_installations` alone never grants a view or a setting. Account-wide numbers, the forecast and settings need
  `account_access`: a personal account's own user, or, for an organisation, a user with admin on every repository the
  installation covers. Everyone else sees only their own repositories. The user's lists and admin flags come from
  their last sign-in, so they can be up to one session (8 hours) old; the installation's repository list counts only
  if the hourly reconciliation stored it within the last two hours. Access fails closed: an installation never synced
  or not synced recently, one with no repository, or a user it is not listed for gets none. A foreign ID answers 404.
- Every POST depends on `csrf_protected`: the `Origin` must be the site's origin exactly, and the form's `csrf_token`
  must be the session's (`csrf_token(request)`).
- No user access token is stored or logged; a sign-in uses it within its one request.
- The App's own calls go through `burnbird.github.client.GithubApp`: installation tokens ask for Actions and Metadata
  read only and live in its memory, never in a row, a log or an exception; calls are serial and paced, every call has
  a time limit, only a GET is retried, and a rate limit is never waited out in a call: it raises `RateLimitedError`
  and nothing more is sent in that scope until it resumes.
- Reconciliation deletes only what GitHub's full list no longer holds: the stored IDs are read before the list is
  fetched, every page is collected before anything is written, and a failure changes nothing. An installation is
  uninstalled only when GitHub answers 404 for it, a repository list that differs from its `total_count` removes
  nothing, and a listed installation is not stored while another one of its account is. The same rule holds for
  rows that change during the fetch, installations and repositories alike: one deleted, uninstalled or moved while
  its list was fetched is not stored again from that list. The mass-deletion guard
  in `burnbird.github.reconcile` stays: an empty installation list uninstalls nothing; more than `max(2, 10%)` of the
  stored installations to uninstall uninstalls none; an empty repository list while repositories are stored removes
  none. Each logs an ERROR, and the next run tries again. Loosening it is a reviewed decision of its own.
- The backfill (`burnbird.github.backfill`) stores a repository's jobs only through `events.store_job`, the upsert the
  webhook uses, and never brings back a repository removed or moved while it was backfilled: each run's jobs are
  stored only after the repository row is locked and found under the same installation, and every run listed must be
  that repository's. It keeps a tenth of each installation's requests for the hourly tasks, saves its progress after
  each range so a pass stops after a bounded number of runs, and an error puts a row off (doubling up to an hour),
  never drops it.
- The redelivery (`burnbird.github.redeliver`) asks GitHub for each failed delivery at most once, recorded before
  the request, and never for one our endpoint refused with 400 or 413. A job event that will not be sent again (refused
  with 400 or 413, a request that failed with anything but a rate limit, or a failed redelivery), and every
  repository after a gap whose window would reach past GitHub's 3 days, is re-queued for the backfill from the cycle
  start, once per cause; a re-queue raises the row's generation, so a pass already running (or one whose lease was
  taken over) never undoes it.
- Every code path that locks more than one repository goes through `retention.lock_repositories`, in ID order.
- Every time limit on stored data is a purge in `retention.run_purges`, which the worker's daily `purge` task runs in
  one transaction; a new limit is a new purge there, with a test of a row just past it and one just inside it.
- An email address never reaches a log line, an exception message or a test's committed text. Sign-in logs say only
  whether an address was read, and tests build invented addresses on `example.invalid` at run time.
- Every answer carries `Content-Security-Policy: default-src 'self'; script-src 'self'; frame-ancestors 'none';
  form-action 'self'; base-uri 'none'`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`
  (`burnbird.security_headers`); `tests/test_security_headers.py` checks them on a page, redirects and error answers.
  Templates hold no script, no inline style and no event handler attribute, and load nothing from another site;
  `tests/test_templates.py` checks every template.
- Every POST depends on `csrf_protected`, except the ones `tests/test_csrf_boundary.py` lists with their reason; a new
  exemption is a reviewed change there. The one-click unsubscribe POST (`/email/unsubscribe/{token}`) is one: RFC 8058
  says the mail client sends it with no cookie or other context, so the token in the path is its only proof. It
  answers 200 with no redirect, the same for an unknown token, and it can only turn a kind of email off; its GET
  only shows the button. A token not shaped like one gets 404 on both, so the page never writes a form address
  someone else chose. The email provider's webhook (`/email/webhook`) is another: it proves itself with the
  provider's HMAC over the raw body, checked before the body is parsed, and it exists only when its key is set.
- Consent rows (`email_consent`) are append-only: a choice is changed in `email_preference` and recorded as a new
  consent row, never by updating or deleting one, except when the user's data is deleted. A user's default choices
  are written once, when their first address is stored, and never reset. A changed `email/consent.txt` is a new
  version by itself.
- A session's device label is built from `burnbird.auth.device`'s own words and a major version only; the raw
  `User-Agent` and client-hint headers are never stored or logged.
- No tracked file holds an email or IP address that is not on `tests/test_public_text.py`'s allowlist. A new one is a
  reviewed change to the allowlist.
- No job from before Delete my data is ever stored again: the account's `data_deleted_at` is set in the same
  transaction, `events.store_job` (the one path for webhooks, redeliveries and backfills) skips any job created
  before it, and a backfill never starts before it.
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

# Invariants

How the code must behave, one rule per line, with the test that enforces it. Loosening a rule is a reviewed decision
of its own.

## Access

- Everything a page shows about repositories, names and numbers alike, is filtered by `allowed_repositories`;
  `allowed_installations` alone never grants a view or a setting (`tests/auth/test_sessions.py`).
- Account-wide numbers, the forecast and settings need `account_access`: a personal account's own user, or a user with
  admin on every repository of an organisation's installation (`tests/auth/test_account_access.py`).
- Access fails closed: an installation never synced, not synced in the last two hours or with no repository, or a user
  it does not list, gets none (`tests/auth/test_account_access.py`).
- A foreign ID answers 404 (`tests/auth/test_sessions.py::test_a_foreign_installation_is_refused`).

## Sign-in and sessions

- `request.session` holds only Authlib's OAuth state, in the signed `__Host-burnbird_oauth` cookie; only Authlib and
  the callback touch it, and the callback clears it (`tests/auth/test_flow.py::test_every_answer_clears_the_oauth_cookie`).
- The sign-in session is the `__Host-burnbird_session` cookie; the `session` table stores only its SHA-256
  (`tests/auth/test_sessions.py::test_the_hash_alone_is_not_a_session`).
- No user access token is stored or logged; a sign-in uses it within its one request
  (`tests/auth/test_flow.py::test_no_token_verifier_or_code_reaches_a_log_or_a_row`).
- A sign-in stores everything in one transaction: a failure stores nothing
  (`tests/auth/test_flow.py::test_a_failing_list_stores_nothing_and_keeps_the_old_lists`).
- A session's device label holds only `burnbird.auth.device`'s own words and a major version; the raw `User-Agent`
  and client hints are never stored or logged
  (`tests/auth/test_device.py::test_nothing_from_the_headers_but_our_own_words_and_digits_reaches_the_label`).

## Requests and pages

- Endpoints are plain `def`. `async def` is only for a dependency that awaits the body and makes no blocking call, and
  for `/auth/login` and `/auth/callback`, whose database work runs in the thread pool (a blocking standard-library
  call in an `async def`: ruff's `ASYNC` rules in `uv run --locked ruff check`; the rest checked in review, standard
  §4.3).
- Every POST depends on `csrf_protected`: the exact site `Origin` and the session's `csrf_token`
  (`tests/auth/test_csrf.py`).
- Only the POSTs listed with their reason in `tests/test_csrf_boundary.py` are exempt; a new exemption is a reviewed
  change there (`tests/test_csrf_boundary.py::test_every_post_but_the_exempt_ones_checks_csrf`).
- One-click unsubscribe is exempt: the token in the path is its only proof, it answers 200 the same for an unknown
  token, and it can only turn email off (`tests/email/test_unsubscribe.py`).
- The email provider's webhook is exempt: its HMAC over the raw body is checked before parsing, and the route exists
  only with its key (`tests/email/test_webhook.py`).
- Every answer carries the security headers of `burnbird.security_headers`, redirects and errors included
  (`tests/test_security_headers.py`).
- Templates hold no script, inline style or event handler, and load nothing from another site
  (`tests/test_templates.py`).

## GitHub

- The App's calls go through `burnbird.github.client.GithubApp`: Actions and Metadata read only, serial and paced, a
  time limit on each, only a GET retried (`tests/github/test_client.py`).
- Installation tokens live only in `GithubApp`'s memory: never in a row, a log or an exception
  (`tests/github/test_client.py`).
- A rate limit is never waited out in a call: it raises `RateLimitedError`, and that scope sends nothing until it
  resumes (`tests/github/test_client.py::test_nothing_is_sent_in_a_paused_scope_until_it_resumes`).
- Reconciliation deletes only what GitHub's full list no longer holds, reads every page before writing, and changes
  nothing on a failure (`tests/github/test_reconcile.py`).
- The mass-deletion guard in `burnbird.github.reconcile` stays as its docstring states
  (`tests/github/test_reconcile.py::test_more_uninstalls_than_the_limit_uninstall_none` and the two empty-list tests).
- The backfill stores jobs only through `events.store_job` and never brings back a repository removed or moved during
  it (`tests/github/test_backfill.py`).
- An error puts a backfill row off, doubling up to an hour, and never drops it
  (`tests/github/test_backfill.py::test_an_error_keeps_the_row_and_backs_off_up_to_an_hour`).
- The redelivery asks for each failed delivery at most once, and never for one we refused with 400 or 413
  (`tests/github/test_redeliver.py`).
- What stays lost is re-queued for the backfill once per cause (`tests/github/test_redeliver.py`).

## Stored data and deletion

- Every path that locks more than one repository goes through `retention.lock_repositories`, in ID order
  (`tests/test_lock_order.py`).
- Every time limit on stored data is a purge in `retention.run_purges`, tested with a row just past it and one just
  inside it (`tests/test_retention.py`).
- No job from before Delete my data is ever stored again
  (`tests/github/test_events.py::test_a_job_created_before_its_account_s_data_was_deleted_is_never_stored_again`,
  `tests/github/test_backfill.py::test_a_backfill_never_starts_before_the_account_s_data_was_deleted`).
- Consent rows are append-only: a choice changes `email_preference` and adds a consent row, never updating or deleting
  one, except when the user's data is deleted (`tests/email/test_preferences.py`).
- A user's default choices are written once, with their first address, and never reset
  (`tests/email/test_preferences.py::test_defaults_never_reset_a_choice_the_user_made`).
- A changed `email/consent.txt` is a new version by itself
  (`tests/email/test_consent.py::test_the_version_is_the_start_of_the_wording_s_sha256`).
- The email webhook and the warnings switch lock the user row first, then the address, then the choices
  (`tests/test_lock_order.py::test_a_complaint_and_the_switch_turned_on`,
  `tests/test_lock_order.py::test_a_complaint_about_an_old_address_and_delete_my_data`).
- A complaint about an email acts once: a replay changes nothing, even after the user turned warnings on again
  (`tests/email/test_webhook.py::test_a_replayed_complaint_after_the_user_turned_warnings_on_changes_nothing`).
- Turning warnings on with the switch lifts a complaint's pause, never a bounce's
  (`tests/account/test_you.py::test_turning_warnings_on_after_a_complaint_lifts_its_pause`,
  `tests/account/test_you.py::test_turning_warnings_on_never_lifts_a_bounce`).
- An email address never reaches a log line, an exception or a test's committed text
  (`tests/auth/test_email.py::test_the_address_never_reaches_a_log`, `tests/test_logs.py`).

## Configuration

- The site's origin (`BURNBIRD_BASE_URL`) is `https://<host>` with no path, and the client ID
  (`BURNBIRD_GITHUB_CLIENT_ID`) is also the App JWT's issuer; both come from the environment
  (`tests/auth/test_config.py`).
- The database URL comes only from the environment, never from a file. The tests use only `burnbird_test`, or
  `burnbird_test_gw<number>` made for one pytest-xdist worker and dropped after it (`tests/test_database_guard.py`).

## Repository

- No tracked file holds an email or IP address that is not on `tests/test_public_text.py`'s allowlist.
- A schema change is a new migration, never an edit to a merged one
  (`tests/test_migration.py::test_the_models_match_the_migrated_schema`).
- Every workflow image is pinned as `name:tag@sha256:<digest>`, written once and classified (`tests/test_images.py`).
- Every action is pinned by full commit SHA (zizmor's `unpinned-uses` audit, the zizmor job), with its release in a
  comment and at least 7 days old (checked in review, standard §4.3).
- Python packages are pinned exactly and locked; uv refuses versions under 7 days old and source builds
  (`uv lock --check`, the dependency-age job).
- A suppression names one rule with a reason and is listed in `.suppressions` (`scripts/check-suppressions.sh`).

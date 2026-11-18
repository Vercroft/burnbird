# Run the tests locally

The storage tests need a throwaway Postgres, and the email tests a local Mailpit. Without them those tests fail; they
never skip. Each image is pinned only in `.github/workflows/ci.yml`, so the commands below read it from there. No test
reaches a real mail server or the email provider: provider answers come from an httpx `MockTransport`.

## Docker Engine on Linux

Run both on the host network, listening on loopback only. The tests then reach them over loopback, so a VPN or a
firewall that blocks traffic to container addresses does not stop them.

1. Start Postgres:

   ```sh
   docker run --rm -d --name burnbird-test-postgres --network host -e POSTGRES_HOST_AUTH_METHOD=trust \
     -e POSTGRES_DB=burnbird_test "$(grep -oE 'postgres:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)" \
     -c listen_addresses=127.0.0.1 -c port=55432
   ```

2. Start Mailpit:

   ```sh
   docker run --rm -d --name burnbird-test-mailpit --network host \
     -e MP_SMTP_BIND_ADDR=127.0.0.1:1025 -e MP_UI_BIND_ADDR=127.0.0.1:8025 -e MP_DISABLE_VERSION_CHECK=true \
     -e MP_BLOCK_REMOTE_CSS_AND_FONTS=true -e MP_MAX_MESSAGE_SIZE=1 -e MP_MAX_MESSAGES=200 \
     "$(grep -oE 'axllent/mailpit:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)"
   ```

3. Name them for the tests:

   ```sh
   export BURNBIRD_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/burnbird_test
   export BURNBIRD_TEST_SMTP_URL=smtp://127.0.0.1:1025 BURNBIRD_TEST_MAILPIT_URL=http://localhost:8025
   ```

4. Run the tests: `uv run --locked pytest`. They run in parallel, each worker in its own database, which the tests
   make and drop.

5. Stop both when you are done:

   ```sh
   docker stop burnbird-test-postgres burnbird-test-mailpit
   ```

## Docker Desktop

Publish the ports on loopback instead of using the host network, then follow steps 3 to 5 above.

```sh
docker run --rm -d --name burnbird-test-postgres -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=burnbird_test \
  -p 127.0.0.1:55432:5432 "$(grep -oE 'postgres:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)"
docker run --rm -d --name burnbird-test-mailpit -e MP_DISABLE_VERSION_CHECK=true \
  -e MP_BLOCK_REMOTE_CSS_AND_FONTS=true -e MP_MAX_MESSAGE_SIZE=1 -e MP_MAX_MESSAGES=200 \
  -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 \
  "$(grep -oE 'axllent/mailpit:[^ ]+@sha256:[0-9a-f]{64}' .github/workflows/ci.yml)"
```

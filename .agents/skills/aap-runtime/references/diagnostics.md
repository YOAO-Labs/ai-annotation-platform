# Runtime diagnostics

## Stuck jobs

Trace the UI request through its endpoint, job row, dispatch, queue route, worker registration, and result/event update. Compare the API and worker database and Redis identities without exposing credentials. Inspect the relevant logs and consumers before retrying or changing job status.

Do not treat a cancelled database status as proof execution stopped. Check revoke/cooperative-cancellation behavior and side effects. Retry only within the requested scope after resolving the cause.

## Stored UI reports

`BugReportDrawer` writes PostgreSQL `bug_reports`. When an API token is unavailable, these read-only examples work with the repository's default local credentials; adjust user/database and Compose selection to the actual stack:

```bash
docker compose exec -T postgres psql -U user -d annotation -c \
  "SELECT display_id, title, severity, status, created_at FROM bug_reports ORDER BY created_at DESC LIMIT 20;"
docker compose exec -T postgres psql -U user -d annotation -c \
  "SELECT display_id, title, description, severity, status, route, browser_ua, recent_api_calls, recent_console_errors FROM bug_reports WHERE display_id = 'B-1';"
```

Use the requested report ID and correlate its route, request failures, and console evidence with a current reproduction. Historical reports can describe an older build.

## Database-backed verification

`apps/api/tests/conftest.py` derives the default `annotation_test` connection from the configured migration connection and permits `TEST_DATABASE_URL` override. Verify the effective target is disposable before tests run migrations or write rows. Keep the existing isolation/role safeguards.

For fixture-created rows that the API must see, use the transaction-bound `httpx_client` fixture. The former `httpx_client_bound` alias has been removed. Tests that intentionally need an independent transaction or cross-connection visibility must follow the explicit connection patterns in `test_worker_signals.py` and `test_discussion_notifications_commit.py`; per-test SAVEPOINT rollback and process-cache cleanup are different concerns.

An old test database can be at Alembic head yet lack the current month's `audit_logs` partition. If annotation writes fail locally while fresh CI passes, inspect the database error and partition coverage before changing the frontend. Initial migration `0037_audit_logs_partition.py` creates a finite range; ongoing maintenance belongs to `app/workers/audit_partition.py`. Use the partition service in the verified test environment when repair is required.

After raw-SQL migration updates, `expire_all()` can leave async ORM objects triggering implicit I/O on later attribute access and raise `MissingGreenlet`. Reload the affected rows explicitly; `apps/api/tests/test_migration_0118_compact_track_identity.py` demonstrates a targeted SELECT with `populate_existing=True`. Verify such migration handoffs against PostgreSQL in the integrated environment.

Screenshot tests have their own API, worker, database, and Redis isolation; follow [media capture](../../aap-doc-media/references/capture.md). Restarting or rebuilding never substitutes for an Alembic migration.

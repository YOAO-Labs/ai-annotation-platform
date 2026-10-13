# Worktree runtime isolation

## Select the exact environment

Run `pnpm dev:worktree -- doctor --mode dev` from the intended checkout before acting. The runtime supports `dev`, `test`, and `e2e`; they do not share databases, Redis, buckets or local files. `exec` defaults to `test`; other commands default to `dev`. Only one active management/execution session may own a mode.

Preserve `.worktree/identity.json` and its checkout binding. Do not copy or symlink `.worktree`, invent ownership tags, or adopt a resource solely because its name has the expected prefix. Do not print credentials or dump the child environment.

The local launcher shares only the PostgreSQL/MinIO servers. It requires local development endpoints and a migration account able to create/manage owned databases. Do not use it for staging, production, remote infrastructure or account-level security isolation.

## Start and verify

Use `pnpm dev:worktree` for API/Web and add `up --with-worker` for two current-checkout host workers: ordinary general/CPU queues and the owner-capable `maintenance` queue. GPU and beat are not started. Both workers must register their tasks and exact queue subscriptions before startup succeeds; doctor reports both, and either worker exiting stops the environment. Do not restart the primary Compose workers to refresh this environment. Check registrations and exercise a relevant task after worker changes.

The Node supervisor reserves API/Web ports and verifies HTTP readiness. The Python owner validates resource ownership and migration history before the supervisor runs. Unknown revisions, duplicate IDs or multiple heads are blockers, not reasons to stamp or downgrade a shared database. `--skip-migrations` requires the database already to be at this checkout's head.

Source manifests matching the primary checkout are insufficient to share Node dependencies: the installed pnpm lockfile must match too. If sharing is rejected, inspect and detach only the current checkout's links, then install its frozen lockfile. Never install through a shared link.

## Tests and rebuilds

Use `pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest <tests>'`. API pytest otherwise defaults to `annotation_test`, even when the development database name is isolated. For Playwright use `exec --mode e2e -- pnpm test:e2e`; the wrapper pins its database override and reserves separate service ports.

Keep the generated `AAP_WORKTREE_MODE` marker intact. Test entry points reject `dev` before database preparation, and dev processes intentionally receive unusable test DSNs rather than aliases into another mode. API/ordinary workers exclude the migration/test owner credentials. The maintenance worker receives the same database's owner connection as `DATABASE_URL`, with migration/test DSN variables cleared; frontend processes receive only public/system configuration.

Redis logical databases do not isolate Pub/Sub. Keep the dedicated Redis instance and all Celery broker/read/write/result overrides together. Redis AOF and DuckDB/temp files belong to the selected mode. Override all eight storage buckets, because cleanup and lifecycle settings are bucket-scoped.

`Ctrl+C` stops the launched processes but retains resources. `stop` also stops the owned Redis container while preserving AOF and business data. Wait for important active tasks before stopping; arbitrary in-flight tasks are not guaranteed resumable.

Reset/destroy require the exact `resources.confirmation` reported by doctor. Verify the mode is disposable, stop its clients, and retain the ownership checks. Never force-terminate unknown PostgreSQL connections or run broad Docker/MinIO cleanup. Destroy used modes before deleting or moving the worktree; Git/Orca removal does not own database cleanup.

After successful destruction, the mode's infrastructure manifest is removed but identity and lock files remain. A partially failed destruction retains the target manifest. Never remove that manifest early just to retarget a different server.

## Regression checks

- `node --test scripts/dev-worktree.test.mjs`
- `apps/api/.venv/bin/python -m unittest discover -s scripts -p 'test_worktree*.py' -v`
- `apps/api/.venv/bin/python scripts/test-orca-worktree-setup.py`
- `apps/api/.venv/bin/python scripts/verify_worktree_isolation.py -v` creates/deletes random owned resources on validated local infrastructure. This is a live, state-changing acceptance test, not a read-only probe.
- `apps/api/.venv/bin/python scripts/verify_worktree_maintenance.py` uses disposable PostgreSQL with separate roles plus owned Redis/buckets to exercise all five maintenance tasks and worker shutdown. It does not alter the shared PostgreSQL roles or data.

The user-facing source is `docs-site/dev/how-to/worktree-environments.md`. Keep that page and `DEV.md` aligned with the actual CLI, and distinguish local acceptance results from remote CI.

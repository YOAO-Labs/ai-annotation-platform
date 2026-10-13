# Worktree runtime isolation implementation plan

**Goal:** Make `pnpm dev:worktree` provision and supervise an independently diagnosable and rebuildable local development environment without changing the shared database or replacing Alembic.

**Architecture:** Keep Orca's existing setup script and Node API/Web supervisor. Add a Python runtime owner using the existing API dependencies. Persist a credential-free worktree identity; derive separate dev/test/e2e resources. Share only the local PostgreSQL and MinIO servers, with owned databases and buckets; use a labeled Redis container and checkout-local files per environment. Scope workers to the same checkout and environment. All destructive actions require exact confirmation and verified resource ownership.

**Tech stack:** Node built-in test runner/process helpers; Python stdlib, SQLAlchemy/asyncpg, Alembic, boto3 and redis from the local API environment; Docker CLI.

**Base:** `origin/main` at `8ef762fb293cbc3b004c03bd615b9afb8e70b1a3`; branch `fix/worktree-isolation`. Preserve the unrelated original worktree. The user subsequently authorized committing and pushing the verified change. Shared-service restarts and browser operations remain outside this task.

## Acceptance criteria

- The normal worktree launcher never silently targets the shared `annotation` / `annotation_test` databases.
- API, migration, tests and optional workers receive a consistent environment; inherited broker/bucket/path settings cannot redirect isolated writes.
- Development, test and E2E environments have distinct database, Redis and storage identities. Concurrent use of the same managed environment fails clearly rather than sharing a destructive test run.
- Credential-free diagnostics report checkout, resources, revisions, running processes and connectivity. Unknown revisions and multiple migration heads fail before applying migrations, even when migrations are skipped.
- Stop preserves business data. Reset/destroy require exact environment confirmation, reject foreign resources and live sessions, and do not force-terminate unknown database clients.
- Optional workers run the current checkout and prove readiness; GPU workers/control and beat remain opt-in or outside this local default.
- Real acceptance uses only newly provisioned disposable resources. Prove two environments have independent rows, Redis keys/pubsub, objects and cleanup, and exercise real API/Web startup and a worker in the intended environment.

## Task 1: Persistent identity and topology

Files: new `scripts/worktree_env.py`, `scripts/test_worktree_env.py`; update `.gitignore`.

1. Add a unittest for persistent, checkout-bound identity and disjoint resource names. Run it red.
2. Implement strict identity validation, atomic persistence and local path checks. Run green.
3. Add tests for mode selection, URL target matching, inherited environment overrides, credentials not persisted, and symlink/foreign-state refusal. Implement one behavior at a time.

Verification: `apps/api/.venv/bin/python -m unittest discover -s scripts -p 'test_worktree_env.py' -v`.

## Task 2: Resource lifecycle and migration preflight

Files: new `scripts/worktree_runtime.py`, `scripts/test_worktree_runtime.py`.

1. Add failing tests for revision validation and ownership-gated destruction; implement pure guards.
2. Implement local-only PostgreSQL provisioning with database ownership comments, individually tagged MinIO buckets, labeled Redis lifecycle and bounded readiness.
3. Implement `init`, `doctor`, `reset`, `destroy`, and `exec -- <command>` using the same topology. Keep credentials in process memory/environment only.
4. Reject active/unowned resources, malformed identity, unknown revisions and multi-head histories. Verify state-changing operations by readback.

Verification: isolated unittest discovery outside API `conftest.py`, then integration against disposable databases and buckets only.

## Task 3: Launcher and worker supervision

Files: `scripts/dev-worktree.mjs`, `scripts/dev-worktree.test.mjs`, runtime helper and tests; existing `scripts/orca-worktree-setup.sh` if integration requires it.

1. Keep port reservation tests and add a failing argument/dispatch regression before modifying the entry point.
2. Delegate the public CLI to the runtime owner; retain the Node supervisor for API/Web subprocesses after safe environment injection.
3. Add environment locking, identity-checked stop, worker opt-in and bounded health checks; reject unsafe migration skipping.
4. Preserve setup's dependency-link safeguards and avoid auto-starting infrastructure during checkout creation.

Verification: `node --test scripts/dev-worktree.test.mjs`; `apps/api/.venv/bin/python scripts/test-orca-worktree-setup.py`.

## Task 4: Documentation and continuous checks

Files: `DEV.md`, `README.md`, `CHANGELOG.md`, `docs-site/dev/concepts/runtime-environments.md`, `.agents/skills/aap-runtime/SKILL.md`, runtime reference, `.github/workflows/ci.yml`. Document new configuration in `.env.example` and regenerate environment reference if new environment variables are introduced.

Describe prerequisites, first-run data, modes, worker behavior, diagnostics, confirmed reset, resource cleanup, concurrency constraints and migration history policy. Add fast runtime tests to CI using existing dependencies. Do not assign a release version.

## Task 5: Real acceptance and review

1. Record read-only shared database revision before validation.
2. Provision two owned disposable environments and prove database, Redis Pub/Sub and MinIO isolation.
3. Start API/Web with the actual launcher; verify HTTP readiness and optional worker registration/dispatch.
4. Exercise stop/restart/reset and confirm another environment plus the shared DB remain unchanged. Clean only task-owned disposable resources.
5. Run relevant lint/format/tests, document actual evidence and limitations, inspect the final diff and run `git diff --check`.

## Outcome

- Implemented the owned local runtime, lifecycle commands, per-mode test isolation, public-only frontend environment, current-checkout workers, migration preflight and installed-lock dependency sharing guard.
- Developer documentation: `DEV.md`, `README.md`, `docs-site/dev/how-to/worktree-environments.md`, `docs-site/dev/concepts/runtime-environments.md`, local-development/deployment pages and the generated environment reference. Runtime procedures are recorded in `.agents/skills/aap-runtime/references/worktree-isolation.md`.
- CHANGELOG: Unreleased entry added; no release milestone or version assigned.
- Local verification: 25 Python runtime regression tests; 8 Node launcher tests; Orca/Codex setup regression checks; 4 API database/configuration tests through isolated `exec`; frozen frontend build and documentation build; lint, formatting and diff checks.
- Live verification: two isolated API/Web environments at independent ports and databases; a current-checkout worker completed task `e0ae0965-2b2a-4de4-a17c-76650bdeb795`; shared `annotation` stayed at `0167` while isolated databases used `0165`. Random-resource acceptance covers database, Redis keys/PubSub/persistence, objects, destruction and rebuilding. Managed Playwright collected 248 tests in 55 files; no browser/E2E test execution is claimed.
- Independent review identified process identity, test-target aliasing, manifest retention and unnecessary credential propagation; regression tests and fixes were added. Follow-up review cleared those findings. Its remaining non-blocking child-registration edge was then fixed with an insert/replace regression and fail-closed launch guard; a final worker dispatch (`557df59a-e19e-4a2b-92d5-9308b7d8af1a`) succeeded. The verified change is ready for the user-authorized commit/push; remote CI is not claimed.

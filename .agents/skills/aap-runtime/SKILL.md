---
name: aap-runtime
description: Set up this project's worktree environment or diagnose and refresh its API/Celery runtime after source, dependency, configuration, or migration changes.
---

# Project runtime and worktrees

Determine which checkout, Compose project, process, and database serve the reported behavior. A worktree's files do not prove a running service uses that checkout. Inspect service names, mounts, working directories, ports, and queue consumers without printing secrets.

## Primary checkout

The main development API/Web may be owned by the user services `aap-development-api.service` and `aap-development-web.service`, while eight Celery worker/beat containers belong to the primary Compose project. Read the current primary-checkout section in `DEV.md`, inspect PID/cwd and Compose labels, and use the installed `ai-annotation-platform-dev-restart` wrapper when configured. Its source is `scripts/development-restart.py`; it validates ownership and readiness before restarting. Do not kill an unknown process merely because it owns port 3000 or 8000, and do not use `dev:worktree` to manage the primary checkout.

Changes under `apps/_shared`, backend dependency manifests, Dockerfiles, or copied build inputs can require rebuilding each running ML backend that consumes them. Determine the affected, currently enabled services from Compose inputs; preserve model/checkpoint volumes, recreate only those services, and verify their actual health/setup endpoints afterward. A platform API restart does not reload ML backend images.

## Worktree setup

Use `orca.yaml` and `scripts/orca-worktree-setup.sh`, not a second bootstrap recipe. Orca supplies `ORCA_ROOT_PATH` and `ORCA_WORKTREE_PATH`; the script refuses the primary checkout. Use the available Orca CLI guide when changing managed worktree state.

The script shares `.env` and optional `.env.local`. It shares the three Node dependency directories only when manifests/lockfiles match the primary checkout and its installed `node_modules/.pnpm/lock.yaml` matches that lockfile. Source manifests alone do not prove the installed graph is current. Before dependency changes, inspect and detach only the intended dependency symlinks; never install through a shared link into another checkout. Preserve existing files and dangling links.

`apps/api/.venv` and `apps/web/src/api/generated` must remain local. Setup runs locked API test-dependency sync and `pnpm codegen`. Missing generated types after a branch switch can be stale ignored output: check the snapshot and regenerate before changing consumers.

A setup fix must exist in the base commit used for future worktrees. Recheck that base's manifests and lockfile; a successful install in one feature branch does not fix the primary branch. For setup-script changes, use `scripts/test-orca-worktree-setup.py`.

## Runtime refresh

For concurrent local checkouts, use `pnpm dev:worktree`, not raw `dev:api` or a second default Compose stack. Read [worktree isolation](references/worktree-isolation.md) before provisioning, testing, stopping, or rebuilding a managed environment. The launcher owns databases, Redis containers, buckets and files; setup still only prepares dependencies and generated types.

| Changed input                                      | Development action                                                    |
| -------------------------------------------------- | --------------------------------------------------------------------- |
| Host API Python under reload                       | Confirm the reloader applied it                                       |
| Vite source                                        | Confirm HMR and the actual served checkout                            |
| Mounted Celery code                                | Restart affected running workers; beat when its code/schedule changes |
| Container environment                              | Recreate affected services; restart affected host processes           |
| Migration                                          | Apply Alembic in the intended database environment                    |
| Dependencies, image build inputs, or copied source | Rebuild and recreate affected services                                |

Check `docker-compose.yml` and `apps/api/app/workers/celery_app.py` for queue routing. Worker variants include default, maintenance, GPU, CPU, export, image-pyramid, and GPU-control. The maintenance consumer needs privileges for partition DDL and materialized-view refresh; keep ordinary workers on the runtime connection. Shared task code may affect multiple consumers. Mounted `/app` source has an anonymous `/app/.venv` volume masking the host environment.

For a changed task signature, verify the callable inside each affected running container and exercise the relevant dispatch. An unexpected-keyword `TypeError` can mean stale worker code. A healthy API alone does not establish worker readiness.

For long checks, retain the real exit code plus JUnit/JSON or another bounded summary before output can be truncated. Do not repeat a completed suite only to recover console counts. After verification, remove only task-owned logs, databases, Redis instances, buckets, browser profiles, and probes; destroy worktree modes before deleting their worktrees.

Read [diagnostics](references/diagnostics.md) for stuck jobs, stored bug reports, or database-backed checks. Finish with evidence from the affected runtime, or name the unavailable service precisely.

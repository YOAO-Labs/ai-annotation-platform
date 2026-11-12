# Failed acceptance environments: retention and retirement

Read this before retaining a failed acceptance environment, when host capacity (disk, memory, Docker networks, PIDs) is low, or when retiring disposable test environments that the normal destroy path refuses.

## What to retain

Failure evidence is the retained artifact; a live environment is not. Always keep the original report, JUnit/JSON, owner receipts and journals, bounded logs, and the exact resource inventory. These are small and outlive the environment.

Keep a failed live environment (containers, databases, buckets, host owner processes) only while all of these hold:

- the failure cause is still unknown and someone is actively inspecting it in that environment;
- the inspection needs live state that the retained evidence does not already contain;
- it is younger than 72 hours.

Once the cause is recorded, a fix candidate exists, or a later run of the same scenario passes, the live environment has no further consumer. Retire it. New qualification runs create fresh environments; never reuse an old owner journal, host or worktree to continue a different run.

Before starting heavy runs (module-data qualification, canonical Raster, cold CLI, browser suites), check free disk, memory and Docker network count, and retire stale task-owned environments first. Recurring `507 XMinioStorageFull`, Docker "address pools have been fully subnetted" or exhausted private network pools mean retained environments are accumulating; recover capacity by retiring them, not by deleting caches only, raising timeouts or retrying.

## Normal retirement

Use the owning tool first: `scripts/worktree_runtime.py destroy --mode <mode> --confirm <id>:<mode>` for worktree modes, the verifier's own cleanup for module-data runs, and the fixture owner's retirement for fixture stores. These paths verify ownership and settle native work. Destroy worktree modes before removing their worktrees.

## Whole-instance retirement of disposable environments

When product guards correctly refuse per-object cleanup (unsettled admission, unknown ACK, unretired native work) inside a disposable test instance, the instance as a whole may still be retired, because nothing outside it depends on its state. This requires explicit user authorization for the batch and must not be reported as a product cleanup pass.

1. Inventory every container, volume, network, host process, database and `/tmp` root with its owner label: `org.aap.acceptance` / `org.aap.acceptance-id`, `org.aap.worktree-owner` (checkout hash = first 16 hex of SHA-256 of the checkout path), `aap.e07.owner` / `aap.e07.checkout`, Compose project. Classify by exact label, never by name prefix alone. Exclude daily, production and registry stacks, user-named volumes, other branches' worktree environments and anything protected by the active plan.
2. Confirm no agent or test process is still working against the targets (recent session activity, process CPU, uncommitted changes in the owning worktree).
3. Stop the instance's host processes (Native owners, Celery, fixture backends, Vite/Playwright) with SIGTERM, then SIGKILL after a bounded wait.
4. Export minimal evidence while containers still run: `pg_dump -Fc` per database, an object listing per MinIO store, a key listing per Redis, plus an archive of the `/tmp` roots and worktree `.worktree/` state. Store it under the plan's untracked evidence directory with restrictive permissions; it contains credentials.
5. Remove containers with their anonymous volumes, then named volumes no longer attached, then empty networks with the same owner labels. Worktree-mode databases and roles that live in a shared cluster are dropped by exact name only after their dumps exist; `DROP ROLE` must succeed without `CASCADE` (a failure means the role holds rights outside the retired databases).
6. Remove the owning worktrees through Orca, keeping branches. Use `--force` only when Git refuses because of a submodule, after checking that the submodule is clean, has no stash and its HEAD is contained in a remote branch.
7. Write a receipt (kept vs retired, counts, before/after capacity) next to the evidence and add one line to the plan status pointing at it.

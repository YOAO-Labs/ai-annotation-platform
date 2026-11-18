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

If product guards refuse per-object cleanup inside an exclusively owned disposable instance, the whole instance may still be retired. An explicit instruction to delete this task's unused environments authorizes subsequent instances within that scope; do not request approval again for each inventory. Verify ownership locally before deletion. Leave shared, protected or ambiguous resources untouched. Instance removal is not a product cleanup pass.

1. Inventory every container, volume, network, host process, database and `/tmp` root with its owner label: `org.aap.acceptance` / `org.aap.acceptance-id`, `org.aap.worktree-owner` (checkout hash = first 16 hex of SHA-256 of the checkout path), `aap.e07.owner` / `aap.e07.checkout`, Compose project. Classify by exact label, never by name prefix alone. Exclude daily, production and registry stacks, user-named volumes, other branches' worktree environments and anything protected by the active plan.
2. Confirm no agent or test process is still working against the targets (recent session activity, process CPU, uncommitted changes in the owning worktree).
3. Stop the instance's host processes (Native owners, Celery, fixture backends, Vite/Playwright) with SIGTERM, then SIGKILL after a bounded wait.
4. Keep the failure report and the smallest evidence needed to diagnose it. Do not dump an exclusively owned database or archive entire runtime roots for hypothetical future review. Export a dump, object inventory or other specific state only when a requested restore check, an active diagnosis or a retention requirement needs it. Keep sensitive evidence in a private directory.
5. Remove containers with their anonymous volumes, then named volumes no longer attached, then empty networks with the same owner labels. Drop worktree-mode databases and roles in a shared cluster only by verified exact name after collecting any required evidence; `DROP ROLE` must succeed without `CASCADE` (a failure means the role holds rights outside the retired databases).
6. Remove the owning worktrees through Orca, keeping branches. Use `--force` only when Git refuses because of a submodule, after checking that the submodule is clean, has no stash and its HEAD is contained in a remote branch.
7. Write a receipt (kept vs retired, counts, before/after capacity) next to the evidence and add one line to the plan status pointing at it.

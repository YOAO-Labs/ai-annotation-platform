# 0112 — Durable Mask deletion fences and terminal receipts

- Status: Accepted; business consumer bound, isolated deployment qualification
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: The deferred GC protocol design in ADR-0111; the byte contract remains unchanged

## Context

A request-local SQL lock cannot protect a new Mask reference after an HTTP
timeout if the remote deletion is still running. Same-content uploads preserve
their ETag. A missing object observed later cannot prove an earlier deletion
has finished. The old bounded eligible prefix also prevents fair progress when
its deletions keep failing.

## Decision

Labeling owns durable per-key epochs, deletion intents, producer grace, roots
and a sweep cursor in migration 0229. The current ten closed JSON operations use
`aap-labeling-mask-gc/v1`. The cleanup login receives only six GC operations;
four producer gates remain part of the business writer's transaction. Native
triggers backstop every retained Mask root, including tracker snapshots and
review scopes. The byte owner never reads business tables.

Preflight captures the epoch before reading bytes. Final registration checks
that exact epoch under the content lock and registers a transaction-bound,
authenticated permit. Collection increments the epoch and holds a durable
fence through every uncertain outcome. Completion clears the active operation
without resetting its epoch, so old verified content cannot become a new root.
Producers renew persisted grace before object I/O; read and final checks do not
renew it.

The cleanup byte process adds `aap-assets-mask-gc/v1` scan/execute/status
operations. An immutable conditional request object claims a stable operation
ID once. Only that claimant can issue the single conditional payload deletion;
matching replays read the receipt and never delete again. The SDK makes one
attempt with explicit deadlines. No receipt means indeterminate, even when
HEAD would show missing or changed content. A successful terminal receipt is
immutable and survives restart. Journal objects live in
`raster-mask-gc/v1/`, with narrowly scoped Get/Put and no Delete permission.
They must not expire independently of the replay guarantee.

Only an exact terminal receipt under the current native claim can release the
fence. Lease expiry, a cleared token, old generations, transport errors and
HEAD absence are insufficient. Scan pages advance independently of deletion
results, while unresolved intents have a later recheck time. Page cursor and
replay receipt are updated atomically.

`apps/labeling-mask-gc` contains the portable contract, coordinator and client,
plus a separate runtime using function-only PostgreSQL and cleanup HTTP. It
has no application, Celery, Redis or S3 imports. The Celery consumer now selects
this owner through explicit HTTP configuration or embeds the same coordinator.
Each native operation uses a fresh transaction in the current task loop; domain
expiration commits and metric reads release their transaction before any RPC.
Configured remote failures do not fall back to local SQL or S3. This is no
production or Go cutover.

Migration 0234 adds one read-only page-inspection function and no storage. The
owner applies its own clock and fail-closed live-root rules without creating a
sweep, intent, epoch, permit or cursor. A preview returns its physical cursor and
global distinct live-root count; it does not grant deletion rights. Normal
staging remains authoritative for eligibility. Each cycle has independent scan
and delete budgets; pending delete reconciliation continues after scan failure.
The standalone service can disable background polling while preserving manual
HTTP and health routes. Its preview is fully observational, while the legacy
Celery dry-run still commits the separate domain-expiration phase.

## Consequences

The native authority and byte journal permit independent execution and safe
reconciliation after lost replies. They add persistent epochs and journals;
uncertain operations can hold a key indefinitely until definitive evidence is
available. Releasing them by elapsed time would lose the safety guarantee.

Local tests cover native privileges, stale claims and epochs, fair progress
past a failed first thousand keys, actual S3 reply loss, late HTTP completion
and byte-process restart. Installed processes, a Node consumer, both nonroot
read-only images and a bounded local sample are qualified. The sample preserves
1001 referenced objects and deletes seven orphans with a three-operation budget.
These results do not qualify production capacity or complete Labeling deployment.
Durable batch and retry checkpoints retain their separate execution, source and
terminal-receipt gates.

Cold migration qualification found that a bare `to_jsonb(source)` backfill can
serialize an existing `source` column instead of the row. The unreleased 0229
artifact now uses explicit whole-row references for every branch, covering
Annotation, Prediction and QC issue roots as well as timezone-stable identities.
The primary checkout remains before this migration. Once roots, epochs, intents
or sweeps exist, downgrade refuses to remove the native safety state. Reverting
the additive preview function leaves those facts intact; recovery rolls forward
and reinstalls exact owner/runtime grants instead of clearing state.

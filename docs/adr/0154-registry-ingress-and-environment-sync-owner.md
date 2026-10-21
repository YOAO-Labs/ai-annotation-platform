# 0154 — Registry Ingress and Environment Synchronization Owner

- Status: Proposed; scoped local qualification passed
- Date: 2026-10-22
- Deciders: Maintainers
- Supersedes: None

## Context

Startup environment synchronization and screenshot seeding wrote Registry rows and
created singleton pools directly through the API ORM. Extracting ordinary health
data did not close those writers. Moving only their probes to another process
would leave both policy and persistence in the original application.

The two entry points also have different transaction owners. Startup synchronizes
the entire configured URL list atomically. Seed discovery participates in the
caller's unit of work alongside project selection and bindings. A remote seed
commit would break that existing composition.

## Decision

Move the environment and seed ingress policy to `aap_inference.registry_ingress`.
Preserve HTTP gather, clock, capability extraction, manual Registry precedence,
failed-probe field preservation and singleton creation order. Empty environment
configuration still disconnects all environment-managed rows without deleting
provenance. New singleton pools remain disabled and use the pre-probe capability
snapshot; ingress does not introduce health reconciliation or pool enablement.

Publish eleven fixed Native functions in `aap_inference_registry_ingress_v1`.
Their owner holds the required Registry/Pool/Member privileges. The dedicated
`inference_registry_ingress_client` LOGIN can execute only those functions and
cannot read or write private tables, create objects or assume another role.
Full Registry and Pool reads retain their existing materialization behavior.
Observation writes lock and compare the endpoint/authentication/GPU/source
snapshot before applying their restricted field set. A changed observation
fails rather than overwriting a different endpoint's state.

The API adapter maps the same ORM identities and defers ingress observations to
the original flush. Unrelated caller work, subsequent field edits and rollback
remain in the original Session. Seed markers merge only their owned key;
unchanged markers and already-disconnected rows preserve their timestamps.
The portable asyncpg adapter borrows an active transaction, binds its original
XID and never commits, rolls back or closes the caller's connection.

Native calls retain the existing invoker GPU configuration trigger. Its relation
lookup is pinned to `pg_catalog, public, pg_temp`; `public` creation is revoked
from untrusted roles. The Inference owner receives only the fence read/lock and
membership update privileges that this existing trigger requires. Managed GPU
mutation rejection and membership epochs remain active.

Startup may select the embedded binding, a dedicated function-only DSN, or an
authenticated HTTP owner. The independent owner performs the actual probes and
opens its single database transaction after the HTTP gather and clock read.
The command accepts trusted operator URL configuration and capability limits;
it never accepts caller-provided health or setup results. Explicit selections
fail closed and cannot fall back to a local writer.

`/v1/registry/env-sync` and `/v1/registry/env-sync-status` share a dedicated bearer
that must differ from every other configured Inference capability. Strict JSON
envelopes reject unknown fields, duplicate keys, nonfinite numbers and bodies
larger than 24 MiB. URL strings are nonempty; the list itself may be empty.

Private receipts contain only request ID, canonical command hash, observed count,
creating XID and timestamp. The Native result is `caller_pending` within its
creating transaction and `committed` in a later transaction. A transaction-level
advisory lock serializes environment sync and receipt lookup. Reusing an ID with
a different hash is rejected. After an ambiguous response the client queries
status once and never resends the write; `unknown` does not prove rollback.

## Consequences

### Positive

- Both actual ingress callers consume the shared policy and fixed data boundary.
- The complete startup synchronization command can run in an independent process.
- Seed ingress preserves the existing caller transaction and project workflow.
- Credentials and raw probe results are absent from acknowledgements and receipts.

### Negative

- Endpoint snapshot fencing intentionally rejects observations made stale by
  concurrent configuration changes.
- A remote owner adds transport, credentials and failure recovery to startup.
- The atomic Pool+Member function runs after the caller's pre-probe flush.
  With autoflush disabled, SQL insertion order within that transaction differs
  from the old ORM interleaving; observations and commit/rollback boundaries do
  not move across backend I/O.
- Native errors can precede ORM failed-flush cleanup. Pending in-memory objects
  and internal Session flags after failure are not an ABI guarantee; the caller
  must still roll back or close its original transaction before reuse.
- Seed project selection/bindings remain Workspace consumers. Full Registry/Pool
  control, complete health remote coordination, stats and GPU consumers remain open.
- This does not solve paused route admission, Redis continuity, uncertain backend
  requests, long-running leases, project-disable races or unload fencing.

## Notes

Local qualification passed 64 distinct selected checks, including both actual
callers, three startup bindings, real restricted LOGIN transactions, atomic
failed-batch rollback, later caller edits, concurrent untouched JSON keys, original XID
fences and nonempty restore. The two immutable legacy corpora contain 54 actual
HTTP/database observations; their comparison explicitly accounts for the internal
Native macro adaptations above. The other six frozen corpora remain unchanged.

Source Inference/shared checks passed 265 tests. Fresh noneditable Inference and
Workspace installations passed 265 and 582 tests, with API/ORM/Celery absent.
Three source-bound images passed nonroot, readonly, no-network cold checks;
the API image also matched 1,258 API/Native inputs, OpenAPI and migration head.
Actual startup used the restricted Registry image, survived restart and produced
18 committed receipts. A 16-request/concurrency-four local sample had no failures;
API, database and controlled backend were outside the image's CPU/memory budget.
Stopping the provider failed closed without a borrowed-writer fallback.

Local evidence must distinguish frozen legacy observations, source/installed
checks, real restricted LOGIN transactions, migrations/restore and actual process
or container execution. Those checks do not establish whole-module independence,
another-language replacement, production capacity, real GPU or AMD64 qualification.
The Go provider remains an experiment and is not a production requirement.

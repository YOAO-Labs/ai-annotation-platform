# Jobs owner contracts

## Borrowed lifecycle writes

`JobsLifecycleManager` owns the existing generic lifecycle rules. The API keeps
its ORM identities and caller-owned transaction. Creation retains the original
full flush and compiled INSERT/defaults/RETURNING through the fixed
`aap_jobs_v1.async_job_creation` relation; all dirty Job rows use one native
`apply_lifecycle` update at their original mapper flush position. The net column
mask preserves UPDATE OF triggers, partial flush and default timestamp expiry.
Ordered commands retain partial writes when a later Python conversion raises.
Payload and result remain producer projections with native JSON values.
Without private table INSERT, creation is pending-only; callers that need a
terminal row must create it pending, then apply the terminal fields in the same
unit of work.

The native contract also provides fixed row/execution-ID reads, video-summary
insertion, pending-only dispatch failure, batch cancellation/deletion and
terminal retention deletion. Bulk operations use actual affected IDs to retain
the pinned SQLAlchemy fetch synchronization and rollback behavior; they do not
commit. Domain execution leases and business authorization stay with their
existing owners.

The strict `jobs-lifecycle-grants.sql` profile permits only this creation
relation and eight public functions. Ordinary LAN and worktree runtime profiles
revoke private table and column INSERT from the runtime and PUBLIC, and reject
inherited INSERT or Jobs creation ownership. They keep SELECT/INSERT on the
creation view and the existing video-summary function. Migration/test owners
may still use legacy initializers with their own private INSERT right.

Legacy column edits and explicit timestamps require the corresponding private
UPDATE plus primary-key SELECT. These checks resolve the actual PostgreSQL
invoker, never a client JSON field. The API retains SELECT for readers not yet migrated. Ordinary runtime profiles
revoke private UPDATE (table and all current columns) and DELETE after broad
grants, and reject inherited rights. Legacy column edits and timestamp overrides
remain denied for ordinary roles; schema-owner fixture tools keep their own
private rights. Production commands and payload/result projections retain the
existing owner path. Closing write rights does not complete Jobs read credential
closure or runtime recovery.

## Test project fixture cleanup

`aap-jobs-fixture-cleanup/v1` publishes the fixed
`aap_jobs_v1.delete_fixture_project_jobs(uuid[])` capability. It deletes exactly
current `project_id = ANY(project_ids)` rows of every kind and status, returns a
nonnegative native bigint, preserves retained/unrelated rows, and borrows the
caller transaction. Empty scope returns zero; duplicate IDs count each row once.
The function is granted to ordinary runtime roles only in actual databases whose
names end in `_test` or `_e2e`, and checks that suffix again inside the definer.

The owner calls Workspace's existing `guard_project_cleanup` in the same
transaction and validates its exact reply. NULL scopes or malformed guard
replies return `22023`, project NOWAIT contention returns `55P03`, and an
unretired video returns `23514`. Original constraints and DELETE triggers remain
active. The E2E route keeps its earlier fixture gates, namespace/project selection,
savepoint, and final commit; owner failures stop cleanup rather than skip Jobs.
This capability cannot substitute for job-ID cleanup in schema-owner screenshot
tools or for a production retention operation.

## Filtering fixture outcomes

`aap-jobs-filter-fixtures/v1` provides the fixed
`aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid)` capability. It updates
the two original filtering fixture projections and returns native bigint `2`.
The completed row receives status/progress/result; the failed row receives
status/progress/error. Both retain all other fields, including null lifecycle
timestamps and the failed result. Updates keep the mapper's ascending UUID
order, original `UPDATE OF` masks and `updated_at=now()` behavior.

Only actual `_test`/`_e2e` databases receive the runtime grant, and the definer
checks the database name again. Null or duplicate IDs return `22023`; a missing
row returns `P0002` and rolls back the entire function statement. The API keeps
its preceding invitation flush, original final full flush, audit and commit
order; it synchronizes the cached fields and expires `updated_at`. No general
record-edit capability, runtime role or credential selector was added.

## Transactional row locks and refreshes

`aap-jobs-row-locks/v1` defines the same fifteen native row columns for
`read_job_for_update(uuid)` and `read_job_for_share_nowait(uuid)`. They retain
the original waiting `FOR UPDATE` and immediate `FOR SHARE NOWAIT` locks in
the caller's transaction. Missing rows return no rows; PostgreSQL errors pass
through without retries or transaction changes. Domain authorization remains
with each caller. The ordinary API/worker profiles receive these two functions;
the strict lifecycle LOGIN keeps its existing eight capabilities.

The API routes ordinary domain reads and supported unlocked ORM column refreshes
through `read_job`. Partial refreshes retain their selected fields, dirty cache,
autoflush and concurrent database timestamp behavior. Explicit locked refreshes
and unsupported ORM query shapes retain their original path. Remaining filtered
queries, domain readers and legacy edits still prevent blanket private ACL removal.
See [the operation ledger](../../docs-site/dev/concepts/module-deployment.md#jobs-row-access)
for the bounded native and packaging evidence.

## Fixed domain queries

`aap-jobs-domain-queries/v1` packages six fixed native capabilities and their
schema without an API or ORM dependency. Cross-frame singleflight uses the
original actor/key advisory transaction lock before querying pending/running
jobs. History page and count remain separate actor/scene/kind statements;
ordering remains `created_at DESC`, with no new project predicate or tie breaker.
The admin prediction projection returns each current project's latest
`batch_predict` creation time; retained scope is intentionally excluded.
`frozen_deployment_usage(uuid,uuid,uuid)` reports pending/running Jobs and
undismissed retry failures for a deployment, including secondary snapshot stages
and legacy exact-instance references. Learning holds the registry and deployment
locks in the same transaction before requesting these facts. Job terminal state
does not prove that native inference has physically drained.

The borrowed-session API binding preserves ORM identities, original autoflush
and statement order. Taking the advisory lock does not flush; the following
mapped lookup does. The count and aggregate retain their original query flush.
No capability commits, retries, authorizes the actor, or changes execution
ownership. Ordinary runtime profiles require these six functions; the strict
lifecycle LOGIN retains its existing eight capabilities. Other private reads
and legacy edits still prevent global Jobs credential retirement.

## Action idempotency

`aap-jobs-action-idempotency/v1` publishes two fixed native capabilities.
`lock_action_idempotency(text,uuid,uuid,text)` keeps the exact legacy
`aap:data-manager:{action}:{project_id}:{actor_id}:{key}` transaction lock and
hash seed. It executes without ORM autoflush. `find_action_idempotent_jobs`
filters current project, actor, kind and the original JSONB text key, returning
all matching fifteen-column rows ordered by `created_at DESC, id DESC`, with a
waiting `FOR UPDATE` and no limit. The existing scalar caller chooses the first
row while every match stays locked until the caller transaction ends.

The binding keeps mapped-select autoflush and cached ORM identity without
forced refresh. NULL selectors preserve the original `IS NULL` predicates;
NULL advisory values retain the original Python `None` spelling. The server
chooses closed predicate templates and binds every value; callers continue to
normalize keys, authorize current domain access and compare request digests.
No capability commits, retries or adds a business status predicate.

Ordinary roles receive these two functions while strict lifecycle/list/domain
profiles retain their existing scopes. This removes the filtered locking read's
private UPDATE dependency. Other private SELECT consumers remain tracked in
[the operation ledger](../../docs-site/dev/concepts/module-deployment.md#jobs-action-idempotency).
Point-cloud quality cancellation uses only status/payload from existing
`read_job`, preserving dirty cache state under `no_autoflush`; dispatch uses
the existing cached getter without forced refresh.

## Public notification scope facts

`aap-jobs` publishes the fixed, read-only `aap_jobs_v1.notification_scope_facts`
relation for consumers that need only an async job's identity, kind, and retained
project scope. The projection keeps unknown `kind` values intact and derives
`scope_project_id` from `coalesce(project_id, retained_project_id)`.

This notification-scope relation leaves authorization, global-kind policy,
pagination, and delivery decisions with the consumer. Consumers receive
`SELECT` on the view, not write rights through it. Jobs retains its private
lifecycle writes and direct access to `async_jobs`.

The versioned row contract and SQL resource are packaged in this distribution so
consumers can validate the relation without importing the API or SQLAlchemy models.

## Jobs async-job list query

`aap_jobs_v1.async_job_list_page` and `aap_jobs_v1.async_job_list_total` expose
the existing API list filters and visibility rules as two separate,
same-transaction capabilities. Page rows have a fixed column contract suitable
for hydrating the API's existing `AsyncJob` identity-map objects; the private
predicate helper retains the underlying table composite. The API still reads
the actor's current Identity status once before calling Jobs and enriches each
page with Workspace project display data afterward. Each page and total
statement also reads the small Identity facts capability for its membership
arm, preserving the old same-statement active/known-role/viewer pairing check;
the original preflight role snapshot still selects the global and owner branches.

The query keeps current and retained project scopes, the two audited
unscoped-global kinds, reviewer/manager export visibility, six JSONB text search
fields, stable `created_at DESC, id DESC` ordering, and independent page then
total statements. It borrows the caller's transaction and does not own Identity
authentication, endpoint credential checks, commit, rollback, or the broader
Jobs read/write surface.

## Private Celery queue owner

The optional `aap-jobs-queue` runtime accepts the existing JSON Celery dispatch
and cooperative revoke requests over `aap-jobs-queue/v2`. It holds only the
Celery broker URL and a private service bearer; it has no API, database, or
Identity dependency. The API keeps its embedded Celery transport when
`JOBS_QUEUE_URL` is blank. An explicit remote URL never falls back to the local
broker.

Consumers include ordinary batch prediction with its original dynamic CPU/GPU
queue and soft time limit, existing failed-prediction retry dispatch, frame
fan-out, cross-frame propagation, all three annotation-export endpoints, and
the existing quality and video-tracker ports. Audit metadata and task events
use the selected transport but retain their existing synchronous fallback;
they are domain delivery, not durable user Jobs.

The queue accepts only native JSON, the explicit `queue` and `soft_time_limit`
options, cooperative revoke, and a single flat canvas with frozen child,
group and mutable callback IDs. Nested canvases and arbitrary Celery options
are not supported. Static routes and Celery's publisher retry policy remain
unchanged. The reply confirms broker acceptance only. Revoke uses
`terminate=False` and does not prove execution stopped.
Qualification and applicable deployment status are recorded in the
[module ledger](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账).

The quality port sends one positional UUID to each existing `media` task: mask
QC, point-cloud quality, video-track quality, mask repair, repair rollback, and
mask-format import. Its synchronous adapter uses the same bounded HTTP contract
without entering another event loop. All execution IDs are selected and
persisted before publication; the original domain binding and failure policies
remain after publication. Credentials are loaded only at dispatch, so explicit
remote configuration failures never fall back to the embedded broker.

Video tracking uses the same synchronous client and its existing `gpu` route.
The API and mask-repair worker select the same dispatcher factory. Creation
and correction persist the complete summary and dispatch intent in their
acceptance transaction, then bind the accepted ID as before. Cancellation
commits its state, one audit, and the scoped retirement intent before redriving
the same canonical tracker. Unconfirmed redrive keeps the existing retryable 503. Tracking and correction both require a matching cancelled canonical
tracker before terminal redrive is allowed.

The standalone `apps/jobs/Dockerfile.queue` image runs as UID 10005, exposes no
host port, and reads `JOBS_QUEUE_BROKER_URL_FILE` and `JOBS_QUEUE_TOKEN_FILE`.
`docker-compose.jobs-queue.yml` is an optional local assembly; the API must be
on the same private Compose network to use `http://jobs-queue:8190`. Host-run
development APIs keep the embedded transport unless a separately reachable
private endpoint is provided. This runtime does not change the default worker
or deployment path.

## Durable dispatch handoff

`aap-jobs-dispatch-ledger/v1` freezes complete queue envelopes in the Jobs-owned
private `aap_jobs_v1.dispatch_intents` table. Migration 0368, the packaged SQL,
and `dispatch.schema.json` define the Native ABI:

- API: `prepare_dispatch(jsonb)`, `finish_dispatch(jsonb)`, `dispatch_status(uuid)`.
- Relay: `claim_dispatches(jsonb)`, `finish_dispatch(jsonb)`, `dispatch_status(uuid)`.

Preparation borrows the caller transaction and never commits it. Publication
happens only after acceptance commits; a separate short transaction records
the fenced acknowledgement. Unknown publication or a lost acknowledgement
keeps the same envelope and IDs recoverable. The separate relay claims one
item immediately before publishing, so queued items do not lose their lease
while waiting behind network calls. Expired generations cannot acknowledge
new claims. Confirmed submissions are not automatically replayed.

The API's input admission and domain failure policies remain authoritative:
unfinished HTTP input admission is not reconstructed from summary metadata;
existing retry delivery keeps its own ledger. Terminal ordinary Jobs retire
unsubmitted intents, while tracker retirement has the narrow canonical proof
above. A lease timeout never proves physical execution stopped.

The relay uses the same immutable image with the `aap-jobs-dispatch-relay`
entrypoint. Install `infra/docker/jobs-dispatch-grants.sql` for an explicit
database and distinct function-only relay LOGIN, then combine
`docker-compose.jobs-dispatch-relay.yml` with the queue assembly. Queue secrets
are broker plus bearer; relay secrets are the narrow DSN plus bearer, never
broker or API/migration credentials. Neither role has direct ledger access.

Outside Compose, set `JOBS_DISPATCH_RELAY_ENABLED=true`,
`JOBS_DISPATCH_RELAY_DATABASE_URL[_FILE]`, `JOBS_QUEUE_URL` and
`JOBS_QUEUE_TOKEN[_FILE]`. Direct and file selectors conflict. Optional bounded
settings are `JOBS_DISPATCH_RELAY_BATCH_SIZE`, `JOBS_DISPATCH_RELAY_LEASE_SECONDS`
and `JOBS_DISPATCH_RELAY_POLL_SECONDS`. Frame recovery reuses the frozen logical
segment IDs and completes only from the full canonical receipt set, not from
duplicate chord header results. This is at-least-once publication, not
exactly-once execution or a general historical-writer retirement system.

## Package verification

From the repository root, the locked test extra includes the asynchronous runner:

```bash
uv run --project apps/jobs --locked --extra test --extra queue-runtime \
  --extra dispatch-relay python -m pytest apps/jobs/tests
```

Database-backed API cases must use the verifier-owned disposable database, not
an everyday development database. The opt-in partial-canvas runtime case also
requires independently installed `aap-jobs-queue` and
`aap-jobs-dispatch-relay` entrypoints; it kills the publisher and first queue
process, replays the saved IDs, and checks actual frame writes.

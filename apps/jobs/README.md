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
invoker, never a client JSON field. The API retains SELECT/UPDATE/DELETE for
consumers not yet migrated. Creation-right closure does not complete Jobs
credential closure or runtime recovery.

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

`aap-jobs-domain-queries/v1` packages five fixed native capabilities and their
schema without an API or ORM dependency. Cross-frame singleflight uses the
original actor/key advisory transaction lock before querying pending/running
jobs. History page and count remain separate actor/scene/kind statements;
ordering remains `created_at DESC`, with no new project predicate or tie breaker.
The admin prediction projection returns each current project's latest
`batch_predict` creation time; retained scope is intentionally excluded.

The borrowed-session API binding preserves ORM identities, original autoflush
and statement order. Taking the advisory lock does not flush; the following
mapped lookup does. The count and aggregate retain their original query flush.
No capability commits, retries, authorizes the actor, or changes execution
ownership. Ordinary runtime profiles receive these five functions; the strict
lifecycle LOGIN retains its existing eight capabilities. Other private reads
and legacy edits still prevent global Jobs credential retirement.

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
and cooperative revoke requests over `aap-jobs-queue/v1`. It holds only the
Celery broker URL and a private service bearer; it has no API, database, or
Identity dependency. The API keeps its embedded Celery transport when
`JOBS_QUEUE_URL` is blank. An explicit remote URL never falls back to the local
broker.

Consumers include cross-frame propagation and the project, batch, and Data
Manager annotation-export endpoints. Cross-frame sends one positional job ID;
exports send the existing keyword payload. Both use the shared `task_routes`
resource, which routes exports to the existing `export` queue. Each publication
has a preselected execution ID. Cross-frame stores it before dispatch; project and batch workers
bind it when they claim the job; Data Manager binds it after successful broker
acceptance and marks the durable job failed if dispatch raises. A pending Data
Manager idempotency retry keeps the job and assigns a new publication ID, as
before. The reply confirms broker acceptance only. Revoke uses `terminate=False` and does not
prove a task stopped. Custom task options, Celery canvases, other task payloads,
and non-JSON serializers remain unqualified. Bounded local API/owner/worker and
full-database-restore evidence is recorded separately for [cross-frame
composition](../../docs-site/dev/concepts/module-deployment.md#jobs-lifecycle-queue)
and [annotation exports](../../docs-site/dev/concepts/module-deployment.md#jobs-export-dispatch).
This does not qualify the remaining producers or complete Jobs credential
closure.

The standalone `apps/jobs/Dockerfile.queue` image runs as UID 10005, exposes no
host port, and reads `JOBS_QUEUE_BROKER_URL_FILE` and `JOBS_QUEUE_TOKEN_FILE`.
`docker-compose.jobs-queue.yml` is an optional local assembly; the API must be
on the same private Compose network to use `http://jobs-queue:8190`. Host-run
development APIs keep the embedded transport unless a separately reachable
private endpoint is provided. This runtime does not change the default worker
or deployment path.

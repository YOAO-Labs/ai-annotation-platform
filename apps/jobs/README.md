# Jobs owner contracts

## Borrowed lifecycle writes

`JobsLifecycleManager` owns the existing generic lifecycle rules. The API keeps
its ORM identities and caller-owned transaction. Creation retains the original
full flush and compiled INSERT/defaults/RETURNING through the fixed
`yoao_jobs_v1.async_job_creation` relation; all dirty Job rows use one native
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

The `aap-jobs-fixture-guard/v1` contract exposes
`yoao_jobs_v1.guard_fixture_project_jobs(uuid[])` and
`yoao_jobs_v1.delete_fixture_project_jobs(uuid[])`. They lock the exact sealed
fixture projects and their Jobs, retain the Workspace video guard, and require
the native prediction or Export retirement proofs before deleting a real Job.
Undelivered presentation Jobs require their own birth proof; unmatched Jobs,
retry ledgers or active execution remain blockers. A terminal Job status alone
does not prove physical work stopped. Retry attempts and delivery tombstones
remain independent of the deleted Job. Before an exact retired Export Job is
deleted, Jobs preserves each complete dispatch intent as a private receipt so
the Job's foreign-key cascade cannot erase its envelope or acknowledgement.
These are test-database-only owner capabilities, not product APIs.

NULL scopes or malformed Workspace replies return `22023`, project NOWAIT
contention returns `55P03`, and an unretired video or unproved Jobs row returns
`23514`. The guard borrows the caller transaction and preserves original
constraints and DELETE triggers.

`fixture_job_dataset_scope(uuid,text,jsonb,uuid,uuid)` uses the Job ID, kind, payload, project and actor. A new retry can prove scope only from its own admission and frozen input. A new cross-frame Job includes the source Scene and every ready target's Dataset; the worker rechecks that scope before writing. Neither current Task mappings nor terminal status can certify a historical Job. Unproved scope blocks cleanup, and deleting a bound Job does not certify physical settlement.

The current migration-bound contract is
`apps/api/alembic/sql/0465_fixture_jobs.schema.json`, matching the installed
`yoao_jobs/fixture_guard.schema.json`. Native acceptance checks every declared
function's arguments, result, volatility and parallel safety against PostgreSQL.
Historical SQL stays unchanged; reclaim classification advances through
`0437_fixture_reclaim_job_scope.sql`, not a rewrite of the `0432` input.

Native cleanup acceptance connects through the verified private fixture owner,
including the Node bigint and transaction checks. A separate ordinary API LOGIN
must remain denied both direct Jobs deletion and private fixture cleanup; a
test database suffix does not grant that caller authority.

Dataset-scoped fixture media backfills use the private
`yoao_jobs_v1.delete_fixture_media_backfills(run_id,dataset_id)` capability before
the Dataset is deleted. It locks the exact sealed run and Dataset, requires each
recorded Job's Jobs and Assets retirement proofs, and preserves immutable
publication and execution evidence. A missing Dataset, foreign run, active
reader or unretired Job does not authorize deletion; ordinary callers cannot
execute this fixture-owner capability. The caller's transaction owns the
deletion and rollback.

## Filtering fixture outcomes

`aap-jobs-filter-fixtures/v1` provides the fixed
`yoao_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid)` capability. It updates
the two original filtering fixture projections and returns native bigint `2`.
The completed row receives status/progress/result; the failed row receives
status/progress/error. Both retain all other fields, including null lifecycle
timestamps and the failed result. Updates keep the mapper's ascending UUID
order, original `UPDATE OF` masks and `updated_at=now()` behavior.

The verified private fixture owner invokes this test-only capability; ordinary
API and worker identities remain denied even on `_test`/`_e2e` databases. The
definer also checks the database name. Null or duplicate IDs return `22023`; a
missing row returns `P0002` and rolls back the entire function statement.
Private fixture setup retains its invitation flush, final full flush, audit
and commit order, synchronizes cached fields and expires `updated_at`. No
general record-edit capability or ordinary-runtime credential was added.

## Transactional row locks and refreshes

`aap-jobs-row-locks/v1` defines the same fifteen native row columns for
`read_job_for_update(uuid)`, `read_job_for_update_nowait(uuid)` and
`read_job_for_share_nowait(uuid)`. They retain waiting `FOR UPDATE`, immediate
`FOR UPDATE NOWAIT` and `FOR SHARE NOWAIT` fences in the caller's transaction.
The frozen export worker uses the immediate update fence before claiming,
checking or publishing its exact attempt. Missing rows return no rows;
PostgreSQL errors pass through without retries or transaction changes.
Domain authorization remains with each caller. Ordinary API/worker profiles
receive these functions; the strict lifecycle LOGIN does not receive any of
the row-lock companions.

API 借用 Session 时只重定向 ORM 生成的单主键、无锁列刷新，保留原投影、绑定值和其余脏字段；自定义查询／锁按原入口处理。`app/db/owner_orm_compat.py` 固定 SQLAlchemy `2.0.49` 的刷新选项与 fetch 同步，使用 owner 返回的 ID 登记缓存变化和回滚，不向数据库重新发送 UPDATE／DELETE。

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
lifecycle LOGIN retains the eight lifecycle functions and the three existing
snapshot/progress capabilities required by the borrowed session. Other private reads
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

`yoao-jobs` publishes the fixed, read-only `yoao_jobs_v1.notification_scope_facts`
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

`yoao_jobs_v1.async_job_list_page` and `yoao_jobs_v1.async_job_list_total` expose
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

## Celery publication and cooperative revocation

The API and Jobs-producing workers publish directly through the platform's
Celery sender. They do not select a queue HTTP endpoint, bearer or alternate
broker. The portable `aap-jobs-queue/v2` schema, route resource and standalone
queue package remain available for wire conformance and explicit external
experiments, not as a platform deployment option.

Consumers include ordinary batch prediction with its original dynamic CPU/GPU
queue and soft time limit, existing failed-prediction retry dispatch, frame
fan-out, cross-frame propagation, all three annotation-export endpoints, and
the existing quality and video-tracker ports. Audit metadata and task events
also use Celery and retain their synchronous fallback; they are domain
delivery, not durable user Jobs.

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
mask-format import. The existing task adapters preserve execution IDs chosen
and persisted before publication, domain binding and failure policies.

The API and mask-repair worker both use the existing Celery video tracker
adapter and `gpu` route. Creation and correction persist the complete summary
and dispatch intent in their
acceptance transaction, then bind the accepted ID as before. Cancellation
commits its state, one audit, and the scoped retirement intent before redriving
the same canonical tracker. Unconfirmed redrive keeps the existing retryable 503. Tracking and correction both require a matching cancelled canonical
tracker before terminal redrive is allowed.

## Durable dispatch handoff

`aap-jobs-dispatch-ledger/v1` freezes complete queue envelopes in the Jobs-owned
private `yoao_jobs_v1.dispatch_intents` table. Migration 0368, the packaged SQL,
and `dispatch.schema.json` define the Native ABI:

- API: `prepare_dispatch(jsonb)`, `finish_dispatch(jsonb)`, `dispatch_status(uuid)`.
- Relay: `claim_dispatches(jsonb)`, `finish_dispatch(jsonb)`, `dispatch_status(uuid)`.

Preparation borrows the caller transaction and never commits it. Jobs and
immutable dispatch births are recorded in that transaction; historical Jobs and
intents are not backfilled with invented no-publication proof. Publication
happens only after acceptance commits, under a separate function-only,
session-held publication fence. Each generation records physical entry before
the broker call and settles only on the publisher's positive acknowledgement.
Inside the platform, a broker exception or interrupted send does not prove
that nothing reached Redis; the physical attempt remains `entered`, even if
a later replay succeeds. The portable HTTP conformance transport still
distinguishes a positively proved pre-request `not_sent` from connection or
response uncertainty. Errors are never classified by their message text.
The relay claims one item immediately before publishing. A later valid generation
may replay the exact frozen envelope and IDs while an earlier attempt remains
unknown; replay or lease expiry does not settle that earlier physical obligation.
If a prior generation already has a positive publication receipt, the relay
reconciles the same intent without another broker call. Expired generations
cannot acknowledge new dispatch leases.

The API's input admission and domain failure policies remain authoritative:
unfinished HTTP input admission is not reconstructed from summary metadata;
existing retry delivery keeps its own ledger. Terminal ordinary Jobs retire
unsubmitted intents, while tracker retirement has the narrow canonical proof
above. A lease timeout never proves physical execution stopped.

The normal Celery Beat schedules `app.workers.jobs_dispatch.recover_job_dispatches`
on `jobs.recovery`. `celery-worker-jobs-recovery` runs one bounded pass at a
time. Its only data credentials are a claim LOGIN (`JOBS_DISPATCH_DATABASE_URL`
or `_FILE`) and the separate publication LOGIN (`JOBS_PUBLICATION_DATABASE_URL`
or `_FILE`). Worker startup authenticates both and checks exact function-only
profiles on the same database. Beat has neither credential. Existing
producers still use the narrow publication LOGIN to fence their first send;
they do not get the claim LOGIN. The broad DDL/maintenance worker does not
consume `jobs.recovery`.

On a new installation, create distinct `yoao_jobs_dispatch_relay_client` and
`yoao_jobs_publication_client` LOGINs with no memberships, `NOINHERIT` and no
administrative privileges. Use `psql`'s `\password` command to set their
passwords. After Alembic and ordinary runtime grants, install the same two
profiles into the intended database:

- Default migration-owned database: `infra/docker/jobs-recovery-grants.sql`
  (`expected_database`, `migration_role`, `application_role`, `dispatch_role`,
  `publication_role`). An inherited `PUBLIC TEMP` grant must first be removed
  from the intended database, preserving explicit TEMP for its maintenance
  role when needed.
- If owner objects were transferred to separate NOLOGIN identities, keep the
  existing `jobs-dispatch-grants.sql` and `module-execution-grants.sql` order
  after the modular owner and ordinary runtime profiles.

Use private `postgresql://` DSN files; expose the claim file only to recovery.
Stop the retired queue/relay topology before starting the embedded topology
against the same broker and database. Preserve the ledger, its original
broker/result DB and execution IDs; do not backfill or delete unresolved
attempts. Frame recovery reuses the frozen logical segment IDs and completes
only from the full canonical receipt set, not from duplicate chord header
results. This is at-least-once publication, not exactly-once execution.

Retirement is separate from replay: the owner must fence the exact Job and all
delivery identities, then obtain a Jobs publication tombstone. Every entered
generation and Celery retry must have its own positive settlement; a later
successful replay never settles or erases an earlier unknown attempt. An
explicit terminal `not_sent` tombstone is also possible when every intent has
only positively refused attempts or has never entered publication, with no
accepted or entered attempt or retry. Both paths require immutable births, the
complete execution set and exclusive publication fences; missing historical
birth evidence cannot establish no publication. The tombstone permanently fences
later publication without claiming acceptance. Timeouts and lost responses do
not establish that outcome. Preserve the
associated queue, results, objects, credentials and evidence until the remaining
physical obligations are resolved.

A complete Job can mix positively published intents with proved unsent intents,
for example after cancellation during a queue outage. Retirement verifies each
intent separately and retains the complete set, including attempts absent from
the broker. Any published intent makes the Job tombstone `published`, never
`not_sent`; an entered or unknown attempt still blocks the whole retirement.
This settles publication only, not the associated physical execution.

Standalone media producers call `prepare_media_delivery` in the existing
business transaction with their actual subject, original receipt, delivery ID
and ordinary v2 envelope. The Assets owner validates and locks the source and
establishes either ordinary ownership or one exact open fixture run; unknown,
ambiguous, sealed and mismatched ownership is refused. Ordinary ownership
preserves the original envelope and publication path. For fixture ownership,
Jobs selects the private queue, preserves the remaining options and atomically
records the publication with its Assets execution birth. Neither a fixture run
nor a private queue or executor credential is accepted from the producer.
The packaged `media_preparation.schema.json` describes the finite request and
result; derivative batches additionally bind every output ID to its actual
original receipt.

Fixture-owned pyramid, thumbnail and video deliveries without an AsyncJob use
the same Jobs publication-control credential but a separate immutable delivery
record. The producer transaction binds the actual run, DatasetItem or
TaskOriginal subject, source receipt, private queue and complete v2 envelope;
Assets validates the source and payload before Jobs records it. The worker
waits on the same publication fence and requires a positive accepted receipt
before Assets admits physical work. An earlier unknown publication attempt
does not prevent admission of a later accepted replay of the same delivery,
but it remains evidence that prevents physical retirement. A sealed run can
retire an exact delivery only after all its publication attempts have definite
outcomes; no attempts or any unknown attempt remain blocked. Assets reads the
exact, shared-fenced retirement outcome for the run, delivery and envelope
digest: `accepted` still
requires positive settlement of the matching physical execution, while
`not_sent` requires positive proof that no execution started. A missing or
mismatched tombstone, or any entered publication attempt, yields no outcome;
the publication tombstone alone never proves execution or no-start.

## Package verification

From the repository root, the locked test extra includes the asynchronous runner:

```bash
uv run --project apps/jobs --locked --extra test --extra queue-runtime \
  --extra dispatch-relay python -m pytest apps/jobs/tests
```

Database-backed API cases must use the verifier-owned disposable database, not
an everyday development database. The partial-canvas runtime test kills
the actual API publisher while a test relay holds its second child before
Redis, then runs the real recovery worker and Beat. It checks that the
uncertain generation stays unsettled and that the complete frame set is saved.

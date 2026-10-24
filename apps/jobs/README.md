# Jobs public notification scope facts

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

The only consumer wired in this slice is cross-frame propagation: one positional
job ID, the existing `default` route, and a stable execution ID stored before
dispatch. The reply confirms broker acceptance only. Revoke uses
`terminate=False` and does not prove a task stopped. Custom task options, Celery
canvases, other task payloads, and non-JSON serializers remain unqualified; the
independent container/broker acceptance is still pending.

The standalone `apps/jobs/Dockerfile.queue` image runs as UID 10005, exposes no
host port, and reads `JOBS_QUEUE_BROKER_URL_FILE` and `JOBS_QUEUE_TOKEN_FILE`.
`docker-compose.jobs-queue.yml` is an optional local assembly; the API must be
on the same private Compose network to use `http://jobs-queue:8190`. Host-run
development APIs keep the embedded transport unless a separately reachable
private endpoint is provided. This runtime does not change the default worker
or deployment path.

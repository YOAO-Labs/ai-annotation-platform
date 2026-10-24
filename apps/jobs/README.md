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
page with Workspace project display data afterward.

The query keeps current and retained project scopes, the two audited
unscoped-global kinds, reviewer/manager export visibility, six JSONB text search
fields, stable `created_at DESC, id DESC` ordering, and independent page then
total statements. It borrows the caller's transaction and does not own Identity
authentication, endpoint credential checks, commit, rollback, or the broader
Jobs read/write surface.

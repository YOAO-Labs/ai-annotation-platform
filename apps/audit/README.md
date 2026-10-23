# Audit append and global query capabilities

This package defines the `aap-audit-append/v1` event/receipt contract and
`aap-audit-query/v1` global list/export contract, with PostgreSQL bindings for
both. It does not start an HTTP service. The clients borrow the caller's
PostgreSQL session; the caller remains responsible for commit or rollback.

`append_events(jsonb)` accepts a closed event envelope while keeping `action`
open for existing and future domain events. It inserts in request order, leaves
`created_at` to PostgreSQL's transaction-time default, and returns `id` as a
canonical positive signed-64-bit decimal string so JavaScript does not lose
`bigint` precision. `detail_json` accepts any JSON value, including JSON null,
matching the current SQLAlchemy JSONB mapping. The Python API hydrates inserted
rows back into the borrowed session identity map before returning them.

The append adapter appends before ORM autoflush, hydrates ORM identity from each
input event and validated receipt under `no_autoflush`, then performs the
original explicit full flush without reading `audit_logs`. This preserves
cached values, pending-actor deletion order and pending-new-actor foreign-key
behavior. HTTP metadata retains the post-response best-effort policy and its
stale-actor foreign-key retry as an anonymous event. Existing narrow
`aap_audit_v1.record_*` functions remain the cross-owner transaction path.

`query_events(jsonb)` provides the fixed global filters used by the super-admin
list and export endpoints. It returns the matching total before cursor or
offset pagination, orders by `(created_at DESC, id DESC)`, and preserves the
current Identity profile email fallback without changing stored history.
Exports use the existing 50,000-row ceiling; an oversized result includes no
event rows so the API can return 413. The SQLAlchemy adapter follows the
caller's normal autoflush setting and never commits. Export still appends
`audit.export` and commits before returning its response stream.

Project-admin visibility remains in the API adapter because its scope combines
Workspace and Labeling facts. Annotation timelines, batch audit views,
dashboard person activity, monthly aggregates, partition maintenance, and cold
archives also remain outside this query capability.

Migration `0315` adds append and `0317` adds global query after Notifications
read-state `0316`. `infra/docker/module-data-grants.sql` keeps direct append
writes with the non-login Audit owner and supplies the query owner's narrow
column reads. The focused `audit-append-grants.sql` profile grants the existing
API LOGIN the append function, retains its current table `SELECT`, revokes
direct table `INSERT` and sequence write access, and removes those privileges
from existing partitions. The current LAN grant path still assigns that tree
to the application role, so this slice does not qualify unique production
writer ownership. Apply a compatible append profile only after all API/worker
consumers use the owner path and old processes are drained.

The focused `audit-query-grants.sql` profile gives a separate LOGIN only
`query_events(jsonb)` with no direct table, sequence, or append capability. The
existing API LOGIN retains its current table reads and gains the exact query
function capability. The bounded checks are `apps/audit/tests`, the Node
conformance self-check/live clients, focused API query and append tests, and the
owned `scripts/verify_module_data_boundary.py` PostgreSQL verifier. Other Audit
readers, analytics, partition/retention, archival/runtime deployment, and full
Audit module recovery remain outside this query capability.

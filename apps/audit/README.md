# Audit append capability

This package defines the `aap-audit-append/v1` event/receipt contract and the
PostgreSQL binding for same-transaction append. It does not start an HTTP
service. The caller keeps its current PostgreSQL transaction and remains
responsible for commit or rollback.

`append_events(jsonb)` accepts a closed event envelope while keeping `action`
open for existing and future domain events. It inserts in request order, leaves
`created_at` to PostgreSQL's transaction-time default, and returns `id` as a
canonical positive signed-64-bit decimal string so JavaScript does not lose
`bigint` precision. `detail_json`
accepts any JSON value, including JSON null, matching the current SQLAlchemy
JSONB mapping. The Python API hydrates inserted rows back into the borrowed
session identity map before returning them.

The PostgreSQL client leaves unit-of-work ordering to its caller and never
commits. The API `AuditService` adapter appends before ORM autoflush, hydrates
ORM identity from each input event and validated receipt under `no_autoflush`,
then performs the original explicit full flush without reading `audit_logs`.
This preserves the existing cached values, pending-actor deletion order and
pending-new-actor foreign-key behavior. HTTP metadata retains the post-response
best-effort policy and its stale-actor foreign-key retry as an anonymous event.
Existing narrow `aap_audit_v1.record_*` functions remain the cross-owner
transaction path.

Migration `0315` depends on repository head `0314`. `infra/docker/module-data-grants.sql`
keeps direct append writes with the non-login Audit owner. The focused
`audit-append-grants.sql` profile grants the existing API LOGIN the append
function, retains its current table `SELECT`, revokes direct table `INSERT` and
sequence write access, and removes those privileges from existing partitions.
A separate client gets only the append function and no table or sequence
access. The profile fails closed if the API owns the audit partition tree or
inherits migration-role defaults that grant future table/sequence writes. The
current LAN grant path still assigns that tree to the application role, so this
slice does not qualify unique production writer ownership. Apply a compatible
profile only after API/worker consumers use the owner path and old processes
are drained.

The bounded package checks are `apps/audit/tests`, the Node conformance
self-check/live client, focused API writer/middleware tests, and the owned
`scripts/verify_module_data_boundary.py` PostgreSQL verifier. Query/history,
analytics, partition/retention, archival/runtime deployment, and full Audit
module recovery remain outside this append capability.

# Audit append, query, and read-relation capabilities

This package defines the `aap-audit-append/v1` event/receipt contract,
`aap-audit-query/v1` global list/export contract, and
`aap-audit-read-relations/v1` event-row layout. It does not start an HTTP
service. Transactional clients borrow the caller's PostgreSQL session; the
caller remains responsible for commit or rollback. The contracts are usable
without SQLAlchemy; PostgreSQL table metadata is imported explicitly from the
optional `read_relation_postgres` module.

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

The read-only `aap_audit_v1.audit_events` view publishes the 14 fixed `AuditLog`
columns under a security barrier. Its `ip` column remains native PostgreSQL
`inet`; Python SQLAlchemy therefore sees the same address/network values as the
previous ORM read, and Node `pg` receives PostgreSQL's text representation.
The physical row key is `(id, created_at)`, matching the partitioned source
table's primary key; valid stored rows may share an `id` when their timestamps
differ. The existing API ORM mapping remains id-only to preserve its legacy
identity-map behavior.
Stored `actor_email` remains an immutable snapshot. Consumers that need the
current-email fallback compose the public Identity profile relation and apply
it only when the stored value is NULL. The view is data access, not
authorization: API consumers keep their existing role checks, project scopes,
filters, pagination, sorting, and transaction snapshots.

The API maps full relation rows onto the existing ORM identity map so repeated,
dirty, and expired `AuditLog` instances retain their normal SELECT behavior.
Core projections explicitly preserve autoflush when enabled. Neither adapter
commits or opens a second transaction. The migrated non-training readers are
the project-admin list, annotation/task histories, batch audit logs, admin
registration/person activity, and review-round contributor snapshot. The
super-admin global list/export remains on `query_events(jsonb)`.

Analytics and lifecycle readers remain outside this relation migration:
monthly summary's materialized/raw tail, DuckDB sync, project performance,
Learning/active-learning evidence, partition retention/archive maintenance,
and append/hydration consumers. Their existing API `audit_logs` privileges
prevent a universal private-table SELECT revocation in this slice.

Migration `0315` adds append and `0317` adds global query after Notifications
read-state `0316`; `0320` adds the read relation after Notifications deletion
`0319`. `infra/docker/module-data-grants.sql` keeps direct append writes with
the non-login Audit owner and transfers the read view to that owner. The
focused `audit-append-grants.sql` profile grants the existing
API LOGIN the append function, retains its current table `SELECT`, revokes
direct table `INSERT` and sequence write access, and removes those privileges
from existing partitions. The current LAN grant path still assigns that tree
to the application role, so this slice does not qualify unique production
writer ownership. Apply a compatible append profile only after all API/worker
consumers use the owner path and old processes are drained.

The `audit-query-grants.sql` profile gives one LOGIN only `query_events(jsonb)`;
the separate `audit-read-grants.sql` profile gives another LOGIN only
`SELECT` on `audit_events`. Neither profile grants direct base-table reads,
sequences, or write functions. The API LOGIN gains SELECT on the view while
retaining current private-table access for residual analytics, Learning,
retention and test/seed consumers. Focused checks cover the row contract,
native INET parity, identity-map/autoflush behavior, API scope, and the Node
read-only peer. This relation slice does not close full Audit assembly,
analytics ownership, partition/archive recovery, or universal private-table
revocation.

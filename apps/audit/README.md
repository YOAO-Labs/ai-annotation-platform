# Audit capabilities

This package defines the `aap-audit-append/v1` event/receipt contract,
`aap-audit-query/v1` global list/export contract, and
`aap-audit-read-relations/v1` event-row and
`aap-audit-daily-projection/v1` daily aggregate contracts. It does not start an
HTTP service. Transactional clients borrow the caller's PostgreSQL session; the
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
stale-actor foreign-key retry as an anonymous event. Its optional request ID is
included only when it fits the append contract's 36-character limit; an
overlong `X-Request-ID` remains unchanged in the request context and response
header but is omitted from this metadata row. Broker publish failures fall back
to the same synchronous append path, while a publish result with unknown
delivery outcome can still produce a duplicate and a later worker failure can
still lose the row. This path remains best-effort and has no exactly-once
guarantee. This normalization is limited to middleware HTTP metadata; explicit
business-audit calls still use the request-context ID directly, so their
overlength handling remains a separate existing follow-up. Existing narrow
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

The monthly summary's raw tail, DuckDB sync, and project-performance event
reads use `audit_events`. The summary's completed-day aggregates use
`audit_daily_facts`; only the owner capability refreshes the underlying
`mv_audit_bi_daily`. Learning/active-learning evidence, partition
retention/archive maintenance, and append/hydration consumers still read or
write the base table, so the API retains its existing private-table privileges.

`aap_audit_v1.audit_daily_facts` publishes the existing six-dimension daily
materialization through a read-only security-barrier view. Its row key matches
the materialized view's unique index, and PostgreSQL `date`, `smallint`, and
`bigint` types are preserved. The monthly summary reads completed days through
this relation and continues to merge the current UTC raw tail from
`audit_events`.

The maintenance capability includes daily materialized-view refresh. The
caller still commits its transaction; the owner function does not change
refresh mode or swallow errors. Its restricted `NOLOGIN` owner needs database
`TEMPORARY` because PostgreSQL's concurrent refresh creates a temporary
relation. The independent maintenance LOGIN receives only `EXECUTE` on the
fixed command function; it has no direct table, sequence, or DDL privileges.
The read-only Audit client may select `audit_daily_facts` but cannot refresh the
view or select the private materialization.

Migration `0315` adds append and `0317` adds global query after Notifications
read-state `0316`; `0320` adds the event read relation after Notifications
deletion `0319`; `0322` adds the daily projection after `0321`; `0333` adds the
fixed maintenance command after the preceding owner migrations.
`infra/docker/module-data-grants.sql` keeps direct append writes with
the non-login Audit owner and transfers the read view to that owner. The
focused `audit-append-grants.sql` profile grants the existing
API LOGIN the append function, retains its current table `SELECT`, revokes
direct table `INSERT` and sequence write access, and removes those privileges
from existing partitions. The current LAN grant path still assigns that tree
to the application role, so this slice does not qualify unique production
writer ownership. Apply a compatible append profile only after all API/worker
consumers use the owner path and old processes are drained.

The `audit-query-grants.sql` profile gives one LOGIN only `query_events(jsonb)`;
the separate `audit-read-grants.sql` profile gives another LOGIN `SELECT` on
`audit_events` and `audit_daily_facts`. Neither read profile grants direct
base-table or materialized-view reads, sequences, or write functions. The API
LOGIN gains SELECT on both views while retaining current base-table access for
Learning, retention, and test/seed consumers. The dedicated maintenance owner
performs refresh through the fixed command capability; read and query clients
have no refresh or database `TEMPORARY` privilege.
Focused checks cover the row contract,
native INET parity, identity-map/autoflush behavior, API scope, and the Node
read-only peer. The independent maintenance runtime is implemented, while
dedicated LOGIN activation and consolidated runtime acceptance remain pending.

## Independent maintenance runtime

The `aap-audit-maintenance` one-shot CLI provides `ensure-partitions`,
`archive-partitions`, and `refresh-daily`. It uses the same fixed
`aap_audit_v1.maintenance_command(jsonb)` capability as the retained API/Celery
adapter and verifies at startup that its database LOGIN can execute only that
function. It has no HTTP listener, scheduler, queue, or application imports.
The existing API/Celery binding remains available during migration.

Set `AUDIT_MAINTENANCE_DATABASE_URL` or its `_FILE` counterpart for every
command. Either the direct value or the file can provide a secret, never both.
The database URL is redacted from configuration representations and command
errors. Connections time out after five seconds; statements are bounded to 60
seconds and run in one transaction per invocation. Database grants must expose
only the fixed maintenance function to the independent runtime LOGIN.

`ensure-partitions` creates the current and next three months' partitions.
`refresh-daily` invokes the existing Audit daily materialization refresh.
Neither command needs object-storage configuration. `archive-partitions` also
needs `AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL`,
`AUDIT_MAINTENANCE_ARCHIVE_S3_ACCESS_KEY_ID`, and
`AUDIT_MAINTENANCE_ARCHIVE_S3_SECRET_ACCESS_KEY`; each credential accepts a
matching `_FILE` variable. Region defaults to `us-east-1`, and the bucket
defaults to the existing `audit-archive`. The storage adapter can access only
that configured bucket, checks that it already exists, and never creates it.
Provision its S3 policy separately with `s3:ListBucket` for the existence check
and `s3:PutObject` on archive objects. It does not create buckets or read
general Assets or application storage credentials.

Each archive operation holds one database transaction through snapshot,
object upload, partition removal, and the archive event. The transaction commits
only after the full operation succeeds; failures roll back database changes and
leave the partition available for retry. The same month uses the existing
deterministic archive key. S3 calls have five-second connect and 60-second read
timeouts.

The container runs non-root and exposes no port. Its default command is help;
the Compose service is behind the `maintenance` profile and defaults to the
non-destructive partition check. To run it locally:

```sh
docker compose -f infra/docker/docker-compose.audit.yml --profile maintenance run --rm audit-maintenance ensure-partitions
docker compose -f infra/docker/docker-compose.audit.yml --profile maintenance run --rm audit-maintenance refresh-daily
docker compose -f infra/docker/docker-compose.audit.yml --profile maintenance run --rm audit-maintenance archive-partitions
```

Compose passes direct environment values. Deployments using `_FILE` variables
must mount those secret files into the container at the configured paths.

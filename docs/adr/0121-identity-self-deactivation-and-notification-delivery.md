# 0121 — Identity 自助注销与通知投递所有权

- Status: Accepted; implementation is in progress and runtime qualification is pending
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: None
- Extends: ADR-0118, ADR-0119 and ADR-0120

## Context

Self-deactivation already has a seven-day cooling period and preserves three
`User.deactivation_*` fields as the source of truth. The request, cancellation,
daily due worker, Audit and Notifications were previously coordinated through
API ORM paths. Administrative suspend/reactivate must not cancel an accepted
self request, and the final enabled super-admin must not be deactivated by a
race between request acceptance, role/lifecycle changes and the due worker.

The current due worker scans all due rows under `SKIP LOCKED` and returns one
aggregate count. A single page size cannot become a lifetime work cap after
moving that processing across a portable command boundary; each committed page
needs a stable replay ID and an explicit stop-on-no-progress rule. Deactivation
notifications also need to be committed with the business result without
reusing the existing best-effort Redis path. The existing Audit redaction is
partial and must not be expanded or described as full account erasure.

This is one Identity lifecycle slice plus a Notifications-owned durable delivery
capability. It does not extract administrative user directory, invitations,
all offboarding, or Identity as a complete module.

## Decision

### User command and compatibility boundary

Keep the existing public routes and `UserOut` response:

- `POST /api/v1/me/deactivation-request`
- `DELETE /api/v1/me/deactivation-request`

The API composes them with private `POST /v1/identity/deactivation/execute`,
using the closed `aap-identity-deactivation/v1` protocol:

```text
{schema, operation, operation_id, token, audit_context, payload}
```

The request contains a UUID operation ID, a non-empty user JWT/API key of at
most 16,384 characters, and the existing four nullable Audit context fields.
The closed operations are:

| Operation                   | Payload                                            | Success data               |
| --------------------------- | -------------------------------------------------- | -------------------------- |
| `request_self_deactivation` | `{reason: string or null}`; at most 500 characters | `{account: AccountRecord}` |
| `cancel_self_deactivation`  | `{}`                                               | `{account: AccountRecord}` |

All four active platform roles may request or cancel their own deactivation.
The request preserves Python `str.strip()` and the seven-day UTC schedule.
Reason is not returned in command responses, Audit details or Notifications
payloads. The response account ID must match the actor; there is no notifications
array in the result.

Success is `{schema,ok:true,operation,operation_id,principal,data}` with the
current fenced Principal and `{account}` data. Failure uses the common closed
failure envelope. Additional codes are `account_busy`, `invariant_busy`,
`operation_conflict` and `policy_rejected`; non-policy failures do not return
native diagnostics. The fixed policy messages are:

- `已有进行中的注销申请` — request already pending;
- `当前无待生效的注销申请` — nothing to cancel;
- `您是最后一名超级管理员，请先指定继任者再申请注销` — no other active SA;
- `其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者` —
  other active SAs exist but none is unreserved.

The native schema is `aap_identity_deactivation_v1`, with public
`deactivation_policy()` and `execute_deactivation_command(jsonb)`. The
non-inheriting `identity_deactivation_client` LOGIN can execute exactly these
two functions and has no table, cross-owner, operator or invariant-helper
permission. Identity validates all wire and native values inside the same
transaction that commits User state, Audit, notifications and the successful
receipt.

Native `aap-identity-deactivation-native/v1` replaces the bearer with `actor`,
an `expected_key_hash` only for API-key credentials, a 64-character lowercase
request fingerprint and derivation ID, plus operation, operation ID, payload
and Audit context. The raw token, context metadata and credential hashes do not
enter the idempotency scope. The derivation master is
`HMAC-SHA256(secret UTF-8, ASCII "aap-identity-deactivation/v1")`; the
derivation ID is `SHA256(master).hex`, and the request fingerprint is HMAC-SHA256
of canonical compact sorted-key `ensure_ascii` JSON for `{operation,payload}`.
Current credentials are reauthenticated before receipt replay; a replay returns
its frozen AccountRecord but never rewinds newer deactivation fields or repeats
effects. Policy failures are bound to the same actor/operation/request scope
before acceptance and create no success receipt. Native execution acquires the
invariant guard, then `User FOR UPDATE NOWAIT`, then checks current
JWT/JTI/generation or API-key hash/scopes/expiry; it never locks a key before its
User row.

The API can select `identity_deactivation_database_url` / file; the Identity
runtime reads `deactivation_database_url` / file. Both environment pairs use
`IDENTITY_DEACTIVATION_DATABASE_URL(_FILE)`. Omitting this optional capability
in a selected runtime returns 503 for that route. Explicit remote or DSN errors
do not fall back to a local ORM writer.

### Shared final-super-admin invariant

The Identity code owner provides two private helpers in `aap_identity_v1`:

- `lock_super_admin_invariant()` tries a fixed, versioned transaction-scoped
  advisory lock and returns false immediately if another reducer holds it;
- `super_admin_status(uuid)` returns the closed
  `aap-identity-super-admin-status/v1` value with non-negative
  `other_active` and `other_unreserved` counts. The UUID is excluded, and null
  excludes nobody. An unreserved SA is active with both
  `deactivation_requested_at` and `deactivation_scheduled_at` null.

Both helpers are SECURITY DEFINER with a fixed `search_path`, and PUBLIC execute
is revoked. Only the Identity code owner has the narrow User column privileges
needed to implement them. The approved legacy API role can execute exactly these
two helpers in addition to its existing allowlist; AUTH, GROUP, self-command,
operator and notification publisher logins receive no invariant-helper grant.

All writes that can reduce or change the lifecycle of an active super-admin
take the same fence before acquiring sorted User row locks: self request/cancel,
the due worker, legacy role changes, delete/deactivate, offboarding/emergency
suspend and reactivate. Legacy role preview calls the status helper so preview
and apply see the same blocker. Admin password reset does not acquire this
global fence. It does not serialize unrelated login, group, profile or presence
commands. A busy fence returns safe 409; the transaction holds it through final
state, Audit, Notification and receipt commit. The API runtime receives only
these two helpers in addition to its already approved functions; AUTH/GROUP and
dedicated command clients do not receive them.

A super-admin self request requires at least one other active SA. If there are
none, it uses the existing final-admin 400. If other active SAs all have
pending requests, it uses the new explicit 400 rather than accepting a request
that leaves no unreserved successor. Legacy role/lifecycle reducers retain
their operation-specific 400 strings while requiring another unreserved SA.

### Accepted request state and paged due execution

An accepted request survives administrative suspension and reactivation, as it
does today. Only explicit self-cancellation or successful due completion clears
the three request fields. A correlation ledger retains request UUID, user UUID,
request/schedule timestamps and state without a live-User FK; it backfills valid
existing pending state and reconciles later legacy-seeded values. It does not
invalidate valid pending requests or impose a new total due cap. An old cancelled
receipt cannot cancel a newer request.

The user operation receipt freezes every successful request/cancel AccountRecord
and its protocol-derived scope. Due receipts freeze every page outcome in an
operator scope including limit, native operation identity and version; neither
ledger contains a raw bearer or a live User foreign key. Implementations may use
separate user/operator receipt tables or one exact table with equivalent scope
separation. A downgrade must be refused while stored native effects, receipts or
live delivery state would be lost; migration-backfilled correlation can be
dropped only if the original three fields still preserve all pending behavior.

Due processing uses private `POST /v1/identity/deactivation/process-due` with
`aap-identity-deactivation-due/v1`. It accepts only the control Bearer and the
closed request `{schema,operation_id,limit}`. It accepts no user token, caller
user IDs or caller clock. `limit` is a strict integer from 1 to 500 and defaults
to 256. The success value is `{schema,ok:true,operation_id,processed,examined,
blocked_last_super_admin,has_more}` with non-negative strict counts,
`processed + blocked_last_super_admin = examined <= limit` and a strict boolean
`has_more`. It returns no user PII or Notification payload. Its closed errors are
`invalid_request`, `invariant_busy` and `operation_conflict`.

The operator `identity_deactivation_operator` LOGIN can execute exactly
`due_policy()` and `process_due_deactivations(jsonb)`; it has no self-command,
AUTH, Notification or table access. The optional runtime DSN pair is
`IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL(_FILE)`; a selected but missing
capability returns 503 and an explicit connection failure never falls back.

Keep task name `app.workers.deactivation.process_deactivation_requests` and
the daily 04:00 UTC schedule. The Celery request UUID is the stable run ID; page
IDs are `uuid5(run_id, "batch:N")`. On retry, replay committed page receipts
first, then claim subsequent pages. A run drains until no progress or
`has_more=false`, so page size is not a total cap. Busy/unavailable retries reuse
the run/page identity. A last-SA block or currently locked row is not reported
as a permanent failure or discarded; no-progress ends the current run, and the
next scheduled run uses a fresh ID.

The due owner orders candidates by UUID and locks User rows with `SKIP LOCKED`.
Immediately before disabling a super-admin it rechecks the number of other
actually active SAs. If none remains, it leaves request fields and ledger
pending, increments `blocked_last_super_admin`, and writes no approval Audit or
completion notification for that user. Successful due work is only the
existing soft deletion: inactive/offline, `disabled_kind=deleted`,
`disabled_by=self`, reason equal to the previous reason or
`自助注销申请到期`, and clearing the three request fields. It does not advance
credential generation, revoke API keys or transfer projects/tasks. Administrative
offboarding credential and task-transfer effects remain separate.

Due receipts freeze every page, including `processed=0`; the operation scope
includes page limit, native operation identity and protocol version but no
secret. A zero page is still idempotent: replaying its operation ID does not
process users that became due later. Native/test-seeded pending fields remain
authoritative and must be reconciled when due or cancellation observes them.

### Audit boundary

Identity code calls only `aap_audit_v1.record_identity_deactivation_event(text,
jsonb,jsonb,jsonb)`. The fixed events target `{target_type:user,
target_id:actor_id}` and use status 200:

| Event                       | Detail                                                            |
| --------------------------- | ----------------------------------------------------------------- |
| `user.deactivation_request` | `{scheduled_at: aware finite timestamp, reason_present: boolean}` |
| `user.deactivation_cancel`  | `null` or `{}` to preserve legacy rows                            |
| `user.deactivation_approve` | `{email,role,scheduled_at: timestamp or null,auto:true}`          |

Actor is the closed `{actor_id,email,role}` value and context is the existing
four nullable request fields. Approval inserts its event, then a narrowly
authorized local bypass nulls only existing `actor_email` and `actor_role` for
that actor's Audit history. `detail_json`, historic Notification payloads and all
other profile data remain. This is the pre-existing partial redaction
semantics, not full GDPR erasure or a general Audit-table grant.

### Notifications-owned delivery outbox

Native artifact `0241_deactivation_notifications.sql` loads before
`0241_identity_deactivation.sql` in migration 0241. Notifications owns
`notification_delivery_outbox`, keyed by Notification UUID with operation /
recipient / type dedup, pending/done state, attempt, availability, worker and
lease identities, lease expiration and a safe `last_reason`. Its Notification
FK uses `ON DELETE CASCADE`, so deleting an inbox row before claim suppresses
the delivery. It is not a generic job ledger and adds no parent/job dependency.

Identity passes current active-super-admin recipient IDs to
`aap_notifications_v1.create_identity_deactivation_notifications(uuid,text,
jsonb,uuid[])`. It creates only `user.deactivation_requested` and
`user.deactivation_completed` with target `user`. The request event includes the
requester if they are an SA; completion excludes the subject. Request payload is
exactly `{email,name,role,scheduled_at}`, completion is `{email,name,auto:true}`.
Notifications may narrowly recheck recipient actor facts but does not get
direct access to Identity/User tables. The database owner applies the current
Python truthiness of `channels.in_app`: a missing channel defaults true; null,
false, zero, empty string, list or object is muted. No reason, project scope,
generic event or arbitrary caller payload is accepted.

Publisher wire is `aap-notifications-delivery/v1`. The separate
`notification_publish_client` LOGIN has exactly
`identity_delivery_policy()`, `claim_identity_deliveries(jsonb)` and
`finish_identity_delivery(jsonb)` and no AUTH, mail, Identity, signing-secret,
table or other-business function access. Claims use a committed lease before
network I/O; claim pages contain 1–100 rows (default 16), leases default to 60
seconds, worker concurrency is four and Redis I/O timeout is at most five
seconds. No lease renewal is needed for that bounded batch. Finish accepts only
`delivered` or `retry` with a safe reason, comparing all lease identity and the
current DB time. Redis must accept the existing notification message before
acknowledgement. Retries and lease takeover keep the same Notification ID;
delivery is at-least-once and can repeat a broker message.

WebSocket publication uses channel `notify:{user_id}` and the existing message
keys except `user_id`, because the channel identifies the recipient. The same
portable executor is available as `NotificationDeliveryExecutor.run_once()`.
The standalone non-HTTP process runs
`python -m aap_identity_runtime.notification_worker` and
`aap_identity_runtime.notification_config.NotificationPublisherSettings` reads
only `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` and
`NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`. It needs no control bearer, AUTH or
JWT signing secret. The default API queue also relays through that executor
every ten seconds using `notification_publish_database_url` and existing task
Redis settings; it does not run a parallel ephemeral publish path.

## Configuration and deployment

Optional API and Identity inputs are separate from Compose host selectors:

| Purpose                      | API / Identity direct or file values                 | Compose host file selector                                 |
| ---------------------------- | ---------------------------------------------------- | ---------------------------------------------------------- |
| Self commands                | `IDENTITY_DEACTIVATION_DATABASE_URL(_FILE)`          | `IDENTITY_RUNTIME_DEACTIVATION_DATABASE_URL_FILE`          |
| Due operator                 | `IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL(_FILE)` | `IDENTITY_RUNTIME_DEACTIVATION_OPERATOR_DATABASE_URL_FILE` |
| API/Celery Notifications SQL | `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)`           | `IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_DATABASE_URL_FILE`  |
| Standalone publisher Redis   | `NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`              | `IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE`     |

Runtime direct configs use the same `IDENTITY_DEACTIVATION_*` names. The
standalone notification publisher maps only its own two files to
`NOTIFICATION_PUBLISH_DATABASE_URL_FILE` and
`NOTIFICATION_PUBLISH_REDIS_URL_FILE`; it receives no Identity database DSN,
AUTH/control token, JWT signing secret or SMTP settings. `identity-admission`
receives only the self and due Identity function DSNs. The three dedicated
LOGINs are installed from `infra/docker/identity-deactivation-grants.sql`;
legacy helper grants belong to the existing `module-data-grants.sql` and
`lan-production-grants.sql` sources. All owner functions must share one
PostgreSQL transaction owner. The optional Compose profile uses the same
Identity artifact, non-root read-only processes and explicit secret files.

## Consequences and remaining scope

The shared fence keeps the final-super-admin invariant across self-service and
legacy lifecycle writers without globally serializing unrelated auth, group,
profile or presence work. Paged receipts keep arbitrarily many due requests
recoverable without one unbounded SQL transaction or an artificial total cap.
Notifications owns durable storage and publication while preserving the two
legacy event types and in-app preferences.

This decision does not finish invitations, user directory, platform-role
administration, other-user hydration, admin/offboarding ownership, whole Identity
deployment, provider-language replacement or production capacity qualification.
It does not expand partial Audit redaction into erasure of `detail_json` or
historic notification payloads. The deactivation role/consumer/failure
qualification is separate from this design record and remains pending until the
actual Root acceptance evidence is recorded.

## Alternatives considered

- Clear a pending self request when an administrator suspends/reactivates the
  account: rejected because an accepted request already survives those changes.
- Check only the super-admin count when a request is created: rejected because
  role changes, offboarding and due completion can race and leave no actual
  active administrator.
- Reuse the one-page due limit as a total cap: rejected because a large backlog
  would permanently strand valid requests.
- Send Notifications after the Identity commit via the old best-effort Redis
  path: rejected because a crash between SQL commit and publish would lose the
  reminder. The Notifications outbox makes only these two events durable and
  leaves other notification paths unchanged.

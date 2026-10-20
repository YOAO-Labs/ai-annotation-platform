# 0127 — Identity 管理员删除账号所有权

- **Status:** Accepted; locally qualified slice (2026-10-20)
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0075, ADR-0121 and ADR-0126

## Context

The legacy API implementation of `DELETE /api/v1/users/{user_id}` combined
account locking, manager scope, task transfer, TaskLock release, soft-deletion
metadata, Audit and historical actor redaction. Moving only account status
would split those effects or change the existing transfer and audit behavior.

This slice moves only the administrator account-delete command to the existing
Identity boundary. Offboarding preview/commit, registration-status settings,
dashboard hydration, provisioning and avatars remain separate. The public
operation stays a soft delete; it does not retire credentials.

## Decision

### Preserve the public contract and lock order

Keep `DELETE /api/v1/users/{user_id}`, its string path and malformed-UUID 404,
optional/null `DeleteUserPayload`, optional/null UUID `transfer_to_user_id`,
ignored public extra fields, current manager roles, errors and `UserOut`. Every
public call receives a fresh private operation UUID; there is no public CAS or
idempotency header. The result remains the existing 21-field
`DirectoryUserRecord`, including opaque preferences, historical role/status
and nullable metadata.

Parse the target UUID, take the shared last-super-admin fence, then lock actor,
target and optional receiver User rows with `FOR UPDATE NOWAIT` in UUID order.
Under those locks, reauthenticate the current credential and active manager
through the existing Account helper. Current manager authority is checked before target absence and self-delete;
after revalidation, target absence returns 404 before self-delete returns 400.
Preserve the opaque 1–255-character
`expected_key_hash`; never trust a caller-asserted actor.

After the missing-target and self-delete checks, an inactive target returns
the complete current User record immediately. This is a successful no-op before
target-role, project, receiver or work validation, Audit, transfer and metadata
changes. An absent or wrong-role receiver is therefore ignored on this branch. Store a
receipt for the accepted no-op so a late retry cannot turn it into a new
deletion.

For an active target, lock every derived Project with `FOR KEY SHARE NOWAIT`.
The derived set includes target-owned Projects, every target ProjectMember,
TaskBatch annotator/reviewer references, direct Task assignee/reviewer
references and TaskLocks joined through Task; include all statuses and do not
use a TaskBatch fallback for Task assignment. Null/orphan references disappear
through the Project join. A project-admin may delete only an employee; an empty
derived set is allowed and every derived Project must be actor-owned. Do not
add a ProjectMember requirement. The legacy active-employee
manages-target check remains satisfied in this branch. After project scope,
retain the existing last-unreserved-super-admin check: another active super-admin with both
self-deactivation request and schedule unset must remain.

### Preserve work blocking and transfer semantics

Work blockers count only direct `Task.assignee_id` assignments in
`pending`, `in_progress`, `review` or `rejected`; reviewer assignments,
TaskBatch fallback, inherited assignment and finished tasks do not count.
Count all target TaskLocks, including expired locks. Return at most five
matching task UUIDs ordered by `created_at DESC`.

When no receiver is supplied and either count is positive, return 409
`has_pending_tasks`. Its detail contains `reason`, `pending_task_count`,
`locked_task_count`, `sample_task_ids` and `message`; the message remains exactly
`该用户当前有 {pending} 个未完成任务、{locks} 个锁定任务，需先转交`. With a
receiver, validate it even when both counts are zero: it must be active, differ
from the target and have role `employee` or `project_admin`. For a
project-admin actor, the receiver must be a member of every distinct nonnull
Project containing a pending direct-assignee task unless the receiver is the
actor. Do not impose a member-role rule or owner fallback.

Transfer only eligible `Task.assignee_id` rows and set both `assigned_at` and
`updated_at` to the current time, preserving the old ORM `onupdate` effect; then
release all target TaskLocks. Leave reviewer assignments, TaskBatch ownership,
ProjectMember, Project ownership, other Task statuses and their timestamps
untouched. Do not introduce a new Task/Batch lock regime. Keep the transfer,
account update, Audit, redaction and receipt in one caller transaction.

### Delete without retiring credentials; preserve Audit behavior

For an active target, set `is_active=false`, `status=offline`,
`disabled_kind=deleted`, `disabled_at` to current UTC, `disabled_by` to the
actor and `disabled_reason` exactly `管理员删除账号`. Do **not** increment
credential generation or change API-key `revoked_at`, reset-link `used_at` or
JWT/JTI state. Account inactivity continues to govern admission; this command
does not revoke credentials as administrator suspension/reactivation does.
Preserve accepted self-deactivation request, schedule and reason unchanged.

The typed Audit owner first redacts `actor_email` and `actor_role` on all prior
Audit rows for the deleted target and captures the row count; it then inserts
one fixed `USER_DELETE` event in the same transaction. The new event keeps the
existing detail fields `email`, `role`, `transferred_to` (UUID string or null),
`transferred_count` and `released_locks` (the pre-transfer count when a receiver
was supplied, otherwise zero). Add `redacted_audit_rows` only when at least one
row was redacted, embedded in the INSERT. The helper must preserve Audit's
least-privilege UPDATE boundary: it may update `actor_email` and `actor_role`,
but must not gain UPDATE access to `detail_json`. The API already forbids
actor-equals-target; the owner helper enforces this precondition. Retain prior
`actor_id`, `detail_json`, IP and target fields. This is partial historical
actor redaction, not complete personal-data erasure.

### Private protocol and receipts

The private endpoint is `POST /v1/identity/deletions/execute`, wire schema
`aap-identity-deletions/v1`, native schema
`aap-identity-deletions-native/v1`, policy schema
`aap-identity-deletions-policy/v1` and policy value
`aap-identity-deletions/v1`. The only operation is `delete_account`.

The wire request has exactly `schema/operation/operation_id/token/audit_context/
payload`; payload is exactly `{target_user_id,transfer_to_user_id}`. The latter
key is always present and null when omitted publicly. The native request has
exactly `schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/
derivation_id/audit_context/payload`, replacing the raw token with the
authenticated Principal and opaque credential binding. Fingerprint normalized
UUIDs and the operation payload using the distinct HMAC domain
`aap-identity-deletions/v1`; exclude Audit context. Wire success is the closed
six-key envelope `schema/ok/operation/operation_id/principal/data`; native adds
strict `replayed:boolean`. Data is exactly `{user: ...}`, matching the target
and showing it inactive. An inactive no-op must not be assigned a synthetic
deleted kind.

Portable interfaces are `DeletionRequest/Reply/Failure`,
`DeletionManager(auth,data,secret).execute` and `DeletionPostgresData(dsn)`;
runtime exposes `deletion_policy()` and `execute_deletion_command(jsonb)`.
Canonical files are `deletion_contract.py`, `deletion_manager.py`,
`deletion_postgres.py`, `deletion_protocol.schema.json` and
`native_deletion_protocol.schema.json`.

Receipts freeze actor, operation UUID/name, target, optional receiver, request
fingerprint, derivation, accepted User record, nullable Audit ID and target
generation/active state. They contain no raw token, credentials, hash, Principal
or live User foreign key. Replay reauthenticates the current manager, checks
receipt binding and compares target generation/active state before new target
policy. A mismatch returns 409 `operation_conflict`. Since either accepted
branch leaves the target inactive, a matching replay returns the frozen result
without repeating transfer, TaskLock release, metadata, Audit or redaction; it
does not process work or locks added after acceptance. Credentials and new
Audit rows added after acceptance remain untouched. Populated receipts block
downgrade.

The closed business error pairs are:

| Code                       | HTTP | Detail                                   |
| -------------------------- | ---: | ---------------------------------------- |
| `invalid_request`          |  400 | `null`                                   |
| `manager_required`         |  403 | `当前账号已无管理权限`                   |
| `target_not_found`         |  404 | `用户不存在`                             |
| `self_delete`              |  400 | `不能删除自己`                           |
| `delete_role_forbidden`    |  403 | `项目管理员仅能删除员工账号`             |
| `delete_scope_forbidden`   |  403 | `该用户跨多个项目，须由超级管理员处理`   |
| `last_super_admin`         |  400 | `不能删除最后一名超级管理员`             |
| `has_pending_tasks`        |  409 | Closed work-count detail described above |
| `receiver_unavailable`     |  400 | `转交目标用户不存在或已禁用`             |
| `receiver_is_target`       |  400 | `转交目标不能与被删用户相同`             |
| `receiver_role_invalid`    |  400 | `转交目标角色不合法`                     |
| `receiver_scope_forbidden` |  403 | `转交目标必须是待转交任务所在项目的成员` |
| `account_busy`             |  409 | `账号正在更新，请刷新后重试`             |
| `project_busy`             |  409 | `{reason:project_busy}`                  |
| `operation_conflict`       |  409 | `null`                                   |
| `unavailable`              |  503 | `null`                                   |

Existing Identity authentication failures keep their established codes with
null status/detail. Public mapping remains unchanged; after an admitted actor
becomes inactive, private `user_inactive` maps to the existing public manager
403, while initial admission with inactive credentials remains 401. Reject
duplicate/alias/extra/misbinding/secret-echo fields and malformed owner replies
inside the transaction so failures roll back without fallback.

### Migration, support capabilities and configuration

Migration 0247 creates schema `aap_identity_deletions_v1` and
`public.identity_deletion_operation_receipts`. The noninheriting
`identity_deletion_client` LOGIN can execute
only `deletion_policy()` followed by `execute_deletion_command(jsonb)`; it
receives no table, sequence, DDL, foreign-schema or other protected-function
rights. Grant allowlists follow actual proname order.

Only the Identity owner invokes the typed support capabilities:

- Workspace `lock_identity_deletion_scope(actor_uuid,target_uuid)` returns one
  nonnull `(has_projects boolean,all_projects_owned boolean)` row and uses the
  same derived project set with `FOR KEY SHARE NOWAIT` locks.
- Labeling `identity_deletion_work(target_uuid)` returns exactly one row:
  `(pending_task_count bigint,locked_task_count bigint,sample_task_ids uuid[],
pending_project_ids uuid[])`.
- Workspace `identity_deletion_transfer_covered(receiver_uuid,project_uuids)`
  returns a nonnull boolean for full `ProjectMember` coverage.
- Labeling `transfer_identity_deletion_work(from_uuid,to_uuid)` performs the
  direct-task reassignment and TaskLock release, returning a nonnull bigint.
- Audit `record_identity_deletion_event(actor_json,target_uuid,detail_json,
context_json)` writes the fixed event and performs partial actor redaction,
  returning its Audit ID as a bigint.

There are no raw cross-owner grants, Task JSON snapshots or generalized lock
framework. The existing unfenced Labeling removal race is outside this owner.
API/runtime use optional `IDENTITY_DELETION_DATABASE_URL(_FILE)` and optional
Compose host selector `IDENTITY_RUNTIME_DELETION_DATABASE_URL_FILE`. A blank
selector disables only this capability and leaves existing Identity startup
available; an explicitly selected DSN must pass readiness and never falls
back. Migration, grants and local qualification pass as documented below; no
production-capacity claim is made.

### Local qualification

Root final evidence at source revision `edb213550c81439f5707f531f82bce31d236cbe2` passed 72 selected acceptance cases with zero skips: 71 database/process cases and one static-artifact case. The aggregate uses the latest result per case; focused restore and dedicated-API passes replace earlier fixture failures, and reruns are not additive counts. The set includes compatibility regressions and is not 72 new DELETE-only tests. Migration 0247 downgrade to 0189, frozen-SQL rollback and restoration to 0247 with grants passed. All owned resources were cleaned up with no cleanup errors.

Portable source and installed-package suites each passed 502 tests; 104 installed package files have exact hashes. The installed package uses Pydantic 2.14.1 and has no API, SQLAlchemy, Celery, Boto3 or Torch dependency. API assembly passed 576 checks with 129 architecture rules explicitly deferred; Node passed 21 self-checks and SDK passed 35. OpenAPI changed only the DELETE operation description; public paths, schemas and fields remain unchanged. Codegen/catalog/schema checks passed, and environment docs contain 459 variables across 48 sections.

The Identity owner image `sha256:4e4d71b3a573fba29758b6a4563ad18a97c25fec0b283a4d115df1c4423a73c0` passed an actual remote API binding in a nonroot, read-only container limited to 1 CPU/256 MiB. Four live cases passed: installed-owner transfer/replay, project-admin scope/receiver coverage, an AUTH-only API binding to the remote Identity container, and a dedicated-DSN API-key binding.

The API cold image `sha256:82aa52c6c31721c449b7fe9bcc1046a47956250a83b8284c798f265799dae997` passed with networking disabled, a read-only filesystem and 1 CPU/512 MiB. It matched 104 package and 104 API/native input hashes; verified migration head 0247, one DELETE route, existing S/R (2), platform-role (2), admin-password (1), directory (4), invitation (13), recovery (5), group (8) and deactivation (2) routes, current OpenAPI, and absence of retired helpers. The temporary container was removed. Its 139.297 seconds measure total image build plus offline verification, not application startup latency.

The finite host-process sample used 12 targets at concurrency four: 34 attempts, 22 busy retries, 861.920 ms elapsed, p50 458.037 ms and p95 856.836 ms. It includes Node/HTTP host overhead and is not container CPU timing, production capacity or an SLO. Confirmation left all 12 receipts and 12 Audit rows unchanged; work, locks, Audit rows and credentials created after acceptance remained untouched by replay.

Native regression also proved that a post-write typed-helper failure rolls back the direct-SQL transfer/deletion and manager transaction. If a request was admitted as super-admin but the account is now project-admin, execution uses the current project-admin scope and preserves its 403 failure; actor-ID and generation fences were not weakened, and protocol schemas did not change. This qualifies only the DELETE owner. Offboarding, registration settings, dashboard hydration, complete Identity, the ten-module/seven-gate objective, production capacity/cutover and provider-language replacement remain incomplete.

## Consequences

Positive:

- Identity owns the account state and durable replay receipt while preserving
  current API behavior and its single-transaction transfer/Audit boundary.
- Deletion no longer duplicates the legacy account writer; shared task-lock
  release used by offboarding remains available.
- Credential retirement remains distinct: DELETE preserves its current
  credential state, unlike administrator suspension/reactivation.

Negative:

- Work eligibility, receiver visibility, lock order and error precedence remain
  compatibility-sensitive and require focused regressions.
- Prior Audit detail and IP remain stored; this partial redaction is not a
  general erasure guarantee.
- This owner does not complete offboarding, other-user hydration, registration
  settings, full Identity, the ten-module/seven-gate objective, production
  capacity or provider-language replacement.

## Notes

- Public route remains in `apps/api/app/api/v1/users.py`. Composition is
  `apps/api/app/api/v1/deletion_commands.py`; caller adapters are
  `apps/api/app/services/identity_deletion_commands.py` and
  `apps/api/app/services/identity_deletion_data.py`.
- The portable implementation is in `apps/identity/src/aap_identity/` and its
  runtime package. The duplicate legacy delete writer, Batch transfer helper
  and Audit redaction helper have been retired; shared
  `release_user_task_locks` remains for offboarding.

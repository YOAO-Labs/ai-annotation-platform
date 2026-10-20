# 0127 — Identity 管理员删除账号所有权

- **Status:** Accepted; implementation in progress, local qualification pending
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0075, ADR-0121 and ADR-0126

## Context

`DELETE /api/v1/users/{user_id}` currently combines account locking, manager
scope, task transfer, TaskLock release, soft-deletion metadata, Audit and
historical actor redaction in the API. Moving only the account status would
split those effects or change the existing transfer and audit behavior.

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

Transfer only the eligible `Task.assignee_id` rows and set their
`assigned_at` to the current time; release all target TaskLocks. Leave reviewer
assignments, TaskBatch ownership, ProjectMember, Project ownership and all
other Task statuses untouched. Do not introduce a new Task/Batch lock regime.
Keep the transfer, account update, Audit, redaction and receipt in one caller
transaction.

### Delete without retiring credentials; preserve Audit behavior

For an active target, set `is_active=false`, `status=offline`,
`disabled_kind=deleted`, `disabled_at` to current UTC, `disabled_by` to the
actor and `disabled_reason` exactly `管理员删除账号`. Do **not** increment
credential generation or change API-key `revoked_at`, reset-link `used_at` or
JWT/JTI state. Account inactivity continues to govern admission; this command
does not revoke credentials as administrator suspension/reactivation does.
Preserve accepted self-deactivation request, schedule and reason unchanged.

Write one typed `USER_DELETE` Audit event with the existing detail fields:
`email`, `role`, `transferred_to` (UUID string or null),
`transferred_count` and `released_locks` (the pre-transfer count when a receiver
was supplied, otherwise zero). Redact `actor_email` and `actor_role` on all
prior Audit rows for the deleted target; retain `actor_id`, `detail_json`, IP
and target fields. Add `redacted_audit_rows` to the deletion Audit detail only
when at least one row was redacted. This is partial historical actor redaction,
not complete personal-data erasure.

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
back. Migration, role grants and full local qualification are still being
verified; no production-capacity claim is made.

### Initial source checks; local qualification pending

Root's initial source checks passed 494 portable source-package tests and 576
API assembly checks, with 129 architecture rules explicitly deferred. The SDK
passed 35 checks. OpenAPI changed only the DELETE operation description; public
paths, schemas and fields remain unchanged. These checks do not qualify the
owner: database/restore, installed-package, live API, owner-image and cold-image
evidence are still pending. No database, live, installed-package, image,
production-capacity or cutover result is claimed here.

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
  runtime package. Remove only the obsolete legacy delete writer and helpers
  after caller checks; retain shared `release_user_task_locks` while offboarding
  still uses it.

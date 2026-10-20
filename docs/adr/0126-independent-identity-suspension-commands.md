# 0126 — Identity 管理员停用与恢复账号所有权

- **Status:** Accepted; source implementation is in progress and local qualification is pending
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0115, ADR-0121, ADR-0124 and ADR-0125

## Context

`POST /api/v1/users/{user_id}/deactivate` and
`POST /api/v1/users/{user_id}/reactivate` currently compose account locks,
manager scope, credential retirement, status metadata and Audit in the API.
Moving only `is_active` and `status` would leave historical no-op ordering,
project-scope differences, the last-super-admin reservation fence, credential
retirement and replay behavior split across owners.

This owner slice moves only these two administrator operations into the
existing Identity artifact. It does not move `DELETE`, offboarding
preview/commit, provisioning, unrelated profile hydration or task/project
handoff. Self-service deactivation remains a separate workflow.

## Decision

### Preserve the public contract

Both routes remain `POST`, super-admin/project-admin-only, with their current
bodies, errors, `UserOut` response and SDK/Web behavior. No public CAS or
idempotency header is added; every public invocation gets a new private
operation UUID.

- Deactivation keeps a string `user_id` path parameter, no request body and
  malformed UUID 404 behavior.
- Reactivation keeps a UUID `user_id` path parameter, so malformed UUIDs retain
  422 validation behavior. Its body remains optional/null
  `ReactivateRequest`, with optional/null `reason` up to 500 characters and the
  current ignored-extra-fields behavior.

The current access policy is preserved rather than normalized or broadened.
Neither operation hydrates its target through the User ORM after migration.

### Shared fence, credentials and project scope

Both operations first take the existing shared last-super-admin invariant,
then lock actor and target User rows with `FOR UPDATE NOWAIT` in UUID order.
Only after those locks, call the same-owner Account credential helper
reentrantly at the operation-specific manager-check point; preserve
deactivation's manager-before-missing order and reactivation's
missing-before-manager order. Verify current active manager, JWT
generation/JTI/expiry or current API-key hash/state/scopes. A Principal's
role/name/email/scopes may change since admission without making it malformed;
bind credential identity, generation and expiry. Preserve the opaque
1–255-character `expected_key_hash`. Global/account/credential contention
returns 409 `account_busy` with `账号正在更新，请刷新后重试`.
Initial public admission still rejects inactive credentials with 401. If the
already-admitted actor becomes inactive before the owner executes the command,
the public adapter preserves the legacy 403 `当前账号已无管理权限`; the private
`user_inactive` authentication code remains unchanged.

The existing derived-project set is the distinct Projects referenced by target
ownership, any target `ProjectMember`, `TaskBatch` annotator/reviewer, direct
Task assignee/reviewer, or `TaskLock.user_id` joined through Task. Include all
statuses; use no TaskBatch fallback for Task assignments; include unbatched
Tasks when they join to a Project; null/orphan references disappear through
the existing Project join.

Workspace exposes one narrow typed
`aap_workspace_v1.lock_identity_suspension_scope(uuid,uuid,text)` function,
returning exactly one nonnull row `(has_projects boolean,
all_projects_owned boolean)`. Its arguments are actor UUID, target UUID and
`suspend_account`/`reactivate_account`. It may reuse the existing Labeling
`identity_directory_project_refs(uuid[])` bulk reader internally; there is no
new Labeling schema, protocol, grant or Labeling-to-Workspace capability.

Preserve the existing project lock modes and branch behavior:

- Real suspension locks all derived Project rows with `FOR KEY SHARE NOWAIT`.
  A project-admin must target an employee and all derived Projects must be
  actor-owned, but an empty set is allowed. The current active-employee
  manages-target helper is always true in this branch; do not add a membership
  requirement. An empty set yields `(has_projects=false,
all_projects_owned=true)`. Project lock contention returns 409 `project_busy` with
  `{reason:project_busy}`.
- Project-admin reactivation requires an employee and a nonempty derived set.
  Its existing query locks only the actor-owned subset with `FOR SHARE NOWAIT`
  before comparing full coverage. Role and scope checks precede target active
  and suspension-kind checks. An empty set returns `reactivate_not_managed`;
  incomplete coverage returns `reactivate_scope_forbidden`. Lock failure
  returns `account_busy`.
- Super-admin reactivation skips project-scope checks and Project locks.

Do not add ProjectMember, TaskBatch, Task or TaskLock locks, a generalized
scope-lock framework or another Labeling wire protocol. The existing race
where a batch is cleared/reassigned away without locking its old User is an
inherited Labeling writer gap; this slice does not claim to fix it.

### Deactivation ordering and effects

After the shared locks and current-credential check, deactivation checks
manager authority, then target existence (404 `用户不存在`), then self-target
(400 `不能停用自己`). If the target is already inactive, return its complete
current 21-field User record unchanged immediately. This includes project-admin
requests for historical or deleted targets and happens before project scope or
target-role checks. It is a successful no-op: no Audit, no credential
retirement and no Project scope lock.

For a real suspension, lock the derived Projects as above. Project-admin then
requires an employee target and full ownership coverage; the empty project set
is valid. After scope, a super-admin target may be suspended only when
`super_admin_status(target).other_unreserved > 0`, counting active super-admins
with both deactivation request and schedule null. Preserve this error order.

Set `is_active=false`, `status=offline`, `disabled_kind=suspended`,
`disabled_at` to current UTC, `disabled_by` to the actor UUID and
`disabled_reason` exactly to `管理员停用账号`. Retire current account
credentials and write exactly one `user.deactivate` Audit with status 200,
target type `user`, target UUID and detail exactly `{email}`. Existing
`deactivation_requested_at`, `deactivation_scheduled_at` and
`deactivation_reason` remain unchanged; in particular, an accepted
self-deactivation reservation stays active across administrator suspend and
reactivate.

### Reactivation ordering and effects

After the shared locks, reactivation checks target existence (404
`用户不存在`) before fresh actor manager authority, then self-target (400
`不能恢复自己`). Project-admin requires an employee target, a nonempty derived
project set and ownership of every derived Project; it locks only the actor-owned
subset with `FOR SHARE NOWAIT` before comparing full coverage. Super-admin
skips this scope and all Project locks. Scope checks precede target-state
checks. An already-active target returns 409 `账号当前已启用`. Only
`suspended` and `emergency_suspended` targets are recoverable; other kinds
return 409 `该停用状态不可恢复`.

Retire credentials **before** changing account metadata, then set
`is_active=true`, `status=offline` and clear `disabled_kind`, `disabled_at`,
`disabled_by` and `disabled_reason`. Write exactly one `user.reactivate` Audit
with status 200, target type `user`, target UUID and detail exactly
`{reason_present:boolean}`. The portable manager determines presence with Python
Unicode `str.strip()` semantics and passes only the strict boolean to the native
command. Raw reason never enters PostgreSQL JSONB; this preserves accepted input
such as U+0000, which the legacy workflow used only for that boolean. Never store
or log the raw optional reason.

Every real suspension/reactivation uses the existing credential retirement
helper: advance generation once, revoke currently active API keys and consume
unused reset links as today. Old credentials never revive. A successful
reactivation replay must not retire keys issued after acceptance. No operation
changes project membership, batch ownership, Task assignment or task state.

### Versioned command, results and errors

The private endpoint is `POST /v1/identity/suspensions/execute` with wire
schema `aap-identity-suspensions/v1`; operations are `suspend_account` and
`reactivate_account`. Native schema is
`aap-identity-suspensions-native/v1`; policy schema/protocol are
`aap-identity-suspensions-policy/v1` and
`aap-identity-suspensions/v1`.

Wire request keys are exactly
`schema/operation/operation_id/token/audit_context/payload`. Suspend payload is
exactly `{target_user_id}`. Reactivate payload is exactly
`{target_user_id,reason}` with null/string reason up to 500 characters.
`SessionAuditContext` remains its four bounded fields. Hide bearer token in
repr/errors/logs. Native request has exactly
`schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/derivation_id/audit_context/payload`;
`actor` is authenticated Principal, never a request assertion.
Native suspension payload remains `{target_user_id}`; native reactivation payload
is exactly `{target_user_id,reason_present}` with a strict boolean derived by the
portable manager. The wire reason remains unchanged for fingerprinting.
`expected_key_hash` is nonnull only for API-key credentials and remains opaque
1–255 characters; the required field is null for JWT credentials. Fingerprint and derivation use the canonical wire operation/payload HMAC pattern with distinct
`aap-identity-suspensions/v1` domain, normalized UUIDs, untouched reason text
and audit context excluded. Portable interfaces are
`SuspensionRequest/Reply/Failure`, `SuspensionManager(auth,data,secret).execute`
and `SuspensionPostgresData(dsn)`; runtime policy exposes
`suspension_policy()` and `execute_suspension_command(jsonb)`. Canonical schema
files are `suspension_protocol.schema.json` and
`native_suspension_protocol.schema.json`.

Wire success is exactly `schema/ok/operation/operation_id/principal/data`; the
native envelope adds strict `replayed:boolean`. Data is exactly `{user:...}`
using the existing 21-field `DirectoryUserRecord` projection, preserving
opaque preferences, historical roles/status strings and nullable metadata.
Target ID must match the request; real suspend returns inactive, real
reactivate returns active. Do not force `disabled_kind=suspended` into an
already-inactive no-op result. Never include credentials, generation, hashes or
retirement IDs. Validate both native and final wire responses before commit.
Malformed, duplicate, extra, alias or secret-echo fields and principal/
operation misbinding roll back as `unavailable`; do not fall back to another
writer.

Known business failures are bound to operation and detail exactly:

| code                         | HTTP | detail                                 |
| ---------------------------- | ---: | -------------------------------------- |
| `invalid_request`            |  400 | `null`                                 |
| `manager_required`           |  403 | `当前账号已无管理权限`                 |
| `target_not_found`           |  404 | `用户不存在`                           |
| `self_suspend`               |  400 | `不能停用自己`                         |
| `self_reactivate`            |  400 | `不能恢复自己`                         |
| `suspend_role_forbidden`     |  403 | `项目管理员仅能停用标注员/审核员账号`  |
| `reactivate_role_forbidden`  |  403 | `项目管理员不能恢复该角色`             |
| `reactivate_not_managed`     |  403 | `该用户不在你管理的项目内`             |
| `suspend_scope_forbidden`    |  403 | `该用户跨多个项目，须由超级管理员处理` |
| `reactivate_scope_forbidden` |  403 | `该用户存在不在你管理范围内的项目`     |
| `last_super_admin`           |  400 | `不能停用最后一名超级管理员`           |
| `already_active`             |  409 | `账号当前已启用`                       |
| `not_reactivatable`          |  409 | `该停用状态不可恢复`                   |
| `account_busy`               |  409 | `账号正在更新，请刷新后重试`           |
| `project_busy`               |  409 | `{reason:project_busy}` (suspend only) |
| `operation_conflict`         |  409 | `null`                                 |
| `unavailable`                |  503 | `null`                                 |

Reuse established Identity authentication failures with null status/detail.
Reactivation Project-lock conflicts map to `account_busy`, preserving its
legacy route behavior; only suspension reports `project_busy`.
`reactivate_not_managed` is the empty derived-project case; partial ownership
returns `reactivate_scope_forbidden`.

### Accepted receipts and replay

Successful suspensions, including already-inactive no-ops, and successful
reactivations receive immutable receipts binding actor, operation UUID/name,
target, request fingerprint, derivation, accepted 21-field User snapshot,
nullable Audit ID and target generation/active flag after acceptance. Receipts
contain no raw reason/token/credential/hash, Principal or live User FK.

Replay reauthenticates the current manager and checks receipt actor, operation,
target, fingerprint and epoch, then the accepted target generation and active
flag, before any new target scope/state policy. Later lifecycle changes return
409 `operation_conflict` and cannot turn an accepted no-op into a fresh mutation.
When the post-state still matches, preserve the operation's authority branch,
including inactive-suspend early return and current project-admin reactivation
scope, before returning the frozen record without retirement, Audit or write. A no-op suspend receipt keeps a late retry from
becoming a new suspension. Reactivation replay must leave newly issued
credentials untouched. No User version column, Audit scan or generalized
supersession framework is added. Actor/payload/epoch mismatch is 409; populated
receipt state blocks downgrade.

### Migration, grants and configuration

Migration 0246 creates schema `aap_identity_suspensions_v1` and table
`public.identity_suspension_operation_receipts`. The non-inheriting
`identity_suspension_client` LOGIN may execute exactly
`suspension_policy()` and `execute_suspension_command(jsonb)`; it has no
relation, column, sequence, DDL, foreign-schema or extra protected-function
access. Readiness preserves ordinary PUBLIC invoker/extension distinctions and
exact proname grant ordering.

API/runtime optionally select `IDENTITY_SUSPENSION_DATABASE_URL` or
`IDENTITY_SUSPENSION_DATABASE_URL_FILE`; optional Compose host selector is
`IDENTITY_RUNTIME_SUSPENSION_DATABASE_URL_FILE`. A blank selector disables
only this capability, returns 503 from the private route and leaves existing
Identity startup operational. An explicitly selected DSN must pass policy
readiness before API execution; failure is closed with no ORM fallback.

Workspace exposes typed
`aap_workspace_v1.lock_identity_suspension_scope(uuid,uuid,text)` returning
exactly one nonnull `(has_projects,all_projects_owned)` row. Audit exposes only
typed `aap_audit_v1.record_identity_suspension_event(text,jsonb,uuid,jsonb,jsonb)`
for `user.deactivate {email}` and `user.reactivate {reason_present}` with fixed
actor/context/target. The Identity LOGIN receives neither support function
directly nor raw Audit/Workspace/Labeling table access. Portable schemas are
generated from the canonical models; frozen migration artifacts must match
them semantically and byte-for-byte.

## Consequences

Positive:

- The two administrator status operations share Identity credential retirement
  and receipts while retaining their different Project-lock modes and
  project-admin scope behavior.
- Existing self-deactivation reservations survive admin suspension/reactivation,
  and no responsibility or task is silently reassigned.
- No new Labeling protocol, raw foreign-table grant or public API contract is
  introduced.

Negative:

- The inherited Labeling race where a batch assignment is cleared or moved away
  without locking its old User remains outside this slice.
- Current role, scope, inactive early-return, error-order and receipt semantics
  are versioned contract details and need continued regression coverage.
- Qualification remains pending until Root reports database, runtime, restore,
  API, restart and cold-image evidence. Full Identity, ten-module/seven-gate,
  production capacity and provider-language replacement remain outside this
  owner slice.

## Notes

- Public routes remain in `apps/api/app/api/v1/users.py`. Their new composition
  is `apps/api/app/api/v1/suspension_commands.py`, with
  `apps/api/app/services/identity_suspension_commands.py` and
  `apps/api/app/services/identity_suspension_data.py` as caller-side adapters.
- After all consumers move, retire only `legacy_user_lifecycle.deactivate_user`,
  `UserLifecycleWorkflow.reactivate` and the unused `reactivate_account` helper;
  preserve DELETE/offboarding and shared lock, metadata and credential helpers.
- Existing accepted self-deactivation and its reservation behavior remain under
  [ADR-0121](0121-identity-self-deactivation-and-notification-delivery.md).
- The [Identity private runtime guide](../../docs-site/api/guides/identity-runtime#管理员停用与恢复账号)
  documents the private commands and preserved HTTP boundary.
- Full module progress remains in
  `docs/plans/1792160873_backend-module-decoupling.md`.

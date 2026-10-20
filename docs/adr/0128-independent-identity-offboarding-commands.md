# 0128 — Identity 离职预览与提交所有权

- **Status:** Accepted; implementation in progress, local qualification pending
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0075, ADR-0121, ADR-0126 and ADR-0127

## Context

The legacy offboarding preview and commit workflow coordinates target account
state, project ownership, memberships, batches, Tasks, edit locks, API keys,
reset links and Audit in one API transaction. Moving only the account status or
one transfer helper would split the snapshot, current authority checks or
credential retirement across owners.

This slice moves only `GET /api/v1/users/{user_id}/offboarding-preview` and
`POST /api/v1/users/{user_id}/offboarding` into the existing Identity
artifact/runtime. It does not add a service, process, dependency, framework,
saga or database. Public paths, DTOs, versions, errors and callers remain
unchanged. The local owner is still awaiting Root qualification.

## Decision

### Preserve the public request and response contracts

Both routes keep UUID path validation (422), current super-admin/project-admin
authorization and public response models. The public DTOs remain in
`app.schemas.user`; routes do not hydrate a User ORM row. Preview has no body.
`OffboardingCommitRequest` keeps these fields and defaults:

- `preview_version`: required, unbounded string.
- `reason`: string defaulting to empty, maximum 500 characters.
- `mode`: `handoff` by default or `emergency_suspend`.
- `projects`: empty list by default. Each entry has a UUID `project_id` and
  nullable/default-null `owner_receiver_id`, `annotator_receiver_id` and
  `reviewer_receiver_id`.

Ignored extra request fields and current validation timing remain. Duplicate
project IDs must not become an early Pydantic rejection: report 422 only after
the existing scope, snapshot/CAS and target-state checks. There is no project
count cap, public operation-ID input, CAS header or idempotency header. Each
public call creates a fresh private operation UUID.
Do not add role, project or response-byte caps; the remote Identity JSON reader
must not impose a 64 KiB body limit on this unbounded public contract.

Preview success has exactly seven top-level keys:
`user/preview_version/generated_at/projects/api_keys/blockers/can_commit`.
`user` uses the existing 21-field `DirectoryUserRecord`. A project row has
`project_id/project_name/roles/tasks/locked_task_count/blockers`; each role is exactly
`owner`, `annotator` or `reviewer`, and each task-count map has
`pending/in_progress/review/rejected/locked`. Role previews contain
`present/batches/receiver_options`; a batch has `batch_id/batch_name`, and a receiver
has `id/name/email/role/nullable project_member_role`. API-key rows have
`id/name/key_prefix/last_used_at/revoked_at`. Blockers contain only
`code/message`. Preserve the legacy array ordering and duplicate blocker rows.

Commit success has exactly `user/status/mode/transfers/unresolved/
revoked_api_key_ids/audit_id`; `status` is always `suspended`, including
`emergency_suspend`. A transfer contains
`project_id/role/receiver_id/batch_ids/task_count/lock_count`. An unresolved row
contains `project_id/role/reason/batch_ids/task_count/lock_count`; its project
and role are always nonnull source values. The native committed result requires
a nonnull positive int64 `audit_id`; the existing public model remains
unchanged, including its nullable type.

### Freeze the legacy snapshot bytes and SHA-256 digest

`preview_version` remains the lowercase hexadecimal SHA-256 of the exact legacy
Python serialization:

```python
hashlib.sha256(
    json.dumps(resource_state, sort_keys=True, default=str).encode()
).hexdigest()
```

Keep Python defaults: `ensure_ascii=True`, the default comma/colon separators
with spaces, UTF-8 without a BOM, sorted object keys, `default=str`, and
preserved list order. Parse native timestamp facts into aware Python datetimes
before hashing; Python string conversion uses a space between date and time,
`+00:00`, and fractional seconds only for nonzero microseconds. UUIDs stringify
in lowercase. Do not replace this with HMAC, hash PostgreSQL JSONB text, or
implement Python's encoder in SQL. Independent vectors cover Unicode, astral
and control characters, zero/nonzero microseconds, UUIDs and nulls.

`resource_state` has exactly the keys
`target/projects/members/batches/tasks/locks/api_keys/receivers`:

- `target` has exactly `id/is_active/disabled_kind/disabled_at/disabled_by/
disabled_reason/role`. It excludes generation, name, email, status and
  self-deactivation reservation fields.
- `projects` has `id/owner_id`, with project UUIDs sorted. `members` has
  `id/project_id/role`, including all target rows sorted by project ID and role.
- `batches` includes only target-assigned, non-archived rows with
  `id/project_id/status/annotator_id/reviewer_id/assigned_user_ids`, sorted by
  project ID and batch ID. Preserve the legacy `list(raw or [])` conversion;
  do not normalize historical opaque assignment values.
- `tasks` includes all-status direct assignee/reviewer Tasks and Tasks referenced
  by a target lock. Each row has
  `id/project_id/batch_id/status/assignee_id/reviewer_id/version`, sorted by
  project ID with nulls last, then ID. The legacy digest hashes a null
  `task.project_id` as the literal string `None` because the old code calls
  `str(task.project_id)`; native facts still retain a nullable UUID.
- `locks` includes every target TaskLock, including expired locks, with
  `id/task_id/expire_at`, sorted by task ID and lock ID. `api_keys` includes all
  target keys with `id/revoked_at/name/prefix`, sorted by creation time
  descending and ID.
- `receivers` maps each derived project UUID string to its owner, annotator and
  reviewer options. Receiver names, emails and roles affect the digest. Project
  and batch names affect preview text/output but not the digest; neither do
  `generated_at`, reason, mode or API-key `last_used_at`.

### Preserve current project facts, role choices and blockers

Derived projects are target-owned projects, all target memberships, all
target-referenced batches, direct Tasks and TaskLocks joined through existing
Projects. Archived batch references still contribute project scope even though
archived batches are omitted from the snapshot. Never use a TaskBatch fallback
for direct Task assignment.

Compute all three role-option lists for every project even when that role is
absent. Owner choices are every active global `project_admin` except the target,
with no membership requirement. Annotator/reviewer choices are active Users
whose `ProjectMember` has the corresponding role, except the target; do not
filter these choices by global User role. Preserve one membership role per
project, with valid roles `annotator/reviewer/viewer`. SQL sorts users by
`name/email/id` under the current database collation; do not sort in Python.

Owner role is present when the target owns the Project. Annotator/reviewer is
present when any non-archived assigned Batch, any direct Task in any status, or
the matching target membership exists. Task counts include only
`pending/in_progress/review/rejected`; `locked` counts target locks on
role-matching Tasks, while project `locked_task_count` counts all statuses.

Preserve blocker ordering and exact legacy messages. Global blockers are
`code/message` only. The order is: `historical_inactive` only for an inactive
target whose kind is `deleted` or `historical_unknown`; then for each project
and role in `owner/annotator/reviewer` order, `no_ROLE_receiver` for a present
role with an empty option list (the legacy message uses `project.name`); then
`historic_mixed_role` for an invalid target membership role or a batch
assignment whose role lacks a matching target membership. Legitimate different
roles on different projects are not mixed. `can_commit` remains advisory and is
exactly `(active or reactivatable-kind) AND no blockers AND not historical-kind`;
do not turn it into a new commit gate.

### Preserve authorization, locks and error precedence

Preview loads the target/snapshot and then checks current scope; a missing
target is 404. It takes no global last-super-admin fence or resource/target
UPDATE locks. Current credentials and mutable Principal metadata are not a
substitute for fresh native facts.

Preview and commit require an active super-admin or project-admin. Self-target
is 400. For an active super-admin target, protect the last unreserved
super-admin before project-admin role/scope checks. Project-admin can manage
only an employee target. An active target with no derived projects may be
previewed/committed; an inactive target with no projects returns
`inactive_unmanaged`. For nonempty scope, all derived projects must be actor
owned. Super-admin skips project-scope restrictions.

Commit takes locks in this exact order:

1. Shared last-super-admin invariant fence.
2. Actor, target and all submitted receiver Users in UUID order with
   `FOR UPDATE NOWAIT`.
3. Target absence check (404), then actor absence/inactive check (403).
4. All derived Projects plus every submitted project ID, even irrelevant or
   unknown IDs, in UUID order with `FOR UPDATE NOWAIT`.
5. Relevant target/receiver ProjectMember rows in `(project_id,user_id)` order
   with `FOR UPDATE NOWAIT`.
6. All target-assigned Batches in the locked project set, including archived
   batches, sorted by ID; then all target-related Tasks sorted by ID.
7. Per-task `pg_try_advisory_xact_lock(hashtextextended('aap:task-edit-lock:' ||
task_id, 0))`; then all target TaskLocks sorted by ID with `FOR UPDATE NOWAIT`.

Do not add a target-membership check, batch fallback, or unrelated lock. An
active nonmanager is scope-rejected after resource locks/facts, not before.
Rebuild every snapshot fact after all lock classes; never reuse pre-lock facts.
Then check current scope **before** raw preview-version CAS, followed by inactive
historical/nonrecoverable state, duplicate project mappings, handoff receivers,
and handoff blockers. Emergency mode still performs scope, CAS, state and
duplicate checks, but skips receiver and blocker checks. Irrelevant mappings and
receivers do not add validation errors, although initial User/Project locks
remain held. Preserve lock failures and exact precedence rather than normalizing
errors.

The stale-preview error is 409 and echoes the raw expected string, current SHA-256
version and current blockers. A huge or NUL-containing expected version must
still produce the stale response. Do not pass raw `preview_version` or raw
pre-CAS reason into native JSONB; portable Python performs the legacy digest and
reason normalization.

### Preserve handoff and emergency effects

Handoff processes original project order and role order `owner`, `annotator`,
`reviewer`. Owner transfer updates only `Project.owner_id/updated_at`.
Annotator/reviewer transfer mutates only non-archived target-assigned Batches and
direct target-role Tasks in the four active statuses. Set annotator Task
`assigned_at` to now; leave reviewer `assigned_at` unchanged. Update both Task
and Batch `updated_at` to now. Rebuild `Batch.assigned_user_ids` as annotator
UUID string followed by reviewer UUID string, omitting null and without
deduplication. Do not change Task version/status/override flags, ProjectMember,
review evidence or finished Tasks. Counts, batch IDs and lock counts use the
original locked snapshot; owner transfer task/lock counts are zero.

Emergency suspension transfers nothing. For every present role, unresolved
reason is exactly `emergency_suspension_requires_later_handoff`. Annotator and
reviewer unresolved `batch_ids` include every snapshot Batch in the project,
not only batches assigned to that role. Task counts include only direct active
Tasks for that role. Both annotator and reviewer lock counts use the same
project count of target locks on active related Tasks; owner counts are zero.

Both modes set inactive/offline and choose `disabled_kind=suspended` or
`emergency_suspended`. Normalize reason with Python
`(reason or '').strip()[:500] or None`; set current UTC `disabled_at/by` and
preserve accepted self-deactivation request/schedule/reason. Retire credentials
after the state update (generation +1, revoke active API keys and consume unused
reset links), then release every target TaskLock. Write one typed
`user.offboard` Audit with status 200 and exact detail
`mode/reason_present/transfers/unresolved/revoked_api_key_count`; never put raw
reason in Audit. The normalized reason legitimately remains in UserOut and the
frozen receipt. Validate final result before commit.

### Private owner transaction, schemas and receipts

The private endpoint is `POST /v1/identity/offboarding/execute`. Wire schema is
`aap-identity-offboarding/v1`, with `preview_offboarding` and `commit_offboarding`;
native phases are `prepare` and `apply` under
`aap-identity-offboarding-native/v1`. Policy schema/value are
`aap-identity-offboarding-policy/v1` and `aap-identity-offboarding/v1`.
Portable interfaces are `OffboardingRequest/Reply/Failure`,
`OffboardingManager(auth,data,secret).execute` and
`OffboardingPostgresData(dsn)`. Files are `offboarding_contract.py`,
`offboarding_manager.py`, `offboarding_postgres.py`,
`offboarding_protocol.schema.json` and
`native_offboarding_protocol.schema.json`.

Wire request keys are exactly
`schema/operation/operation_id/token/audit_context/payload`. Preview payload is
`{target_user_id}`; normalized commit payload has exactly
`target_user_id/preview_version/reason/mode/projects`. Wire success is exactly
`schema/ok/operation/operation_id/principal/data`, with data
`{preview:PublicPreviewRecord}` or `{result:PublicResultRecord}`. Failure uses
the established closed envelope
`schema/ok:false/error(code,status_code,detail)`.

Native request keys are exactly
`schema/phase/operation/operation_id/actor/expected_key_hash/request_fingerprint/
derivation_id/audit_context/payload`. Prepare payload has exactly
`target_user_id/receiver_ids/payload_project_ids`; receiver and project UUID
arrays are sorted and unique. Apply uses the same envelope with `phase=apply`
and the commit operation; its closed payload carries the transaction ID,
expected target generation/active state, normalized reason and presence flag,
explicit transfer plans and frozen public transfer/unresolved rows. The
canonical generated schema freezes the remaining field names. The raw preview
version and pre-CAS reason never enter native prepare JSONB. Use HMAC domain
`aap-identity-offboarding/v1` over canonical wire operation/payload (including
raw version/reason and normalized UUIDs), excluding token and Audit context.
Native prepare returns its transaction ID and a discriminated `snapshot` or
`replay` projection. Apply returns `{result}` with strict `replayed:boolean`;
private prepare/apply facts never reach the public response.

Migration 0248 stores commit receipts only. Each receipt binds actor, private
operation UUID/name, target, request fingerprint/derivation, target generation
and active state after commit, frozen result, Audit ID and accepted scope basis
`{target_role,target_was_active,project_ids}`. It contains no raw token,
credential, hash, Principal or live User foreign key. On replay, reauthenticate
the current manager and credential; reject binding/epoch mismatch or changed
target generation/active state before execution. Replay authority uses the
accepted scope basis because the handoff can remove the last direct reference:
SA is allowed; PA requires accepted employee target and current existence and
ownership of every accepted project, or an empty accepted project list with an
active target. An inactive unassigned accepted target remains denied to PA.
Scope loss denies PA; newly added target references do not become new work.
Matching replay returns the frozen result without repeating CAS, state checks,
receiver validation, transfer, credential retirement, TaskLock release or Audit.
Public retries use fresh operation UUIDs and still follow normal CAS; a stale
reused public preview is never converted into idempotent success.

`OffboardingPostgresData.transaction()` owns one connection and transaction for
prepare, portable snapshot/digest/scope/CAS checks, apply, strict native/wire
validation and commit. Prepare returns the transaction ID; apply verifies the
same transaction, actor/target/request binding and expected target
generation/active state before writes. Do not pass a transaction handle over
HTTP, persist a prepare ledger, or split this into a saga. Native reply
validators bind both phases to the original target; the apply result must also
match the requested mode, `disabled_by` actor, normalized reason and terminal
account state. The native committed result requires a positive nonnull int64
`audit_id`. Native post-write validation failure must raise through a rollback
subtransaction before returning `unavailable`, including for a direct SQL
caller that commits the failure response.

Preview policy, snapshot and public response are portable. Native prepare data
is discriminated as `snapshot` or `replay`; current Principal remains outside
the data projection. Replay facts include the accepted frozen result and private
scope basis; public replies strip the transaction ID, native facts and replay
flag.

Migration 0248 creates schema `aap_identity_offboarding_v1` and table
`public.identity_offboarding_operation_receipts`. The noninheriting
`identity_offboarding_client` LOGIN can execute exactly
`apply_offboarding_command`, `offboarding_policy` and
`prepare_offboarding_command`, sorted by actual proname. It has no raw
cross-owner table, column, sequence, DDL, foreign-schema, TEMP or other
protected-function rights. Optional API/runtime DSN is
`IDENTITY_OFFBOARDING_DATABASE_URL(_FILE)`; optional Compose host selector is
`IDENTITY_RUNTIME_OFFBOARDING_DATABASE_URL_FILE`. A blank selector disables
the standalone Identity runtime capability; the embedded API adapter continues
to use the caller transaction. An explicit DSN must pass readiness and never
falls back to caller-transaction mode.

Owner-only typed capabilities are:

- Workspace `lock_identity_offboarding_scope(target_uuid,payload_project_ids,
receiver_ids)` returns the locked UUID project union.
- Labeling `lock_identity_offboarding_work(target_uuid,project_ids)` returns a
  nonnull boolean; false means advisory conflict only. SQL lock errors are
  preserved for `offboarding_busy` mapping.
- Workspace `identity_offboarding_facts(target_uuid)` returns exactly one
  nonnull row of project, membership and candidate-membership JSON arrays;
  Labeling `identity_offboarding_facts(target_uuid)` returns exactly one row of
  batch, task and lock JSON arrays.
- Identity constructs candidate Users from its own User data and typed
  candidate-membership facts. It preserves SQL ordering by name/email/id.
- Workspace `transfer_identity_offboarding_owners(target_uuid,changes)` returns
  actual changed project IDs/count. Labeling
  `transfer_identity_offboarding_work(target_uuid,assignments)` returns actual
  Batch/Task transfer summaries; Identity validates planned versus actual counts.
- Labeling `release_identity_offboarding_task_locks(target_uuid)` returns the
  released count. Workspace `identity_offboarding_replay_scope(actor_uuid,
project_ids)` returns exactly one nonnull `(projects_exist,all_projects_owned)`
  row without writes.
- Audit `record_identity_offboarding_event(actor_json,target_uuid,detail_json,
context_json)` returns the Audit ID and accepts only the fixed event/context.

No raw cross-owner grants or per-project RPC are added. Existing Project owner
transfer helpers used by other workflows remain; the legacy offboarding
coordinator, Batch handoff/release helpers and old account lifecycle module are
retired without aliases. `services/user_lifecycle.py` remains only for the
shared `UserLifecycleService.lock_accounts` capability after caller search.

### Closed failures and local qualification status

The exact business error pairs are:

| Code                        | HTTP | Detail                                                                                                                           |
| --------------------------- | ---: | -------------------------------------------------------------------------------------------------------------------------------- |
| `manager_required`          |  403 | `当前账号已无管理权限`                                                                                                           |
| `target_not_found`          |  404 | `用户不存在`                                                                                                                     |
| `self_offboard`             |  400 | `不能交接自己`                                                                                                                   |
| `last_super_admin`          |  400 | `不能停用最后一名超级管理员`                                                                                                     |
| `target_role_forbidden`     |  403 | `项目管理员仅能处理标注员/审核员账号`                                                                                            |
| `inactive_unmanaged`        |  403 | `该用户已停用且不在你管理的项目内，须由超级管理员处理`                                                                           |
| `scope_forbidden`           |  403 | `该用户存在不在你管理范围内的项目`                                                                                               |
| `offboarding_preview_stale` |  409 | `{code:offboarding_preview_stale,expected_preview_version,current_preview_version,blockers}`                                     |
| `historical_target`         |  409 | `历史未知或已删除账号不可再次交接`                                                                                               |
| `state_forbidden`           |  409 | `账号当前状态不可交接`                                                                                                           |
| `duplicate_project`         |  422 | `同一项目不能重复提交交接映射`                                                                                                   |
| `receiver_required`         |  409 | `{code:receiver_required,project_id,role}`                                                                                       |
| `receiver_ineligible`       |  409 | `{code:receiver_ineligible,project_id,role,receiver_id}`                                                                         |
| `offboarding_blocked`       |  409 | `{code:offboarding_blocked,blockers}`                                                                                            |
| `account_busy`              |  409 | `账号正在更新，请刷新后重试`                                                                                                     |
| `offboarding_busy`          |  409 | Project/member lock: `{code:offboarding_busy,message:职责正在变化，请刷新预览后重试}`; task/edit lock: `{code:offboarding_busy}` |
| `operation_conflict`        |  409 | `null`                                                                                                                           |
| `unavailable`               |  503 | `null`                                                                                                                           |
| `invalid_request`           |  400 | `null`                                                                                                                           |

Existing Identity authentication errors preserve null status/detail. Initial
inactive admission is 401; an admitted actor that becomes inactive is mapped
to the existing public manager 403. Error builders use JSON-mode for UUIDs,
including nested blocker lists. Reject duplicates, aliases, extras, non-finite
values, numeric coercion, misbinding and secret echo; validate native and final
wire data inside the transaction. No local database/runtime/image qualification
is claimed yet. The full ten-module/seven-gate objective remains active; this
slice does not qualify full Identity, Go or another provider, production
capacity, or primary cutover.

The pre-owner legacy characterization baseline passed two tests and cleaned up
all owned resources. On a fixture with one project and three tasks, legacy
preview used 11 SQL calls, 1,196,316 peak Python bytes and 41.509 ms host time;
commit used 30 calls, 813,724 bytes and 46.974 ms. On four projects and twelve
tasks, preview used 20 calls, 908,996 bytes and 48.771 ms; commit used 54 calls,
959,931 bytes and 65.541 ms. The frozen preview-version vectors are
`22377e1b9f071a85ada1364c91aaf3d2e70ff94de723d93574a6c29754f47d28` and
`48b16b7c7558d05990a779d8ea10fb88e2c5f2f42db5057f573c07d299043497` for those
fixtures, respectively. These are legacy baselines for same-scenario comparison,
not offboarding-owner passes, an SLO or production-capacity evidence.

## Consequences

Positive:

- Identity owns the offboarding snapshot, policy, one-transaction transfers,
  credential retirement, Audit and frozen commit receipt.
- The exact current Python digest, receiver projection, transfer ordering and
  current-scope replay boundary remain portable and versioned.
- Owner helpers expose only typed facts/commands; API and Identity do not gain
  raw cross-owner table access.

Negative:

- A stale preview remains a compatibility-sensitive compare-and-swap contract;
  option/name/email changes intentionally affect the digest.
- A handoff may alter target-derived references, so replay must use accepted
  scope basis while rechecking current project ownership for project-admins.
- Migration, current runtime qualification, other Identity lifecycle and all
  full-module/provider goals remain unfinished.

## Notes

- Public routes and DTOs remain in `apps/api/app/api/v1/users.py` and
  `apps/api/app/schemas/user.py`. HTTP composition is
  `apps/api/app/api/v1/offboarding_commands.py`, exporting
  `require_offboarding_manager`, `get_offboarding_commands` and
  `execute_offboarding`. The caller port is
  `apps/api/app/services/identity_offboarding_commands.py` (`OffboardingCommandsPort`
  and `IdentityOffboardingCommands`) and its same-caller-transaction adapter is
  `apps/api/app/services/identity_offboarding_data.py`
  (`IdentityOffboardingCommandData`).
- Portable source files are under `apps/identity/src/aap_identity/`; do not add a compatibility alias for the removed API lifecycle coordinator.
  `apps/api/app/services/user_lifecycle.py` retains only the shared
  `UserLifecycleService.lock_accounts` capability after caller search.
- The Project transfer helper remains for its other callers. The Batch
  `handoff_offboarding_role` and `release_user_task_locks` helpers are retired.

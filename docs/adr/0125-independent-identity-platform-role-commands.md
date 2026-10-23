# 0125 — Identity 平台角色预览与变更所有权

- **Status:** Accepted; locally qualified slice (2026-10-20)
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0118, ADR-0123 and ADR-0124

## Context

`GET /api/v1/users/{user_id}/role/preview` and
`PATCH /api/v1/users/{user_id}/role` currently preview and change platform
roles in the API. Their rules depend on the current super-admin credential, the
last-super-admin fence, target-owned projects, memberships and unfinished
Labeling work. Moving only the final `User.role` assignment would split the
preview, blockers, credential fence, Audit and accepted-response replay across
owners.

This slice moves only these two super-admin operations into Identity. It does
not include deactivation, deletion, offboarding, provisioning, other-user
hydration, project membership changes, batch ownership or task reassignment.

## Decision

### Preserve the public contract

Keep both public paths and methods, super-admin-only access, request defaults,
errors, response models and SDK/CLI behavior. Do not add compare-and-swap or an
idempotency header. Preview keeps a UUID `user_id` path parameter and a required
`role` string with `min_length=1`. Mutation keeps a string path parameter and
`RoleChangePayload.role` as a string; its existing ignored-extra-fields
behavior remains. Invalid mutation UUIDs remain 404 and must not become UUID
validation errors in the public schema.

Accepted roles are exactly `super_admin`, `project_admin`, `employee` and
`viewer`. Do not trim or lowercase input or add an arbitrary short length cap.
Historical target role strings, status strings and opaque preferences remain
readable. Invalid requested role returns 400 `非法平台角色: {role}`; missing or
inactive target returns 404 `用户不存在`; self-change returns 400
`不能修改自己的角色`. A freshly locked actor must still be active
`super_admin`; losing that role after admission returns 403
`当前账号已无管理权限`.

### Blockers and preview semantics

There are three work blockers, in this exact order:

1. If the requested role is `super_admin`, `project_admin` or `viewer`, any
   target `ProjectMember` row whose role is not `viewer` adds
   `账号仍持有标注/质检项目职责，请先在项目中变更职责`.
2. If the requested role is `employee` or `viewer`, any target-owned Project
   adds `账号仍是项目负责人，请先转移项目后变更平台角色`.
3. Only for requested `viewer`, any task in `pending`, `in_progress`,
   `rejected` or `review` whose effective assignee or reviewer is the target
   adds `账号仍有未完成的标注/质检工作，请先交接`.

Effective work uses `COALESCE(Task.assignee_id, TaskBatch.annotator_id)` or
`COALESCE(Task.reviewer_id, TaskBatch.reviewer_id)`. Explicit Task assignments
override the batch fallback, unbatched Tasks participate, and a Task counted
through both assignments contributes once. TaskBatch is outer-joined. TaskLock
is not a blocker. Work blockers are computed only for a real role change.

Preview remains advisory, returns 200 with blockers, and does not acquire the
last-super-admin reservation fence. Its order is self-change, same-role,
last-super-admin, then the three work blockers. Work blockers are skipped when
the requested role already matches. The target must be active; `用户已停用` is
not a reachable preview blocker. Only the super-admin public branch is exposed;
exclude the unused project-admin/redacted-project branch and always return
`other_project_count: 0`. `can_change` is true exactly when `blockers` is empty.
The preview last-super-admin blocker applies when the target currently has the
`super_admin` role and the requested role does not. It uses the current
`super_admin_status(target)` snapshot; a remaining unreserved super-admin is
active with both deactivation request and schedule null.

Preview data has exactly these twelve fields:
`user_id/email/current_role/requested_role/can_change/blockers/projects/assigned_batch_count/assigned_task_count/review_task_count/other_project_count/warnings`.
Each project has exactly
`project_id/project_name/membership_role/annotator_batch_count/reviewer_batch_count/assigned_task_count/review_task_count`;
`membership_role` is nullable. Projects are target memberships joined to
Project, ordered by `Project.name ASC, Project.id`; no owned-only projects or
status/archive filters are added.

Project display counts include every status and use direct batch annotator or
reviewer and direct Task assignee or reviewer assignments only. They do not use
the blocker fallback. A batch present in both batch slots contributes twice to
`assigned_batch_count`. Totals sum only the membership projects and do not mix
viewer blocker counts into display metrics. Counts are exact nonnegative
signed-int64 values; IDs are UUIDs and booleans are strict. Do not add row,
project or response-byte caps. Opaque values preserve incoming numeric lexemes
in the independent consumer while backend JSON remains compatible.

Warnings are exactly these two strings, in order:

- `平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。`
- `若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。`

### Mutation authority, lock order and errors

For every mutation, lock the shared last-super-admin invariant before locking
actor and target User rows in UUID order with `FOR UPDATE NOWAIT`. Reuse the
same-owner Account credential helper reentrantly only after those User locks;
verify current JWT generation and expiry or API-key state, hash and scopes.
Preserve `expected_key_hash` as an opaque 1–255-character value, not a
bcrypt-only or hex-only restriction. Current actor role/name/email/scopes may
change since admission; bind the credential identity, generation and expiry,
then enforce current super-admin authority without treating a valid Principal
as malformed.

After the invariant, ordered User and credential locks, a same-role mutation
freezes its successful no-op receipt and returns without Project locks,
blocker/last-super-admin checks or Audit. For a real role change, first lock all
Projects currently owned by the target in UUID order with NOWAIT through
Workspace. Account/global-fence contention returns 409
`账号正在更新，请刷新后重试`; owned-Project contention returns 409 with
`{reason:project_busy}`. Only after the Project locks, compute work blockers;
any blocker returns 409 with exactly
`{reason:platform_role_change_blocked,blockers:[...]}`. Then, if the old role
is `super_admin` and the requested role is not, check the existing
`super_admin_status(target)` under the shared fence. An unreserved remaining
super-admin is active and has both deactivation request and schedule null. If
none remains, return 400 `不能降级最后一名超级管理员`. Thus work-blocker 409
takes precedence over the final-super-admin 400, while Project contention is
reported first. Do not change project memberships, batch owners or tasks. Do
not bump JWT generation or revoke API keys.

Other exact failures are `invalid_request` 400/null,
`super_admin_required` 403/`当前账号已无管理权限`,
`target_not_found` 404/`用户不存在`,
`invalid_platform_role` 400/`非法平台角色: {rawrole}`,
`self_change` 400/`不能修改自己的角色`, `last_super_admin`
400/`不能降级最后一名超级管理员`, `account_busy`
409/`账号正在更新，请刷新后重试`, `project_busy` 409 with the closed
reason object, `platform_role_change_blocked` 409 with the closed reason and
blocker list, `operation_conflict` 409/null and `unavailable` 503/null.
Reuse Identity authentication errors with null status and detail. Unknown,
malformed, extra, aliased or duplicate fields, secret echoes and binding
failures roll back as unavailable without exposing diagnostics or falling back
to another writer.

### Versioned command and data owners

The private endpoint is `POST /v1/identity/platform-roles/execute`. Wire schema
is `aap-identity-platform-roles/v1`; operations are `preview_role_change` and
`change_platform_role`. Native schema is
`aap-identity-platform-roles-native/v1`; policy schema/protocol are
`aap-identity-platform-roles-policy/v1` and
`aap-identity-platform-roles/v1`. The portable interfaces are
`PlatformRoleRequest/Reply/Failure`, `PlatformRoleManager(auth,data,secret).execute`
and `PlatformRolePostgresData(dsn)`. Canonical wire schemas are
`platform_role_protocol.schema.json` and
`native_platform_role_protocol.schema.json`.

Wire requests contain exactly `schema/operation/operation_id/token/audit_context/payload`;
payload is exactly `{target_user_id,role}`. Reuse the four-field
`SessionAuditContext`. Hide raw tokens in repr, errors and logs; never accept an
asserted actor. Native requests contain exactly
`schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/derivation_id/audit_context/payload`.
The actor is the authenticated `Principal`. Fingerprint and derivation use the
canonical operation/payload HMAC pattern under the distinct
`aap-identity-platform-roles/v1` domain, with normalized UUIDs and the
unmodified role string.

Wire success is exactly `schema/ok/operation/operation_id/principal/data`;
Native success adds strict `replayed:boolean`. Preview data is the twelve-field
model above. Mutation data is exactly `{user: ...}` using the existing
21-field Directory-user-compatible projection. Its ID and requested role must
match and the account must be active. Preserve opaque preferences and
historical status/nullable metadata; never return credential hashes, password
fields or generation.

Workspace supplies two narrow bulk capabilities. Stable
`identity_platform_role_facts(target UUID)` has the closed reply
`schema/target_user_id/owned_project_count/non_viewer_membership_count/membership_projects`;
each membership project contains exactly
`project_id/project_name/membership_role` and is ordered by project name then
UUID. The raw non-viewer count is not replaced by the joined project count.
Volatile `lock_identity_platform_role_owned_projects(target UUID)` locks
current target-owned Projects in UUID order with NOWAIT and returns a strict
boolean.

Labeling supplies stable
`identity_platform_role_facts(target UUID, ordered_membership_project_ids UUID[])` for the
closed reply `schema/target_user_id/unfinished_effective_work_count/membership_projects`;
each membership project contains exactly
`project_id/annotator_batch_count/reviewer_batch_count/assigned_task_count/review_task_count`.
Identity passes Workspace's ordered membership project IDs once. The global
unfinished-effective-work count is independent of that project list; per-project
direct counts follow the same order and include zero rows. There is no
Labeling-to-Workspace capability or raw-table grant. The frozen JSON schemas are
`platform_role_workspace_facts.schema.json` with ID
`aap-workspace-identity-platform-role-facts/v1`, and
`platform_role_labeling_facts.schema.json` with ID
`aap-labeling-identity-platform-role-facts/v1`. Both use strict int64 counts;
Workspace membership projects are ordered by name then UUID. No app ORM joins,
per-project RPCs or raw foreign-table access are introduced. Audit exposes only
the typed `record_identity_platform_role_change` capability for the current
actor/context/target/email/old/new facts.

Preview User, facts, counts and last-super-admin status use one statement
snapshot with STABLE helpers. Mutation reads facts after acquiring the
existing owned-Project locks, preserving account-to-Project ordering without
adding membership, task or TaskLock locks.

### Accepted receipts and replay

Only successful mutation commands, including same-role no-ops, receive an
immutable receipt. It binds actor, operation UUID, target, operation name,
fingerprint and derivation, and freezes the accepted `{user: ...}` result plus nullable
Audit ID. It contains no raw token, credential/hash, Principal or live User FK.
Real changes write exactly one typed `user.role_change` Audit with detail
`email/old/new`, status 200 and the target User. No-op receipts do not
write Audit.

The shared invariant and User locks plus a try operation-advisory fence
serialize duplicate UUIDs. Replay always reauthenticates and rechecks a current
active super-admin actor and active target. Profile/context changes and later
target-role changes may still return the frozen historical accepted result; it
is not a new read or a request to reapply the mutation. Thus after A→B→A, replay
of the original B operation returns the frozen B snapshot while the database
remains A and Audit does not change. On replay, skip fresh work-blocker and
last-super-admin mutation checks because no new role write occurs. Actor,
payload or derivation-epoch mismatch returns 409 `operation_conflict`.
Same-role receipts prevent a retry from becoming a later change. There is no
User version column/trigger, supersession guard or Audit-scan capability.
Public calls always use fresh operation UUIDs and expose no replay key. A
populated receipt blocks downgrade; backup, restart, response loss and epoch
change still require local proof.

### Migration, grants and configuration

Migration 0245 creates schema `aap_identity_platform_roles_v1` and receipt
table `public.identity_platform_role_operation_receipts`. The non-inheriting
`identity_platform_role_client` LOGIN can execute exactly
`platform_role_policy()` and `execute_platform_role_command(jsonb)`, with no
relation, column, sequence, DDL, foreign-schema or extra inherited protected
function access. Readiness preserves ordinary PUBLIC invoker/extension
distinctions and verifies exact grants.

API/runtime optionally select `IDENTITY_PLATFORM_ROLE_DATABASE_URL` or
`IDENTITY_PLATFORM_ROLE_DATABASE_URL_FILE`; Compose host selector is
`IDENTITY_RUNTIME_PLATFORM_ROLE_DATABASE_URL_FILE`. A blank selector disables
only this capability, returns 503 from the private route and leaves existing
Identity startup operational. An explicitly selected DSN must pass
`platform_role_policy()` readiness before the API executes a command; failures
are closed and do not fall back.

### Local qualification

Root integrated source revision `34800df85` passed 56 distinct selected
database/process cases with zero skips: 48 Native/legacy-API cases, five restore
cases and three live cases. The live cases cover the installed owner with the
independent Node consumer, an AUTH-only API binding to the nonroot/read-only
Identity runtime container (1 CPU, 256 MiB), and a dedicated-DSN API binding.
The installed package has 94 exact `.py`/`.json` file hashes; source and
installed package suites each passed 447 tests. API/architecture passed 499
checks with 127 explicitly deferred rules; Node 22 self-checks and 35 SDK
user/CLI/OpenAPI checks passed.

The API cold image
`sha256:a49450a0b203a3f05f68c6de058cd61d7d84d66cfd2b9384265ce3d8adbb6ca9`
passed in 38.894 seconds with network disabled, read-only filesystem and a
1 CPU/512 MiB limit. It verified 94 package files, 89 API/native inputs, both
role routes, the prior owner route counts, unchanged OpenAPI, migration head 0245 and
absence of retired role helpers; the temporary container was removed.

A finite host-process sample used 12 targets at concurrency four. Execute made
34 attempts with 22 busy retries in 927.441 ms (p50 455.302 ms, p95 920.672 ms);
replay made 34 attempts with 22 retries in 907.557 ms (p50 446.324 ms, p95
907.389 ms). These include Node/HTTP host-process overhead and do not establish
capacity or an SLO. Replay left all 12 receipts and 12 Audit rows unchanged.
This is local owner qualification only; full Identity, production capacity and
cutover remain outside scope.

## Consequences

Positive:

- Preview and mutation share Identity authority while Workspace, Labeling and
  Audit expose only bounded facts or typed writes.
- The existing last-super-admin and account/project lock order remains explicit;
  accepted results can be recovered without reapplying a later role change.
- Public HTTP behavior stays compatible and no project work is silently
  reassigned.

Negative:

- Blocker ordering, count definitions and the historical A→B→A receipt behavior
  become versioned contract details.
- The local database/process, installed-package, architecture, SDK and cold-image
  checks above qualify this owner slice only. They do not qualify full Identity,
  production capacity or cutover.
- Other-user hydration, lifecycle writes, full Identity, the ten-module/
  seven-gate objective, production capacity and language replacement remain
  outside this slice.

## Notes

- Existing public paths remain in `apps/api/app/api/v1/users.py`; their HTTP
  composition is `apps/api/app/api/v1/platform_role_commands.py`, with
  `apps/api/app/services/identity_platform_role_commands.py` and
  `apps/api/app/services/identity_platform_role_data.py` as the caller-side
  command/data adapters. The owner covers only preview and role change; it does
  not claim the whole Identity module is independently deployed.
- After all callers move, retire only
  `management.role_impact_preview`/`management.platform_role_change_blockers`,
  `legacy_user_lifecycle.change_user_role` and its unused `set_platform_role`
  helper. Preserve the remaining lifecycle implementation.
- The [Identity private runtime guide](../../docs-site/api/guides/identity-runtime#平台角色预览与变更)
  documents the versioned command and preserved HTTP boundary.
- Completed administrator-password and Directory qualifications are recorded
  independently in ADR-0124 and ADR-0123; their evidence is not counted as
  platform-role qualification.
- The overall backend module-decoupling goal remains active in
  `docs/plans/1792160873_backend-module-decoupling.md`.

# 0120 — Identity 分组目录与指派独立所有权

- Status: Accepted; group implementation and bounded local runtime qualification are complete
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: None
- Extends: ADR-0115, ADR-0118 and ADR-0119

## Context

The platform already exposes a global group catalog and account-group
assignments, but the relevant API workflows still read or write Identity-owned
Group/User data through ORM paths. Moving the HTTP call alone would not make
assignment safe: a project admin's target scope depends on current Workspace
project ownership and membership, and role revocation or project transfer must
remain fenced through commit. Invitation acceptance also creates groups, but it
stages a User and Project membership in a legacy multi-owner transaction; that
workflow cannot be claimed as migrated by this catalog/assignment slice.

The decision must preserve the public DTOs, partial bulk behavior, current role
matrix, Unicode trimming and stable audit semantics. It must keep one shared
PostgreSQL transaction owner and avoid granting Identity direct Workspace table
access. This work does not complete group invitation, directory/admin, other-user
hydration or Identity lifecycle ownership.

## Decision

### Private Identity protocol and public compatibility

Keep the existing public group endpoints and DTOs. API handlers pass the current
JWT or API key plus the existing private control bearer to
`POST /v1/identity/groups/execute`; the request is the closed
`aap-identity-groups/v1` object:

```text
{schema, operation, operation_id, token, payload}
```

The eight operations and payloads are:

| Operation                   | Payload                         | Success data                                        |
| --------------------------- | ------------------------------- | --------------------------------------------------- |
| `list_groups`               | `{}`                            | `{items: GroupRecord[]}`                            |
| `query_groups`              | `{page, page_size, search}`     | `{items, total, page, page_size, pages}`            |
| `create_group`              | `{name, description}`           | `{group: GroupRecord}`; `group.id = operation_id`   |
| `update_group`              | `{group_id, name, description}` | `{group: GroupRecord}`                              |
| `delete_group`              | `{group_id}`                    | `{group_id, deleted: true}`                         |
| `preview_group_assignments` | `{user_ids, group_id}`          | Existing `BulkGroupAssignmentPreview` DTO           |
| `apply_group_assignments`   | `{user_ids, group_id}`          | Existing `BulkGroupAssignmentResponse` DTO          |
| `assign_user_group`         | `{user_id, group_id}`           | `{account: AccountRecord}` for the requested target |

Every object is closed and validated before the caller transaction commits. The
success envelope has `schema`, `ok:true`, matching operation and UUID, current
`principal`, and `data`. The failure envelope is
`{schema,ok:false,error:{code,status_code,detail}}`; it never exposes SQL or
native exception text.

`GroupRecord` is exactly `{id, name, description, member_count, created_at}`.
Name length is 1–100; description is nullable and at most 500 characters;
timestamps are timezone-aware; member count includes active accounts only. List
sorts by name then UUID. Query trims `search` with Python `str.strip()` and uses
ILIKE over name or description. Page starts at 1, page size is 1–500, no new
search-length ceiling is introduced, and pages is zero for an empty result.
Batch user IDs contain 1–500 UUIDs, preserve duplicates and order, and do not
inherit a new global response byte/cardinality ceiling.

Updates map omitted and null name/description to the existing unchanged
semantics. A non-null description is written and audited; an empty string clears
it. Creating a blank name returns `policy_rejected` 400 with `名称不能为空`.
Duplicate names return 409 with `已存在同名数据组: ` plus the trimmed create
name or the raw update name, matching the legacy paths. An unchanged name-only
update does not create an Audit event.

Both `super_admin` and `project_admin` manage the global catalog. A super-admin
can assign any target. A project admin can assign themselves, a member of any
currently owned project, or any active employee globally. Bulk assignment also
preserves the existing ability to include inactive members of an owned project;
single assignment requires an active target. Out-of-scope and nonexistent user
targets remain indistinguishable. Bulk preview returns per-item failures with
safe legacy messages and `retryable:true`; a missing requested group is a
top-level 404 on apply but a per-item error in preview. `assign_user_group`
returns the existing AccountRecord with `account.id` equal to the requested
`user_id`, not the acting user.

Policy failures are closed to three details: blank name 400, duplicate name 409,
and loss of current manager role 403 (`当前账号已无管理权限`). Other policy and
operation failures keep the existing safe code/status translation. Additional
codes are `group_not_found`, `target_not_found`, `group_busy`, `account_busy`
and `operation_conflict`; missing group/user map to 404, and transient locks map
to retryable 409 without leaking SQL details.

### Native Identity ownership and Workspace scope

Migration 0240 adds schema `aap_identity_groups_v1`, `group_policy()` and
`execute_group_command(jsonb)`. The pre-created, non-inheriting
`identity_group_client` LOGIN can execute exactly those two functions and has no
direct Group/User/Project/ProjectMember table privileges. It receives no
Workspace helper grant. Workspace remains the owner of project and membership
rows and projects an allowed UUID set through:

```text
aap_workspace_v1.lock_group_assignment_scope(uuid, uuid[], boolean)
```

The scope projection binds the actor through Identity's fresh native principal;
it never trusts API-supplied management booleans. Workspace locks owned Project
rows before membership rows, uses NOWAIT for writes, re-reads scope after locks,
and holds relevant project and membership locks through the outer transaction.
Reads/previews use the same owner capability without mutation locks. This keeps
the database shared and the write atomic; it does not introduce a saga or a
separate database requirement.

Group writes lock the requested Group before actor and target User rows. A missing
requested Group can never silently clear assignment. Rename and delete pre-lock
all affected Users in UUID order using NOWAIT before bulk mutation. Assignment
target activity and visibility are re-read under lock; owner transfer, member
removal and credential revocation cannot authorize stale assignments. The
implementation must not serialize the entire catalog globally, and bulk
savepoints must not swallow unknown internal errors.

### Receipts, Audit and replay

`identity_group_operation_receipts` stores operation UUID, actor, operation,
request fingerprint, derivation ID, frozen result and creation time without a
live-User foreign key. Writes are `create_group`, `update_group`, `delete_group`,
`assign_user_group` and `apply_group_assignments`; the bulk receipt is retained
even when all requested items fail. Reads and preview have no receipt. A receipt
scope binds actor, operation, fingerprint and derivation. Reuse of an operation
UUID with another scope conflicts. An advisory try-lock serializes a command,
then Identity reauthenticates the current actor and manager role before replay.
Policy failures create no receipt. A single-assignment replay also checks that
the target is still in current visibility scope before returning frozen account
data.

The fingerprint master is `HMAC-SHA256(secret UTF-8, ASCII
"aap-identity-groups/v1")`; derivation ID is `SHA256(master).hex`. The request
fingerprint is HMAC-SHA256 over sorted-key compact `ensure_ascii` JSON
`{operation,payload}`. Raw bearer, request metadata and user secrets are excluded
from receipt scope. State, typed Audit and receipt commit in the same transaction.

Audit calls use the closed four-argument owner function
`record_identity_group_event(text,jsonb,jsonb,jsonb)` with explicit
`target_type`, `target_id` and `detail`; only allowed business detail is stored.
The accepted events are:

| Event                    | Status | Detail                                                                |
| ------------------------ | ------ | --------------------------------------------------------------------- |
| `group.create`           | 201    | `{name}`                                                              |
| `group.update`           | 200    | `{name}` and optional `{description: "updated"}`                      |
| `group.delete`           | 204    | `{name}`                                                              |
| `user.group_change`      | 200    | `{email, old_group_id, old_group_name, new_group_id, new_group_name}` |
| `user.bulk_group_assign` | 200    | `{group_id, group_name}`                                              |

Update name detail preserves the old event semantics (new name or old-to-new
change). There is no Audit for list/query/preview or an unchanged name-only
update, and no grant for arbitrary Audit table writes.

### Invitation boundary and configuration

Invitation acceptance remains the existing caller-owned coordinator. It may call
the private Identity helper `resolve_invitation_group(uuid,text)`, which validates
the current pending UserInvitation/token/expiry/inviter and returns `{id,name}`
or null inside the caller transaction. It resolves the invite name from the
matching invitation row, may create the Group under a SHARE lock held through
commit, and does not expose raw token material. The helper is not part of the
GROUP wire protocol and is not granted to `identity_group_client`. User
initialization, Project membership staging and final invitation Audit remain in
the legacy coordinator.

The API can select a dedicated `identity_group_database_url` or file. The
standalone Identity runtime uses `group_database_url` or its file under the
`IDENTITY_` prefix. If no group owner is selected, the API caller transaction
remains the embedded path; an explicitly selected DSN/remote failure never
falls back. A selected owner missing GROUP returns 503. Compose uses
`IDENTITY_RUNTIME_GROUP_DATABASE_URL_FILE` as the host file selector and mounts
it as `IDENTITY_GROUP_DATABASE_URL_FILE`; the secret must target the same
PostgreSQL database that owns the Workspace scope projection and 0240 functions.

## Consequences

The Identity module can own the global group catalog and assignment policy
without directly reading Workspace tables. API callers can replace the Identity
implementation while preserving the existing user-management response DTOs,
partial bulk results and role boundaries. Migration 0240 retains receipts that
prevent a destructive downgrade.

The strong assignment decision still depends on one shared PostgreSQL database
and an outer caller transaction across Identity, Workspace, Audit and receipt
functions. This decision does not extract the invitation coordinator, group/user
directory search, platform-role administration, user hydration, employee
offboarding or multi-owner lifecycle. It does not complete Identity as a whole,
qualify production, or provide an alternate-language implementation. Local
native/API/role/consumer qualification remains a separate gate; this ADR records
the boundary, not a claim that those checks passed.

## Alternatives Considered

- **Give Identity direct Project/ProjectMember table reads:** rejected because it
  duplicates Workspace scope and lets project transfer or membership removal
  race assignment under stale authorization.
- **Split bulk assignment into separate project/user RPCs:** rejected because
  partial outcomes, current scope and per-item owner writes must be consistent
  with the existing public DTO and outer transaction.
- **Include invitation acceptance in this group service:** rejected because
  invitation acceptance stages a new User and Project membership with legacy
  token, permission, Audit and final-commit semantics. It needs its own complete
  multi-owner lifecycle contract before transfer.

## Notes

- Public/private contract and module config: `docs-site/api/guides/identity-runtime.md`,
  `apps/api/app/config.py` and `apps/api/app/api/v1/groups.py`.
- Identity runtime: `apps/identity/src/aap_identity/group_contract.py`,
  `group_manager.py`, `group_postgres.py` and
  `apps/identity/src/aap_identity_runtime/api.py`.
- Workspace projection and typed Audit: `apps/api/alembic/sql/0240_identity_groups.sql`.
- Function-only grant contract: `infra/docker/identity-groups-grants.sql`.
- Invitation remains a separate coordinator: `apps/api/app/application/invitation_acceptance.py`.
- Remaining module gates: [module deployment status](../../docs-site/dev/concepts/module-deployment.md).

## Local qualification

The assembled group passes 67 distinct database/process cases, 272 installed-package
tests and 39 independent Node consumer self-checks. AUTH-only API credentials
exercise all eight routes through remote-container and dedicated-DSN owners.
Current scope/credential fencing, ordered partial bulk results, response-loss
restart replay, restored receipts/ACLs and populated-downgrade refusal are verified.
The local 1 CPU / 256 MiB provider sample completes 12 creates at concurrency four
in 0.138s, with observed p95 0.050s; its shared PostgreSQL budget is separate.
These are local group results, not full Identity completion, production capacity
or a replacement-language provider. No production or primary cutover was performed.

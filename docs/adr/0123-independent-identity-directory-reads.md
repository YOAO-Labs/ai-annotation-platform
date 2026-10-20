# 0123 — Identity 用户目录读取所有权与可见性合同

- **Status:** Accepted; implementation is in progress and runtime qualification is pending
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0118, ADR-0120 and ADR-0122

## Context

The four user-directory reads—legacy list, paginated query, statistics and
export—currently share some filters but deliberately do not share one visibility
policy. The legacy project-admin list has a global active-employee picker mode;
the newer query, statistics and export also expose a read-only active
super-admin arm. Query results add management and lifecycle flags derived from
Workspace memberships, task batches, tasks and locks. Treating these paths as a
single generic user scope would change who can see a row or act on it.

Export is the only directory operation that writes business state: it records a
`user.export` Audit event. Moving it needs a frozen result and success receipt
so an uncertain response does not create a second Audit event. This is a
bounded Identity read-owner slice, not platform-role administration or a full
Identity extraction.

## Decision

### Public compatibility and private protocol

Keep the public routes, parameters, defaults, models, statuses and file formats
unchanged:

- `GET /api/v1/users`
- `GET /api/v1/users/query`
- `GET /api/v1/users/stats`
- `GET /api/v1/users/export`

The API adapts these reads to private `POST /v1/identity/directory/execute`
using `aap-identity-directory/v1`. The four operations are `list_users`,
`query_users`, `user_stats` and `export_users`. The request has exactly
`schema`, `operation`, `operation_id`, `token`, `audit_context` and `payload`;
`token` is the current JWT/API-key credential, not an actor assertion. The
native protocol is `aap-identity-directory-native/v1`, its policy is
`aap-identity-directory-policy/v1`, and the fixed derivation domain is
`aap-identity-directory/v1`.

Both protocols use closed success and failure envelopes. Native success adds
`replayed`; the manager validates the native value, commits, strips that field
and returns the six-key wire response. Malformed, unknown, mismatched or
unavailable results roll back and fail closed; explicit DSN failures never
fall back to a second SQL writer. Existing public dependency and role-filter
errors keep their statuses and details. An invalid platform role is a 400
`非法平台角色: {role}`, not a 422, and is bound to the submitted role. The
manager-required detail remains `需要角色权限: super_admin或project_admin`.

### Preserve the two visibility policies

The legacy `list_users` behavior is intentionally distinct:

- A super-admin sees all rows matching status and role, optionally intersected
  with the requested project's members. List order remains `created_at DESC`;
  ties have no new ordering promise.
- A project-admin sees all active employee candidates only when `role=employee`
  and `status=active`. Otherwise the result is the union of self and members of
  projects the actor owns. The legacy `project_id` input is ignored for a
  project-admin.

`query_users`, `user_stats` and `export_users` share the newer management
visibility and filters:

- Without `project_id`, a super-admin sees all matching rows. A project-admin
  sees self, members of owned projects, every active employee and every active
  super-admin. The added super-admin arm is read-only unless the exact
  own-member rule independently applies.
- With `project_id`, a super-admin sees matching members of that project. A
  project-admin sees the intersection of self plus members of owned projects
  with that project's member IDs; global employee and super-admin arms do not
  apply. Preserve the literal SQL intersection: it does not separately require
  the requested project to be owned by the actor, so a self/own-member row that
  is also a member of a foreign requested project can remain visible.

Role values are not trimmed or lowercased and have no new arbitrary short
length cap. Status defaults to `active`. Search is trimmed with Python
`str.strip()` semantics, preserves ILIKE wildcard behavior, matches email and
name, and is capped at 255 characters. `group_id` is exact. Query page defaults
to 1/50, page size is 1–500, and page is a positive signed int64. Offset
arithmetic must not overflow. List and export add no total-row or response-byte
cap.

### Preserve record shapes and same-snapshot flags

Directory user records have exactly the 21 public `UserOut`/existing
`AccountRecord` fields. Role and status remain strings; `preferences` is opaque
JSON and retains unknown values without rounding nested numbers. UUIDs,
datetimes and actual booleans remain typed. Passwords, hashes, generations and
API-key fields never enter a record. Query items add only `is_managed` and
`is_lifecycle_managed`.

For super-admin query results both flags are true. For project-admin results,
`is_managed` is true for self, a member of an owned project or any active
employee. `is_lifecycle_managed` additionally requires that none of the
target's derived projects has a different owner. Derived references include
projects the target owns, all project memberships, TaskBatch annotator/reviewer
assignments, Task assignee/reviewer assignments and TaskLock-to-Task projects.
Historical, inactive and archived references still participate; orphan
references retain the old inner-join-to-Project behavior. The selected rows,
count and flags use one consistent SQL snapshot, with bulk projections rather
than per-user RPCs.

Statistics return exact non-negative int64 `total`, `online` and
`weekly_active`; both subordinate counts are at most `total`. The weekly cutoff
is inclusive at `now - 7 days` and shares the read snapshot. Query orders by
`created_at DESC, id DESC` and page metadata/counts bind to its selected rows.

Export contains exactly eight row fields: `id`, `email`, `name`, `role`,
`group_name`, `group_id`, `status` and `created_at`. Preserve the eight-column
CSV header, UTF-8 BOM, formula escaping, `created_at.isoformat()`, media types,
attachment names and JSON `_export_meta`. A fresh export binds actor email,
UTC timestamp and request ID in its frozen result and writes one `user.export`
Audit event with status 200, `target_type=user`, null target ID and the existing
`format/count/actor_email/ip/request_id` detail. A successful replay requires
current authorization, returns the frozen export and does not repeat Audit.

### Owner capabilities and receipt scope

Migration 0243 is designated to create `aap_identity_directory_v1`. The dedicated
`identity_directory_client` LOGIN has exactly `directory_policy()` and
`execute_directory_command(jsonb)`; it has no direct table, column, sequence,
DDL or foreign-schema capability. Readiness rejects extra grants. Identity
receives only the User columns and exact cross-owner functions required by the
directory contract.

Workspace provides bulk owned-member and requested-project-member projections,
plus `identity_directory_lifecycle_blocked(actor, user_ids)`. Labeling provides
`identity_directory_project_refs(user_ids)` for deduplicated batch/task/lock
references, with at most 500 input users. Workspace may execute this one
Labeling function; neither owner grants Identity raw access to foreign
relations. Audit exposes `record_identity_directory_export(...)`, one closed
typed capability for the existing event. These projections remain typed SQL
and share the caller's snapshot.

Reads do not create receipts. A successful export receipt binds operation,
actor, request fingerprint and derivation epoch and atomically freezes only its
eight export row fields, metadata and Audit ID. On replay, the owner rechecks
current actor/credential gates and verifies every frozen User ID under the
current export scope and original `project_id`; it does not recompute the old
search, group or status filters. If a frozen user was deleted or is no longer
visible, replay fails with `409 operation_conflict`. A profile or request
context change alone may still return the original frozen rows and metadata.
The receipt has no live User foreign key and stores no raw token, password,
credential hash, native actor or second full User/preferences copy. Reusing an
ID with another actor, payload or epoch conflicts with 409. Populated receipts
prevent migration downgrade.

The API and Identity runtime can select
`IDENTITY_DIRECTORY_DATABASE_URL` / `IDENTITY_DIRECTORY_DATABASE_URL_FILE`.
The optional Compose host selector is
`IDENTITY_RUNTIME_DIRECTORY_DATABASE_URL_FILE`. A selected dedicated DSN uses
the function-only role; a standalone runtime must be provisioned with that
capability. Existing API paths and role checks remain the public boundary.

## Consequences

Positive:

- Public REST behavior remains stable while Identity owns a versioned contract
  for the four read operations and their row scopes.
- Project-derived lifecycle flags use narrow Workspace/Labeling projections
  without granting Identity raw access to those tables.
- Export retry after a lost response returns the frozen file metadata and rows
  without duplicating Audit.

Negative:

- The different legacy-list and management-query visibility rules are
  intentionally more complex than one reusable predicate and must remain
  separate in future work.
- `is_lifecycle_managed` still requires a bounded, bulk cross-owner projection
  from Labeling through Workspace in the same PostgreSQL snapshot.
- The source, API adapter, native roles, standalone runtime, export replay and
  backup/restore still require local qualification. This ADR does not complete
  those checks, platform-admin writes, full Identity or the ten-module/seven-gate
  goal.

## Notes

- Current API behavior is implemented in `apps/api/app/api/v1/users.py` and
  `apps/api/app/services/management.py`; this slice replaces only their four
  read paths.
- The [Identity private runtime protocol](../../docs-site/api/guides/identity-runtime.md#用户目录读取)
  records wire, filters and deployment configuration.
- Current module gaps remain in
  [module deployment status](../../docs-site/dev/concepts/module-deployment.md).
- Full backend progress is tracked in
  `docs/plans/1792160873_backend-module-decoupling.md`.

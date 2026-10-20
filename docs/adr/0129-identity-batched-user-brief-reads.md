# 0129 — Identity batched UserBrief reads

- **Status:** Accepted; batched UserBrief owner locally qualified
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Related:** ADR-0123

## Context

Existing API callers use `app.services.user_brief.resolve_briefs` and
`resolve_briefs_with_project_role` to add a small User projection to already
authorized tasks, projects, results and activity records. The helpers currently
read User and ProjectMember through the caller's ORM session. Moving those reads
must preserve each caller's transaction, authorization and response behavior;
this is not a new public profile endpoint or a directory-manager operation.

## Decision

### Preserve the two internal helpers and their value contract

Keep both helper signatures and all callers. They return
`{canonical_user_uuid: UserBrief}` with exactly these six required fields:
`id/name/email/role/avatar_initial/avatar_ref`. `id` is a UUID; `role` and
`avatar_ref` may be null; `name/email/avatar_initial` are strings. Copy name,
email, avatar reference and global role unchanged. For a project-scoped
read, use a truthy `ProjectMember.role`; when that role is absent or empty,
fall back to the User role. Owners and historical assignees can still receive a
brief without a project membership row.

Preserve the legacy Python initial expression:

```python
source = (name or email or "?").strip()
avatar_initial = source[:1].upper() if source else "?"
```

Whitespace-only names do not fall back to email. Do not add Unicode
normalization: Python may uppercase one source code point into multiple
characters, such as `ß` becoming `SS`.

For either helper, omit `None` inputs, normalize UUID strings, accept duplicate
input IDs and return each existing User once. Invalid UUIDs keep their current
`ValueError` before the owner call. Empty input returns `{}` without SQL or
remote I/O. Missing Users are omitted. Return existing Users regardless of
active flag, online state, pending retirement or historical role values. Input
size is unbounded; do not add a 500-user cap, filters, name/avatar cleanup or a
new public ordering guarantee.

### Keep authorization with the caller

Every caller must first authorize its own task, project, result or other source
rows, then enrich the associated user IDs. The helper has no actor parameter and
must not expand what those callers can access. The trusted service boundary is
the existing internal control bearer or the dedicated function-only SQL role;
do not require a manager-only Directory credential or reuse the 21-field
Directory DTO.

Identity owns User profile reads. For project-scoped role overlays, Identity
calls Workspace's narrow
`identity_brief_project_roles(project_id, user_ids)` capability. Workspace
alone reads ProjectMember; neither Identity nor API receives a raw
cross-owner table grant. Each nonempty batch makes one SQL function call or
private HTTP request for the full batch, not one call per user. There is no cache: each call
uses its binding's normal transaction snapshot.

### Use a closed, read-only private protocol

The portable Identity artifact adds
`aap_identity.briefs_contract`, `briefs_manager` and `briefs_postgres`, plus
canonical wire/native schema artifacts and runtime configuration. Its private
HTTP endpoint is `POST /v1/identity/briefs/execute`, protected by the existing
internal control bearer and `application/json`. The service has no user-token,
authenticator, signing-secret, audit-context or operation-ID dependency.

Wire schema and required requests are:

```json
{
  "schema": "aap-identity-briefs/v1",
  "operation": "get_user_briefs",
  "payload": { "user_ids": ["<uuid>"] }
}
```

```json
{
  "schema": "aap-identity-briefs/v1",
  "operation": "get_project_user_briefs",
  "payload": { "project_id": "<uuid>", "user_ids": ["<uuid>"] }
}
```

Do not accept unknown keys, a `protocol` alias, JSON numbers/booleans/null as
UUIDs, duplicate JSON keys or non-finite numbers. Empty and duplicate ID arrays
are valid. Do not add a request-body size cap. The existing runtime semaphore
and timeout apply.

Success has the exact closed envelope
`schema/ok/operation/data`, where `data` is
`{project_id: UUID | null, users: UserBrief[]}`. Global reads return null
`project_id`; project reads return the requested project ID. Results are unique,
sorted by canonical UUID string and a subset of requested IDs. Missing Users are
not fabricated. Before returning, validate the operation, project scope, ID
subset/order, closed six-field records, field types and the computed initial.
Malformed, wrong-scope, extra-ID or overbroad owner data fails as unavailable;
never return a partial result. Invalid requests return HTTP 400 with
`{"schema":"aap-identity-briefs/v1","ok":false,"error":{"code":"invalid_request"}}`.
Unavailable or timeout responses use HTTP 503 and
`{"detail":"Identity brief reads unavailable"}`. A bad control bearer is 401;
a disabled optional capability returns 503.

Native requests use the same operations and payloads with schema
`aap-identity-briefs-native/v1`. Native success uses the same envelope but
returns exactly the five source fields
`id/name/email/role/avatar_ref`; Identity computes `avatar_initial` in Python
to retain legacy semantics. Native errors only represent malformed requests;
SQL/system/unavailable failures map to 503 and never return partial records.
The policy is exactly
`{schema:"aap-identity-briefs-policy/v1",policy:"aap-identity-briefs/v1"}`.
The canonical native snapshot embedded in migration SQL must be byte-identical
to the exported package artifact.

`BriefsManager` validates native output within its `transaction()` data port and
then emits validated wire output. Do not add receipts, writes, audit events or
caching. Preserve strict output validation even in embedded/direct mode.

### Preserve caller-owned transactions and least privilege

Migration 0249 follows 0248. It creates schema `aap_identity_briefs_v1` with
`STABLE SECURITY DEFINER` read functions with `search_path=pg_catalog,pg_temp`;
PUBLIC schema and function access is revoked. Identity
exposes exactly `briefs_policy()` and `execute_briefs_command(jsonb)` to the
noninheriting `identity_briefs_client` LOGIN. It receives no table, column,
sequence, DDL, TEMP, role-membership, foreign-schema or other protected
function privileges or direct Workspace capability grant. The Workspace
capability returns only `(user_id, role)`
for the requested project and supplied IDs; it has no active/status/role filter.
Grant sources must agree across `infra/docker/identity-briefs-grants.sql`,
`module-data-grants.sql` and the monolithic production-grants source; the owned
verifier checks the exact role and function envelope. This slice does not apply
grants to the primary database.

The embedded SQLAlchemy adapter stays in the caller transaction and never
commits or rolls back, including after decoding errors, cancellation or SQL
errors. Preserve autoflush behavior: flush before text SQL only when
`db.autoflush` is true, respecting `no_autoflush`; do not flush when it is
false. This keeps pending caller changes visible when appropriate without
committing writes in feedback, discussion or lock-conflict callers. The
dedicated asyncpg adapter owns a read-only transaction and always closes its
connection. A selected remote/DSN failure never falls back.

Configuration is optional. API `IDENTITY_BRIEFS_DATABASE_URL` and
`IDENTITY_BRIEFS_DATABASE_URL_FILE` are mutually exclusive selectors; blank
keeps the caller Session path. Existing `IDENTITY_URL` takes precedence and
uses the private HTTP runtime. Standalone runtime uses the same optional DSN
pair, is disabled when blank, and checks readiness only when configured. The
Compose host selector is
`IDENTITY_RUNTIME_BRIEFS_DATABASE_URL_FILE`, defaulting to `/dev/null`; a
configured secret file enables the capability inside the container. Do not add
a new service, dependency, framework, runtime or version bump.

## Qualification status

Root's latest selected-case union passed 64 distinct cases with zero skips:
59 database/process cases plus five pure/static cases. It combines 11 Native,
39 caller/API and 14 live/restore cases. The 39-case caller suite replaces an
earlier partial fixture run; reruns are not added again. The 14 live/restore
cases include two installed-owner bindings, three actual API bindings, the new
0249 restore and eight affected prior restore/challenge cases. API checks cover
ordinary employee JWT bindings, the distinction between global reviewer and
project reviewer role projection, outsider 404/no-PII behavior, and explicit
remote selection without fallback or overbroad privileges.

Migration 0249 upgrade, frozen SQL installation/rollback, downgrade and restore
to 0249, exact grants, and owned PostgreSQL/Redis/S3/host/container cleanup all
passed with no cleanup errors. Source and noneditable installed packages each
passed 549 tests; 114 installed package hashes match exactly. The installed
package uses Pydantic 2.14.1 while API uses 2.13.3, and the portable artifact
contains no app, SQLAlchemy, Celery, Boto3 or Torch runtime. API assembly passed
625 checks with 132 architecture rules explicitly deferred; Node checks passed
21 and SDK serialization checks passed 43, with no SDK production-code change.
Wire schema SHA-256 is
`3e69854dc30d28e0f5cb1b958d328c526f31f273447032ac62d930a850798d14`; native
schema SHA-256 is
`91c438d4857ca90b42e8379260d4834577ab06ede69e2de012f8c93f68d8be22`, and its
migration snapshot is byte-identical. OpenAPI stayed byte-identical; codegen,
operation catalog (509 routes/93 modules) and generated environment docs (464
variables/48 sections) passed.

The nonroot, read-only Identity owner image
`aap-identity:briefs-local-3713b413718f`
(`sha256:c86eba7f5b49ef4424ba0a5eab9751d99f866b74e268f3190bfd06615dd89884`,
source `573517e5ee3e2d0a9dd76af99dbcca05decc4ae0`) passed a real remote API
binding under 1 CPU/256 MiB. The cold API image
`aap-api:local-identity-briefs-37051cbfbb77`
(`sha256:f7a80e3a2b41643d33044dfa5eff5fc49510f16fa3563241989262d5841652e7`,
source `48a9e36219518ec5698f2a93f22e0bc658b914ae`) verified 114 package and 45
API/native inputs, all six UserBrief fields, no ORM in the caller helper,
caller-transaction retention, identical native schema snapshot, current
OpenAPI, migration head 0249 and hidden SQL parameters. The proof ran without
network, read-only and at 1 CPU/512 MiB; its container was removed. The 41.875
seconds is combined build and offline verification, not startup latency.
Compose configuration-only checks passed both disabled and enabled selector
states, matched the secret mount and read-only 1 CPU/256 MiB limits, and started
zero deployments.

The pre-change legacy baseline passed on an owned disposable 0248 database and
cleaned up with no errors. Its six-user fixture
`apps/api/tests/fixtures/identity_briefs_legacy.json` covers global/project
responses, inactive and historical roles, owners without membership, empty-role
fallback, Unicode/whitespace initials and 1,201 IDs. Captured embedded query
counts were empty=0, global=1, project=2 and large=1. These are legacy helper
compatibility measurements and single host samples, not new-owner performance,
capacity or SLO claims.

## Project-member list consumer

`projects.list_members` now enriches the already-authorized Workspace membership
rows with one call to the existing global `resolve_briefs` batch. Workspace
continues to own membership selection, `assigned_at DESC` ordering, membership
role, version and timestamps. The global brief supplies `user_name`,
`user_email`, `platform_role` and `avatar_ref`; the project membership role
remains the member's project duty. This consumer does not use the project-role
overlay, add a public field such as `avatar_initial`, or issue per-member calls.
The extra batch read replaces the former join. Embedded mode keeps the caller
transaction; remote Workspace and Identity reads do not share a snapshot.

An empty membership list makes no profile-owner call. Missing profile rows stay
omitted, matching the old inner-join result, and inactive users remain eligible.
The existing ten-field member response is unchanged. Its `platform_role` is a
required four-value enum: an invalid historical global role still fails model
validation, and `_member_out` still raises `ValidationError` when its target is
missing. This slice does not make that field nullable or widen its schema.

The pre-change public response and `_member_out` baseline was captured in
`apps/api/tests/fixtures/project_member_profiles_legacy.json`, including inactive
users, valid roles, ordering, versions and avatars. The focused acceptance union
passed 21 selected cases with zero skips (18 database/process and three
pure/static); it includes the focused Native/role-write cases and extended
remote/dedicated API assertions. SDK serialization passed 44 checks, including
the captured member-response serialization case, with no SDK production change. The existing
114-file installed package and qualified owner image were unchanged and reused;
they are not additional consumer qualification runs. These results qualify
this consumer path only and do not add to the prior 64-case owner qualification.

The updated consumer assembly was also verified in a fresh API image,
`aap-api:local-identity-member-profiles-a443d477f161`
(`sha256:5b64c0fb1636a8144036ec6d1b420ccdf90652cd2d6727f410cbea163b865ca7`,
source `3955efd57b5d3a87f5dd8d229159cbc426d23cdd`). Its 114 package and 47
API/native inputs match the committed source. The offline, read-only proof at
1 CPU/512 MiB confirmed all ten member fields, required platform-role validation,
and no User ORM read in the three profile serializer functions; its container
was removed. The 12.416 seconds is build plus offline verification, not startup
latency. The existing Identity owner artifact was reused without a rebuild.

The full ten-module/seven-gate objective remains active. This internal read
extraction does not qualify full Identity, provider-language replacement,
production capacity or primary cutover.

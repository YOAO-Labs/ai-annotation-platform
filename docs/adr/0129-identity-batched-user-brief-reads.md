# 0129 — Identity batched UserBrief reads

- **Status:** Accepted; implementation and local qualification pending
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

Root passed the legacy helper characterization baseline on an owned disposable
0248 database and cleaned up with no errors. Its six-user fixture, recorded in
`apps/api/tests/fixtures/identity_briefs_legacy.json`, covers global and project
responses plus a 1,201-ID request. Legacy embedded query counts were empty=0,
global=1, project=2 and large=1. This is compatibility evidence for the old
helper, not a new Identity-owner/runtime result or a performance comparison;
the single host-time sample is not capacity/SLO evidence. The new portable
source, installed artifact, SQL roles, HTTP/Node bindings, restore behavior and
API caller transaction boundaries remain unqualified. Do not infer owner
throughput or deployment readiness before Root's actual evidence.

The full ten-module/seven-gate objective remains active. This internal read
extraction does not qualify full Identity, provider-language replacement,
production capacity or primary cutover.

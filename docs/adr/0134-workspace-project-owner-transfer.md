# 0134 — Independent Workspace Project Owner Transfer

- **Status:** Proposed
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Direct project owner transfer still holds Identity account locks and a Workspace
Project lock through an API-owned SQLAlchemy workflow. A single property setter
is not a deployable command boundary. Its caller-owned transaction also differs
from membership's legacy successful commit behavior and must remain explicit.

## Decision

Add the closed `aap-workspace-owner-transfer/v1` command to `aap-workspace`.
Requests contain schema, operation UUID, actor UUID, project UUID and target UUID.
Success binds every identifier and returns previous owner, aware updated_at and
changed. Failures carry only status_code, JSON detail and an explicit rollback
flag. Reject duplicate keys, nonfinite values, unknown fields and wrong bindings.
No public route, DTO, SDK field or idempotency header is added.

Native `aap_workspace_owner_transfer_v1` owns current authorization, ordered
account UPDATE NOWAIT locks, Project UPDATE NOWAIT lock, owner mutation and
private success receipts. Identity facts are obtained through the existing
account-lock capability; Workspace receives no raw User-table access. The
`workspace_owner_transfer_client` LOGIN has exactly two RPCs: policy and transfer.
No membership or business Audit effects are introduced.

Preserve the old precedence: active super-admin actor, active target, Project
existence/lock, then project-admin target role. A same-owner request does not
UPDATE or advance updated_at. Changed owners retain PostgreSQL transaction-time
updated_at behavior. Raw account-lock errors receive a standard HINT phase marker;
expected Project SELECT-lock errors receive a different marker. Adapters map only
the marker and declared busy SQLSTATEs, never message text or later DML failures.
The borrowed API adapter never commits. Account-lock errors explicitly roll back;
Project-lock errors leave the transaction aborted until caller rollback. Unrelated
pending work retains autoflush/no_autoflush behavior and caller commit ownership.

A private operation UUID binds actor, project, target and normalized request hash.
A nonblocking transaction advisory lock serializes an operation. The immutable
receipt and owner write commit together. Matching replay returns frozen success
without reapplying after supersession, but rechecks current active super-admin
actor and locks the existing Project. Former target status, role or deletion does
not invalidate accepted success. Conflicting reuse returns 409. API transport
loss retries the same UUID at most once within the existing timeout; HTTP and
business failures are not retried and selected paths never fall back.

The existing Workspace runtime can enable membership, transfer or both. It shares
control bearer, timeout and bounded concurrency; readiness checks every enabled
capability. A separate optional owner-transfer Compose file avoids introducing
required secrets into the existing membership Compose. Native SQL, schema and
grants must be installed together on the same PostgreSQL database.

A minimal Node conformance provider uses a pinned standard PostgreSQL driver and
the same two-function credential. It validates the wire and transaction result
without importing or proxying Python. Business policy remains Native-owned. This
is a local single-command transport replacement experiment; it does not select a
production language or replace the full Workspace module.

## Consequences

The public adapter retains hydration and route-owned commit/refresh, while the
command can run in a separate process or image. A remote path adds transport and
failure coordination; standalone owner transactions do not include unrelated API
pending work. Workspace and Identity still compose one PostgreSQL transaction,
so this decision does not allow separate databases. Project creation,
configuration/templates, lists/mentions and other module gates remain open.

## Verification

The actual old implementation was captured before extraction at commit
`1a0526a4fc7a67f3867c999465b9623b3dc9094f`: 17 HTTP calls with a fresh transaction
per request and four direct caller-transaction probes. The frozen fixture is
`apps/api/tests/fixtures/workspace_owner_transfer_legacy.json`. API SQLAlchemy
statement counts exclude SQL executed inside Native functions and are not
capacity measurements. Current implementation qualification is recorded in
`docs/plans/1792160873_backend-module-decoupling.md`; legacy capture alone does
not qualify the new implementation.

The scoped final union passed 66 distinct selected cases: 59 database/process
and seven pure/static cases, with no effective failures or skips. Failed
fixture/diagnostic attempts were replaced by successful focused retries.
Source and noneditable installed suites each passed 85 cases; all 14 installed
package files match source without API/ORM/Celery imports. Four public bindings
match all 17 captured HTTP responses and preserve the four caller transaction
probes. Native/grant, malformed-reply rollback, raw DML and deferred-commit
failure, real reply-loss recovery, two-capability coexistence, nine affected
restores, and Python/Node image restart/supersession checks passed.

The Python owner image matches 14 package files; the Node image matches five
runtime inputs and uses pg@8.16.3 as UID 1000. The fresh API image matches 136
installed package files and 72 application/Native inputs, unchanged OpenAPI and
head 0254. The API cold proof explicitly supplies ordinary UID 10001, read-only
filesystem, no network and fixed budgets; its default image user is unchanged.
Architecture separately passes 318 cases with 133 existing inapplicable rules
skipped. Codegen and the 509-route/93-module catalog remain unchanged. Exact
code/image identities are in the implementation ledger. The accepted local
transport experiment does not qualify full Workspace, separate databases,
production capacity or the ten-module/seven-gate objective, and no primary or
production cutover, push or remote CI was performed.

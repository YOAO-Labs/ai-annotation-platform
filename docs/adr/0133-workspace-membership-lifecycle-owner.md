# 0133 — Independent Workspace Membership Lifecycle Owner

- **Status:** Proposed
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Workspace membership creation, role preview/change, and removal still combine
current authorization, account and project locks, Labeling responsibility
handoff, audit, and membership writes through API-owned SQLAlchemy workflows.
Moving only the membership table write would split the transaction and leave
authorization and cross-owner effects tied to the API process. Invitation
acceptance already has its own owner and must not gain a second membership
writer. Project transfer, configuration, templates, listing, and mentions are
separate Workspace responsibilities and remain outside this slice.

## Decision

Create a portable `aap-workspace` package with a closed
`aap-workspace-membership/v1` command contract and a Workspace membership
manager. The manager preserves the existing Python policy, snapshot digest,
blocker ordering, audit context defaults, and public error mapping. It executes
each command in one caller-owned transaction: native prepare returns bound
current facts or a replay; the portable manager evaluates policy and any
preview token; native apply performs the mutation, cross-owner effects, typed
Audit, and immutable success receipt atomically.

The command set is limited to `add_member`, `preview_role_change`,
`change_role`, and `remove_member`. Public API methods, request/response models,
and their existing authorization dependencies remain unchanged. The API
hydrates any required response display data only after the owner operation has
completed; the portable contract does not return ORM objects or User
credentials.

Native capability `aap_workspace_membership_v1` owns membership rows, command
receipts, lock/fence enforcement, and calls narrowly scoped Identity, Labeling,
and Audit capabilities. The dedicated `workspace_membership_client` LOGIN may
execute only `membership_policy()`, `prepare_membership_command(jsonb)`, and
`apply_membership_command(jsonb)`. It receives no raw User, ProjectMember,
Labeling, or Audit table access and no AUTH, FENCE, or workflow-append grants.
Labeling handoff and lock functions are granted only to the Workspace owner;
the existing private invitation group-resolution capability is retained.

Prepare and apply share the same PostgreSQL connection and transaction. The
Native prepare guard is transaction-local and binds the operation, actor,
project, request, target membership/version, and transaction. Apply rechecks
that binding and current authority, uses ordered NOWAIT locks, preserves the
legacy explicit rollback/error behavior, performs the required Labeling and
Workspace writes, records the typed Audit, and stores a frozen receipt before
the caller commits. A failure does not create a successful receipt. The
default API data adapter continues using its caller Session and does not commit
or roll back unrelated caller work; configured independent adapters preserve
the same prepare/policy/apply transaction boundary.

The Native receipt binds operation UUID, actor, project, operation, request,
and audit context. Matching replay returns the frozen successful response
without reapplying effects or requiring live target foreign keys; reuse with a
different request conflicts. Current actor/project authority is rechecked.
Public calls continue creating fresh operation UUIDs and retain the existing
membership CAS behavior; no public idempotency header or stale-CAS success is
introduced.

An optional API binding may use either the existing caller transaction, a
dedicated Workspace data URL, or the private Workspace runtime URL with a
separate control bearer. The standalone runtime requires its restricted
database URL and control token. Compose host selectors are
`WORKSPACE_RUNTIME_MEMBERSHIP_DATABASE_URL_FILE` and
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`. The runtime defaults to host
`0.0.0.0`, port `8172`, a 30-second request timeout, and eight concurrent
requests; the API client timeout defaults to 30 seconds. These selectors do
not change the public API or its OpenAPI schema.

Native SQL transports `change_role.preview_token` and `reason` as canonical
ASCII JSON string literals inside their string fields. Public HTTP values
remain unchanged. The required `opaque_text_codec` policy field is
`json-string-ascii/v1`; both SQL adapters check it in the execution transaction,
and standalone readiness checks it too. A mixed migration/runtime codec cannot
execute commands. The request fingerprint binds these encoded values. Policy compares
the original token, and Native converts reason to JSONB only at Audit detail
construction. This preserves stale-version/snapshot conflicts before a NUL
reason can produce the original Audit error and transaction rollback.

Local scoped qualification passed 119 distinct selected cases: 96 database or
process cases and 23 pure/static cases, with no effective failures or skips.
Focused retries replace earlier failed or gated attempts in this count.
Source and noneditable installed suites each passed 51 checks; the installed
Workspace package and standalone image match nine source files. A fresh API
image verifies 131 installed package files, 65 application/Native inputs,
the public OpenAPI snapshot and migration head 0253. Its offline probe uses
an explicit ordinary UID, read-only filesystem, no network and fixed budgets;
these results do not establish production capacity or change the API image's
default user configuration.

Both public API bindings passed the 20 captured legacy HTTP cases plus four
NUL conflict cases, with four business Audits and 22 HTTP access Audits each.
The legacy capture shares an outer transaction timestamp, so unspecified
member-list tie ordering is not compared: current `assigned_at DESC` is checked
on actual timestamps and normalized rows are compared by user ID. Ordered
snapshot arrays retain their UUID ordering. Five literal/control/Unicode
reasons retain exact Audit values, matching replay returns frozen receipts,
and changing the reason conflicts. A current NUL reason retains SQLSTATE
22P05 and rollback; missing codec metadata blocks execution in all three
bindings. Installed process exit/restart, capability revocation, backup
restore, populated downgrade refusal and empty downgrade passed. Eight
existing Identity restores remain a separate affected regression scope.

Node self-checks passed 37 checks, including eight frozen digest vectors.
The Node peer is a consumer and exercises only one controlled native add;
it is not a replacement provider. Architecture checks passed 318 cases,
with 133 inapplicable rules explicitly skipped; these are separate from the
119-case selected qualification. The public API acceptance still uses the
transitional `legacy_runtime` assembly; only the member command owner is
qualified with the exact three-function LOGIN. No primary or production
cutover, remote CI, push, or provider-language replacement is claimed.

## Consequences

### Positive

- Membership policy remains portable while Native SQL owns current database
  authority, locks, cross-owner writes, Audit, and replay receipts.
- Invitation acceptance continues using its existing Identity owner; there is
  one membership writer for invitation and administrative membership flows.
- The API can retain its public contract and default caller-owned transaction
  while an independent Workspace runtime is introduced behind a private
  control boundary.

### Negative

- The command spans Workspace, Identity, Labeling, and Audit capabilities in
  one PostgreSQL transaction. It does not yet permit those owners to move to
  separate databases.
- A remote runtime adds a network hop and requires coordinated migration,
  restricted grants, readiness, and failure handling.
- Only membership lifecycle moves. Project creation/transfer, project
  configuration and templates, member listing, mentions, and other Workspace
  consumers remain on their existing paths.

## Alternatives Considered

- Keep the full policy and all writes in API ORM workflows: this does not
  establish an independently deployable Workspace command boundary.
- Move policy into SQL: this duplicates or changes Python policy semantics
  and makes cross-language policy iteration harder to qualify.
- Split Workspace, Labeling, Audit, and Identity writes across services or
  databases: this breaks the existing atomic transaction and would require a
  separate saga/outbox design.

## Notes

The implementation is a local architecture slice, not a provider-language
replacement. The full Workspace and Identity goals, ten-module/seven-gate
qualification, production capacity validation, and primary cutover remain
unfinished. See the [Workspace runtime guide](/api/guides/workspace-runtime).
The repository implementation ledger is
`docs/plans/1792160873_backend-module-decoupling.md`.

The pre-change legacy baseline separately passed 20 HTTP cases, eight frozen
preview-digest vectors, and four Audit checks. Those results capture old
behavior only; they are not qualification evidence for the new owner.

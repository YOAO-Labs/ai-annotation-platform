# 0145 — Workspace Project Access Facts for Independent Owners

- **Status:** Accepted; selected local prerequisite checks passed
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Named pipelines belong to Inference, while project access policy and fences
belong to Workspace. Workspace already consumes the pure Inference validator.
A reverse core dependency on Workspace runtime would form a cycle. A remote
permission response cannot retain locks for a later Inference transaction.

Actual legacy apply observations establish Project KEY SHARE semantics:
ordinary counter updates can commit while Project FOR UPDATE NOWAIT conflicts.
The older Native manager helper uses SHARE and narrower row decoding, so it is
not equivalent. A source pipeline can disappear after its snapshot was read;
the accepted copy still succeeds. This extraction must preserve those phases.

## Decision

Publish the existing pure access values, policy, Project query wire, complete
Project/member codecs and read-authority sequence from `aap_workspace_contract`.
It remains stdlib/Pydantic only, without Workspace runtime, Inference, API, ORM
or transport imports. Move implementations and actual callers, with no old
forwarding modules. The unchanged query schema moves with its producer and is
explicit package data for noneditable installation.

Unlocked visibility continues through the existing Project Query contract. Add
one fixed Workspace-owned Native capability:

```text
aap_workspace_v1.project_access_step(actor_id uuid, project_id uuid, phase text)
```

Its phases are `lock_actor_share`, `lock_project_key_share` and
`lock_member_share`. The envelope binds protocol
`aap-workspace-project-access-step/v1`, both UUIDs and phase, with one fact:

- Actor: null or current actor ID, platform role and active state.
- Project: null or the existing complete 43-field row text, including the typed
  Labeling counter projection in the same Project statement.
- Member: the existing complete nine-field member-row text.

These are facts rather than permission booleans. The caller decodes each result
before continuing and applies canonical Workspace policy. The function validates
arguments, returns missing/inactive facts without deciding permission, and does
not write business rows, own a transaction or create receipts.

Workspace reuses its current private account/Project/member helpers internally;
Inference receives no private-helper grant. The public function has a fixed
search path and UTC/date/float settings and belongs to the Workspace code role.
Only the Inference owner receives the additional schema usage and fixed function
execution capability, without raw foreign-table access or a new login. Downgrade
removes only this function with RESTRICT.

## Transaction and failure contract

Use one read/write transaction and connection for the ordered Account SHARE,
Project KEY SHARE, optional Member SHARE and final business write. Autocommit
calls or independently committed HTTP permission checks do not provide this
fence. Existing separate reads retain their READ COMMITTED statement snapshots.

Lock errors retain SQLSTATE 55P03, 40001 or 40P01 with fixed HINT markers
`aap.workspace.project_access.account_busy`, `.project_busy` or `.membership_busy`.
Provider exception bodies are not a public response. Invalid arguments raise 22023. Complete Project/member temporal decoding remains at its existing phase;
finite out-of-range values are not silently discarded.

The module move changes no public REST/SDK field or schema. Legacy fingerprints
and captured fixtures remain immutable. Executable old-code probes may adapt
only the import of byte-identical moved policy after checking the frozen hash.

## Qualification scope

This is a prerequisite for the independent named-pipeline owner, not completion
of that owner, Workspace, Inference or language replacement. Fifteen distinct
selected database cases pass without skips: ten compatibility cases, three new
Native phase/grant/contention cases, one full migration-validator case and one
new owned database dump/restore case. The compatibility cases replay all seventy
frozen named-pipeline HTTP samples and both lock observations without rewriting
either fixture. Those observations are not additional test cases.

The full migration validator passes fresh, reversible, reversible-suffix,
forward and restore phases at head 0270, with no owned database leftovers. The
new nonempty dump/restore check compares function definitions, named owners,
SECURITY DEFINER settings and ACLs, then runs all three phases as Inference in
both source and clone. Clone-only account deactivation and membership deletion
leave source facts intact. It checks role membership as metadata rather than
using an owner-authenticated session to prove SET ROLE denial. Its first run
found a test-only PostgreSQL internal-char decoding assumption; casting the
catalog field to text fixed it. The failed run and its clean resource teardown
are retained separately.

Workspace and shared-contract source suites pass 299 and 21 cases; fresh
noneditable installation passes the same suites. Thirteen installed packages
match 252 source files. The 49,025-byte query schema is present at its unchanged
SHA-256 `74cb9a87548e9ef71d1df23a1a9af3bf532f1850338cf66b1c37cc2e71a468eb`.
Cold shared imports load no Workspace runtime, API or ORM; old module paths are
absent. Focused API/architecture checks separately pass 372 cases with 142
existing inapplicable lazy-rule skips; no skipped rule counts as qualification.
Two verifier-selection tests also pass. The earlier mixed pure selection's
unavailable default test database is retained as a failed attempt and superseded
by the owned compatibility run, rather than counted as a pass.

Canonical evidence is
`.worktree/workspace-project-access-facts-qualification.json`, including source,
installed, migration, recovery, failed-attempt and cleanup receipts. Existing
Guide container qualification remains attached to its recorded commit; it is
not reused as a current image fingerprint after this package relocation. This
prerequisite does not qualify a new Inference process, container or language
provider. No production runtime is switched here.

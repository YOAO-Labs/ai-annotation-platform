# 0147 — Inference Pool Policy and Workspace Backend Bindings

Date: 2026-10-21

Status: Accepted

## Context

Backend control still combines registry/pool topology, project enablement, health,
routing leases, credentials and backend HTTP inside API services. Moving a registry
query facade would leave the real write and execution boundary coupled. The next
complete owner must cover these operations and their actual consumers.

Two prerequisites can move without changing transaction boundaries. Capability
canonicalization and fingerprints are pure Inference policy. Clearing a deleted
pool's Project primary bindings is a Workspace write currently issued by the
registry-deletion coordinator.

## Decision

Move capability normalization, fingerprint and diff policy into
`aap_inference.pool_capability`. Remove the former API module and migrate every
caller directly. Preserve its original function/class AST and the exact JSON
float, Unicode, ordering and default behavior in the frozen legacy vectors.
This is a portable Python policy prerequisite; a non-Python replacement still
needs contract and equivalence qualification.

Migration0272 publishes
`aap_workspace_v1.clear_primary_backend_bindings(uuid) -> bigint`. It rejects
null input, clears only Projects pointing to the supplied pool, updates their
timestamps using transaction time, and returns the affected count. It uses one
SQL UPDATE in the caller's connection and transaction, with no independent commit
or additional admission decision. Existing registry-deletion authority, retention,
quiescence, Audit and final commit remain in their established order.

The function is SECURITY DEFINER with fixed search path and serialization settings,
and no PUBLIC execute. General Alembic migration does not require deployment role
names; owner reassignment and exact Inference execute belong to
`module-data-grants.sql`. The transitional API runtime is explicitly granted this
one function because it still owns the coordinator. No Project raw-table grants,
new LOGIN, HTTP endpoint, DSN or broader pipeline-client permission are added.

The API adapter preserves conditional ORM autoflush and `no_autoflush`, then calls
the fixed function. It never commits or rolls back. It does not synchronize loaded
Project objects in the identity map; the current sole caller does not load those
Projects. A future reuse needing current ORM objects must refresh them explicitly.

## Verification and limits

The immutable old corpora cover58 registry/project and38 pool/member observations;
the original corpus bytes retain old failure phases, including a member removal
that committed before returning500. Raw transport artifacts are evidence rather
than deterministic replay fields. Corpus observations are not test-case counts.

Real owned database checks cover match/no-match, timestamps, outer rollback,
autoflush/no-autoflush, exact owner/ACL/raw-table denial, existing deletion flows
and nonempty dump/restore. Pure source and noneditable installed package checks
verify capability vectors and package isolation. Full migration validation remains
required for every new Native head; failed attempts stay in separate receipts.

The local prerequisite ledger selects42 distinct passing acceptance cases:41 DB/process and one static schema case. The
mixed initial run stays failed; its15 named successful cases are selected, while
the failed pool replay has a later successful result after the explicit response
fix. The old error was intermittent because ORM identity-map retention can determine
whether get() reloads the expired field. Four member mutation routes now explicitly
refresh after commit; a retained-object regression reproduces the old failure and
checks all four successful projections, one Audit, and repeated-remove404.

Source and fresh noneditable Inference each pass92 cases. Five packages/forty files
match the installed and image manifests. Both new owner/API images pass restricted
offline cold imports; the API also matches1,235 API/Native inputs, public OpenAPI
and migration head0272. The existing protocol dependency retains transitive
FastAPI. Native0272 passes five full migration phases with exact derived-database
cleanup. These checks use macOS ARM64 and local Linux ARM64 containers; they do
not measure Backend control production capacity. Canonical local evidence is
`.worktree/inference-backend-prerequisites-qualification.json`.

This does not establish a complete independent Backend control runtime, independent
database, GPU lifecycle owner, non-Python replacement or production capacity. The
named-pipeline owner keeps its two-RPC contract. Primary and production cutover
and further Go work are outside this slice.

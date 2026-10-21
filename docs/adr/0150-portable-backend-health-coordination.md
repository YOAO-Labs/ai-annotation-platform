# 0150 — Portable Backend Health Coordination

Date: 2026-10-21

Status: Accepted

## Context

The complete Inference control boundary still includes registry/pool writers,
health evidence/apply, Redis routing, GPU lifecycle and actual consumers. Backend
transport and Workspace credential-owning setup probes do not close it. The
health coordinator in API still combines endpoint capture, unlocked backend I/O,
GPU evidence, write fences and capability reconciliation. Copying only its query
projection or committing health in a second service transaction would not retain
the current behavior of drift acceptance and GPU callers.

## Decision

`aap_inference.health.BackendHealthCoordinator` becomes the single implementation
of the complete established health state machine. Its typed data port borrows the
caller's state and transaction; explicit client and capability-limit factories
supply the existing transport and configured limits. The same owner provides
`reconcile_pool_capability`, including the existing Python canonicalization and
fingerprint implementation. API wrappers delegate both operations; no second
health or pool-reconciliation policy stays in the API.

`InferenceHealthData` is a transitional API binding. It uses the exact injected
AsyncSession, original queries and full ORM row materialization. It does not open
a session, commit, rollback or close. Mutable row protocols deliberately preserve
caller identity-map and pending unit-of-work behavior; they are not a JSON or
cross-language wire model. Application code still supplies MLBackendClient and
retains the public permission, audit and commit boundaries.

The phases remain: challenge validation; full registry materialization; optional
unlocked membership read and database start clock; health and conditional setup;
caller database observation clock; lifecycle digest/proof; registry row lock and
endpoint/resource recheck; optional resource advisory lock and try promotion
barrier; ordering floor and membership row recheck; registry update/refresh; pool
reconciliation and caller flush. No row lock is introduced before backend I/O.
The limits factory is evaluated after setup returns, as in the original argument
order. Actual setup/extraction failures remain fail-open; other existing error
and early-return phases are retained.

Project/global health handlers, drift acceptance, periodic health and GPU cold
refresh continue through this one coordinator. Drift acceptance still combines
health, accepted pool mutation and Audit in its outer transaction. The separate
stats publisher reads backend health and writes Redis without setup/database
apply; it remains outside this coordinator and part of the unfinished boundary.

## Verification and remaining scope

Before changing the production code,24 actual old observations were frozen from
12 controlled HTTP scenarios with autoflush disabled/enabled. They retain exact
SQL/HTTP phase order, result/error timing, caller state, external visibility,
GPU proof and pool snapshot/generation. Source capture pins6590de1b2 and the
original coordinator/transport hashes. Existing40 health/worker cases also passed.
The fixture SHA is7a9cfd4808e8e2b97d7b84c43621f9e724b6b2b82f0bf6dfcf74d263f7f83418;
it is immutable and excluded from formatting. Capture is explicit and cannot
replace an existing baseline. Replays must preserve the original fixture bytes.

This is a complete policy/coordinator relocation prerequisite, not an independently
deployable health service. Fixed Native reads/apply, transaction-bound encrypted
credential snapshots, authenticated provider observations and all actual bindings
remain required. Initial whole-row JSON materialization must still precede GPU
membership/start reads. Capabilities return before the caller's observation time;
managed-lifecycle proof is finalized afterward. No remote commit or unverified
routability/quiescence/health boolean may replace these boundaries.

The existing health reconciliation order is member then pool, whereas member
mutation paths use pool then member. That inversion can deadlock. This extraction
preserves it for equivalence and does not claim contention-safe full-owner status;
a consistent lock order and controlled race acceptance are mandatory follow-up.
Native head274 and all existing deployment selectors remain unchanged. Full
Inference, all ten modules/seven gates, non-Python replacement, production/primary
cutover, Go expansion, real GPU and production capacity remain unqualified here.

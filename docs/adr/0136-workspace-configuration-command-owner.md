# 0136 — Workspace Configuration Command Owner

- **Status:** Proposed; implementation and local qualification in progress
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Related:** ADR-0135

## Decision

The portable Workspace manager coordinates six configuration operations:
project creation/update and template creation/update/deletion/duplication.
The closed private protocol is `aap-workspace-configuration/v1`. Its request
binds operation UUID, operation, actor, target, structural selectors and an
opaque ASCII JSON payload. The public request models remain the admission
boundary; the owner does not validate them a second time.

The opaque carrier preserves unset versus null, nested nonfinite values and
escaped NUL until the existing policy/write phase. Its outer JSON is strict.
Native does not cast the whole payload to JSONB. Fixed typed column parameters
preserve the distinct TEXT and JSONB conversion failures at persistence.
Success returns resource IDs so public response validation can remain after
commit, including existing committed-null failures.

Native exposes only `configuration_policy()` and
`configuration_step(jsonb,text,jsonb,text[],text[])` to the separate
`workspace_configuration_client` LOGIN. Steps and field names are closed sets;
they do not accept arbitrary SQL or repository operations. Workspace owns its
configuration rows and private operation state. Identity, Assets, Labeling and
Inference provide typed facts/effects without foreign raw table grants.

An operation uses one connection/transaction. Current account, Project and
membership fences preserve their lock modes and error order. A source backend
retains the draft/flush/enable/setup/final-authority sequence. Other creates
fence authority before insertion, and project updates enable their backend only
after final authority. Staged primary bindings retain the old autoflush phase.

Snapshot boundaries are explicit. Project threshold reads cast physical REAL
to DOUBLE before JSON projection; templates already use DOUBLE. Python selects
dirty fields using the old equality behavior rather than comparing cast SQL
values. Assets media reads remain batched at 500. Typed inner projections share
the appropriate calling query snapshot; separate conditional phases use fresh
reads. See [PostgreSQL volatility and snapshots](https://www.postgresql.org/docs/17/xfunc-volatility.html).

Private success receipts bind the request and prevent repeated writes. Replay
rechecks current authority and does not resurrect deleted resources. Delete
replay uses its frozen scope. Deferred draft guards prevent unfinished mutation
steps from being committed. Neither mechanism commits a borrowed caller.

## Bindings

The default API data port borrows its Session and never commits or rolls back
ordinary rejections. Project allocation precedes its forced caller flush.
Standalone DSN/HTTP bindings own all operation business writes and their commit.
They cannot roll back unrelated pending work in another API connection.
Selected remote failures do not fall back to another writer. Raw owner failures
retain the plain public 500 envelope; transport/invalid private replies use503.
A connection cleanup failure cannot replace an already committed result.

Standalone HTTP supports the private configuration endpoint and a separate
Compose profile. API selectors are prepared; switching the six public callers
and deleting their moved ORM implementations are the next integration step.
Point-cloud evaluation promotion, guides, class rename, imports and other
adjacent writers retain their existing boundaries.

## Evidence and open gates

The existing 106 HTTP and 17 caller captures remain immutable. An additional
pre-port fixture records numeric dirty/timestamp behavior, ten account-lock
caller cases and two setup-demotion races at the foundation checkpoint.

Local pure Workspace checks passed126 cases before the subsequent batching
fix; its affected video suite then passed8 cases, including 501-item and empty
reads and missing-source precedence. Twelve API binding checks and three
connection-cleanup checks passed; these overlap the Workspace suite. Five
owned-database cases passed at0257, including restricted credentials, typed
read facts, all six commands, update/delete replay and absence of draft state.
Earlier failed attempts exposed and corrected owner-column grants and two
Native reply/selector mismatches. All their owned resources were cleaned.

This is a local implementation checkpoint. Public-caller integration, full
frozen-response comparison, all binding/failure/lock/restart/restore checks,
final installed artifacts and images remain open. Full Workspace, the other
modules and the ten-module language-replacement objective remain unfinished.
There is no primary runtime or production cutover, push or release.

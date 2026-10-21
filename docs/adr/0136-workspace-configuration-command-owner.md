# 0136 — Workspace Configuration Command Owner

- **Status:** Proposed; local command-slice qualification complete
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
Compose profile. Six public callers now use the same owner through explicit
selection. Their moved API workflow and ORM configuration writers are removed;
the API retains commit/response assembly and live raw-row reads.
Point-cloud evaluation promotion, guides, class rename, imports and other
adjacent writers retain their existing boundaries.

## Evidence and remaining module work

The existing 106 HTTP and 17 caller captures remain immutable. An additional
pre-port fixture records numeric dirty/timestamp behavior, ten account-lock
caller cases and two setup-demotion races at the foundation checkpoint.

The pure Workspace suite passed132 cases, including 501-item batching, empty
reads, missing-source precedence, deep JSON and connection cleanup. Twelve API
binding checks passed separately, making144 checks in that source group. Five
owned-database cases passed at0257, including restricted credentials, typed
read facts, all six commands, update/delete replay and absence of draft state.
Earlier failed attempts exposed and corrected owner-column grants and two
Native reply/selector mismatches. All their owned resources were cleaned.

The six public callers passed57 affected API cases. Default borrowed execution
matches all106 frozen HTTP responses and17 caller records; only SQLAlchemy's
internal active flag on three raw write-conversion failures is excluded. SQLSTATE,
transaction ownership, unrelated pending work, lock-abort behavior, deferred
commit rollback and external setup probe order remain checked. Old SQLAlchemy
statement traces cannot observe Native SQL; they are not counted as matched.
The comparison caught and corrected a missing-pool success fallback. Two focused
pure regressions now require the original failure for both create and update.

Dedicated DSN and independent HTTP execution each match the same106 responses,
probe order and committed-null cases. Actual process tests also passed for a
lost committed reply, same-ID retry, post-restart replay and unavailable-owner
failure without local fallback. Separate real database tests preserve numeric
update/no-op counts, caller transaction timestamps, failed transaction state,
autoflush/account contention and both setup/revocation races.

Four failure/recovery cases passed against real PostgreSQL: raw conversion
errors roll back, forged finish replies roll back, unfinished commands cannot
commit, and backup/restore preserves receipts, replay and current revocation.
Nine existing protected Workspace/Identity restore cases also passed without
skips. Full migration validation passed at0257, including fresh installation,
173 reversible downgrades, the83-revision reversible suffix, forward migration
and backup/restore. Its four derived databases were removed without cleanup errors.

Noneditable installed suites passed132 Workspace and27 Inference checks outside
the source tree. Their25/7 package files plus14 shared dependency files match
source; the environment has no API, SQLAlchemy, Celery, Boto3 or Torch package.
The standalone owner image matches all46 files. Its offline proof and actual
configuration-only HTTP/public API flow passed as UID10004, read-only, with all
capabilities dropped, no new privileges,64 PIDs,1 CPU,256 MiB and32 MiB tmpfs.
Only two read-only secret files are mounted. Restart preserves receipts and
deleted-template replay without overwriting later edits; revoked actors are
rejected. Containers, secret files and disposable database resources were removed.

A bounded local profile completed16 updates to16 independent projects at
concurrency4, with16 successes, no failures and no OOM. Elapsed time was420 ms,
p50 was103 ms and nearest-rank p95 was114 ms. PostgreSQL and the caller are outside
the owner container budget. This is a local interoperability/load observation,
not a production capacity or SLO result. Shared-target NOWAIT semantics remain
unchanged; this profile does not measure their conflict rate.

The restricted offline API image matches168 installed package files and131
application/Native inputs, public OpenAPI and migration head0257. It uses
UID10001, no network, a read-only filesystem,1 CPU and512 MiB for the cold proof;
this does not change the API image's default user or transitional runtime role.
Architecture checks passed322 cases with135 inapplicable parameter cases skipped.
Final read-only review found no additional actionable issue. Normal commit hooks,
documentation build and whitespace checks passed.

The qualified code checkpoint is `a82f83cc5cf9b02b3b62be4c16bcb2e1641b0565`.
The Workspace image is `sha256:5de0eca63baa2109f8c0625f085ee73832b2d0c36189458420cfc11b758b0bba`;
the API image is `sha256:9dc72f68e3d995daeaa217f6727f326c08901fcf204d0c9f07e63e980cfbb3f8`.

This closes the bounded local qualification for six configuration commands.
Template query and response hydration ownership now follow
[ADR-0137](0137-workspace-template-query-owner.md). Project reads still use API
ORM paths; guide assets, class rename, named pipelines, import project creation and adjacent
writers retain their existing owners. Full Workspace, the other modules and the
ten-module language-replacement objective remain unfinished. There is no full
second-language provider, independent database or production capacity claim.
Primary source, shared links and its four original running containers were
preserved. There is no primary runtime or production cutover, push or release.

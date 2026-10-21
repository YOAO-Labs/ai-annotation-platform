# 0142 — Labeling Class Usage Query Owner

- **Status:** Accepted; selected local qualification complete
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

`GET /api/v1/projects/{project_id}/class-usage` combines current Workspace manager
authorization with two Annotation reads. The former belongs to Workspace; the
latter belong to Labeling. The existing Workspace annotation counter excludes
canceled annotations and cannot replace this endpoint's active-only aggregates.

The immutable legacy fixture freezes 16 actual HTTP observations, ordered SQL,
an independent inter-statement commit and a genuine attribute decoding failure.
Its SHA-256 is
`c2e4517d3f103156c20c0eda0f52f804601073bf3e73d0c91e62c8b7176e647e`.
The database accepts an Annotation scoped to the target Project with a foreign
Task; it counts by Annotation.project_id. A stored 5000-digit JSONB numeric
fails full Python decoding and returns plain HTTP 500 after class aggregation.

## Decision

Keep the public API as the trusted coordinator. Credential admission remains in
API. It calls `require_project_manager` on the existing Workspace Project Query
owner before calling a separate Labeling projection. The guard resolves initial
Project, current actor, refreshed Project and optional complete nine-column
membership once. Visible nonmanagers retain the original manager denial (403)
after full membership decoding. Missing membership, unknown roles and
out-of-range dates retain their earlier errors. The repeated-phase `/access`
operation is not used.

Labeling accepts only its authenticated private peer, request UUID, operation and
Project UUID through `aap-labeling-class-query/v1`. The operation is
`get_project_class_usage`; replies echo the complete bound request and contain
only the two count maps in `data_json`. Keys and whitespace have no ordering
promise. Duplicate fields, mismatched bindings, extra fields, booleans and
nonpositive counts are rejected; integer values retain their exact JSON spelling.
The private projection does not accept a caller `is_manager` flag, public user
credentials or Workspace implementation dependencies. Its trusted peer must
authorize before invoking it, as with existing private profile projections.

The portable Labeling manager reads class groups, then full attributes. Both
statements filter only Annotation.project_id and is_active, including active
canceled and AI rows. There is no Task join, tool-unit restriction, write, lock,
receipt or response cache. Empty class names are omitted. Every attributes value
is decoded before counting top-level dictionary keys; nonobjects do not contribute.
No partial class result is returned after attribute failure.

Native0266 exposes `class_query_policy()`, `class_counts(uuid)` and
`attribute_rows(uuid)` in `aap_labeling_class_query_v1`. The latter returns JSONB
text, preserving complete decoding and the legacy large-integer failure. The two
row-set functions avoid introducing a single aggregated JSON value limit. All
three functions are STABLE SECURITY DEFINER with fixed search paths and
Labeling ownership. `labeling_class_query_client` receives only these functions,
without raw tables, column grants, memberships, CREATE or TEMP privileges.

Default API adapters share the existing caller Session, including autoflush and
no_autoflush. They never commit, roll back or close it. An explicit Labeling DSN
or HTTP URL selects an independent provider and cannot fall back locally;
conflicting selectors or an empty DSN file fail closed. Independent connections
use read-only READ COMMITTED transactions and cannot see caller pending work.
Workspace and Labeling remote calls do not share a transaction or common snapshot.

The existing function-only credential verifier moves from Workspace to the
neutral `aap-owner-data` leaf. Both owners use it without a cross-domain Python
dependency; the former private implementation is removed and real callers move.
The new Labeling runtime has its own package, lockfile, image and Compose entry.
It needs a Labeling DSN and control bearer, with no profiles, API, ORM, queue,
object-storage or ML dependency. Python retains the existing JSON semantics;
another-language provider remains a separate replacement gate.

## Verification and limits

Source Workspace/Labeling suites and noneditable packages pass 244/27 cases;
all 42 Workspace, eight Labeling and two neutral utility files match installed
bytes. Labeling installs without Workspace, Identity or Inference. Private HTTP
and transport cases preserve plain500, unavailable503 and fail-closed selectors.
An initial canonical-JSON ordering restriction and empty-file fallback are
reproduced and corrected before passing follow-ups.

The actual narrow role rejects raw Annotation/Project/User reads, TEMP and other
owners' functions; lost execute permission and an extra column grant both fail
readiness. Active canceled counts and complete large-number text/decoding pass.
Nine existing feature/authority/migration cases and three new role/codec cases
pass on head0266. The initial grant check correctly rejects missing class_name
and attributes privileges; only these two owner columns are added.

All 16 frozen HTTP samples match each borrowed, dedicated and installed-HTTP
binding, with both coordinated owners selected in the corresponding mode.
The semantic interleaving sample pre-seeds its row and is not phase evidence.
Three separate real database cases gate class aggregation, commit a new class
and attribute externally, then observe the old class map and new attribute key.
The test-only gate preserves and restores function OID, owner, ACL and definition.
Refreshing the observer's cached statistics snapshot makes all three cases pass,
without a longer deadline or production lock. Failed reports remain separate;
owned resources are removed after each run.

The changed Workspace artifact also passes the nine existing Project, Directory
and Access compatibility cases: 79 frozen public observations per binding remain
unchanged across borrowed, dedicated and installed-HTTP modes.

The default adapter also preserves unflushed work under `no_autoflush`, flushes
normally when enabled, leaves that work invisible to an independent connection,
and lets the caller roll it back. Full migration validation passes a fresh
266-upgrade chain, the 173-step reversible prefix, the 92-step reversible suffix,
authentic forward conversion and pre-conversion backup restoration, with no
owned databases left behind. Static API checks pass 354 applicable cases;
143 architecture rules remain explicitly inapplicable. Public OpenAPI stays
byte-identical.

An owned current-schema dump restores all three functions, ownership and ACLs.
All 16 frozen samples match on both source and clone through each binding; real
borrowed statement-gap probes also match. Disabling the actor only in the clone
is denied by Workspace while the source still permits it. The dump and clone are
removed with no cleanup errors. Initial restore harness failures (missing
interleaved fixture state and clone URL/driver selection) are retained separately
and resolved without changing the frozen baseline or database safety guards.

Current committed production inputs are `d3a3a25212f7ba9a87b0e24b3593ae85ec6b1576`.
Three fresh linux/arm64 images pass restricted offline checks:

| Artifact  | Image ID                                                                  | Verified installed files             |
| --------- | ------------------------------------------------------------------------- | ------------------------------------ |
| Labeling  | `sha256:167bc5c9b12e3c822c43251865abafc0205b55673e719eaadf5ec65d46f41aff` | 10 package files                     |
| Workspace | `sha256:141f400583c23a1355fb0797214e62696f2a3accec3bd01a31f087c2b1c8aa90` | 186 package files                    |
| API       | `sha256:229631c015ff0975c85de5e1f3d664d281385c76e60546bab8608b6e932ef4cf` | 196 package and 209 API/Native files |

Each image matches its committed source bytes. The API cold proof retains current
OpenAPI, head0266 and the guard-before-projection coordinator. An initial probe
incorrectly compared the transport schema to the deliberately different Native
row-set schema; corrected policy/row-set checks pass on the same built image.
The failed probe report is retained separately, and its container was removed.

The actual Labeling container uses a read-only root, nonroot UID10006, dropped
capabilities, no-new-privileges, 1CPU/256MiB and exactly two read-only secret files.
It connects to only the task-owned PostgreSQL network. Workspace runs as a
separately installed HTTP peer without Briefs configuration. All16 public samples
match, including plain500; a newly committed attribute is visible immediately.
Sixteen requests at concurrency4 return the same counts without OOM. Restart
changes the ephemeral port; inspecting the new origin and reconfiguring the API
preserves the result. The database and API are outside this owner resource budget.

The final union has 41 distinct selected database/process cases, no unresolved
failures or skips. It includes ten existing current-schema restore regressions;
retries replace earlier case outcomes rather than increasing the total. The
aggregate is `.worktree/labeling-class-query-owner-qualification.json`. Task-owned
containers, network, credentials, databases and buckets are removed without
cleanup errors. Source/installed suites and the143 inapplicable static rules stay
separate from this union. Documentation build and final diff checks pass.

This qualifies one read slice on a Mac ARM64 host. Complete Labeling, Workspace,
ten modules/seven gates, production capacity, primary cutover and another-language
provider remain unfinished. Go remains experimental.

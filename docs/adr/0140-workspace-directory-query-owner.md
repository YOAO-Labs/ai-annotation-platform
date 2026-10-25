# 0140 — Workspace Directory Query Owner

- **Status:** Accepted; selected directory capability locally qualified
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Member-list and mention-candidate responses previously combined API membership
queries, current Project authority and Identity account reads. The existing
Project query owner does not own these responses. The actual pre-extraction
capture freezes thirteen HTTP observations and two caller transaction probes;
its fixture is retained unchanged.

## Decision

The existing Workspace package owns `aap-workspace-directory-query/v1`, with
`list_members` and `mention_candidates` operations. A closed request carries its
UUID, actor UUID and Project UUID. Seven fixed read actions bind their parameters
and facts; neither arbitrary queries nor caller-provided permission booleans are
accepted. The API retains credential admission and final public DTO validation.

Project and Directory managers share one current-authority implementation:
initial full Project decode, current actor status, refreshed Project decode,
then role and membership checks. These remain separate reads without new locks.
Missing Project still precedes inactive-account failure; refreshed disappearance
precedes invalid role. API `get_current_user_id` already resolves actor context,
so the extraction adds no second actor-context dependency.

Member lists decode every selected membership column, including both timestamps,
before one global Identity Briefs lookup. Selection remains `assigned_at DESC`;
empty sets avoid profiles, missing profiles are omitted, and invalid stored roles
still reach the existing public DTO failure. Display profiles do not authorize
the request. The borrowed binding uses the API's selected profile port; the
standalone process uses the existing explicit Workspace Identity Briefs settings.

Mention candidates read the captured Project owner, active super-administrators
ordered by database name collation, then joined membership accounts in three
separate phases. UUID deduplication keeps the first kind. The membership join has
no active-account filter: an inactive owner or administrator can reappear as a
member. A batched Identity-owned account capability and the membership selection
share one native statement snapshot, preserving the former JOIN. Workspace gains
no raw User-table permission.

The default binding borrows the caller transaction, respects autoflush and
`no_autoflush`, and neither commits nor rolls back. Dedicated DSN and HTTP
bindings own read-only transactions and cannot see another connection's pending
work. Explicit provider failures never select a local fallback. Profile failure
retains its specific public 503; stored-value decode errors retain plain 500.

Native0263 exposes only `directory_query_policy()` and
`directory_query_step(jsonb,text,jsonb)` to `workspace_directory_query_client`.
The role has no membership, raw-table, DML or temporary-object capability. The
existing runtime supports directory-only assembly, independent readiness,
control-bearer admission and optional loopback Compose deployment on port8177.

## Member-authority temporal correction

The pre-extraction authority reader decoded both membership timestamps even
though permission policy only consumed identity and role. Native0261 omitted
those fields. A targeted regression first reproduced two incorrect successes
for finite timestamps outside Python's datetime range; true infinity remained
valid.

Additive0262 preserves the existing wire and function signatures. A STABLE
wrapper calls the frozen implementation and checks the same selected row's two
timestamps in the same statement snapshot, before permission policy. Manager
paths that never read membership remain unchanged. Infinities and nulls retain
their prior meaning. Upgrade moves deployed direct EXECUTE ACLs and grant options
to the wrapper, keeps its owner and hides the implementation; downgrade restores
the prior public function and ACLs. The historical0261 artifact is not rewritten.

## Verification and limits

The frozen directory capture contains thirteen actual HTTP responses and two
autoflush probes. Portable Workspace source tests pass226 cases. The native
checks cover closed parameters, exact function-only credentials, all read facts,
inactive-account joins and real manager execution. A deployed-ACL migration test
performs downgrade/upgrade twice, retaining runtime/client access and grant
options while denying direct implementation access.

An extended regression also reproduces upgrade failure when an existing client
uses its grant option to authorize another role. Upgrade now copies every
grantee's effective EXECUTE/grant-option state before revoking the old function's
dependent privilege chains. The public wrapper retains access for both roles;
the private implementation has no non-owner EXECUTE entries. The strengthened
two-cycle migration regression passes after that correction.

All thirteen frozen HTTP observations match through borrowed, function-only DSN
and noneditable HTTP providers. The two caller autoflush probes deliberately use
the borrowed binding. Two installed-process cases verify optional-profile
isolation, profile/data grant loss, no fallback, stop and restart. With existing
member-profile and mention regressions, that run passes fifteen selected tests
without skips. Nine transport cases and exact public OpenAPI also pass.
Noneditable Workspace passes226 tests and matches all42 source files; its
Identity, Inference and shared-package manifests also match. Documentation builds.

The restricted owner image matches184 installed files and passes nonroot,
read-only, no-network and resource-limit checks. A real Directory container
performs public reads, sixteen requests at concurrency four and restart within
1 CPU/256 MiB; the API and database remain outside that budget. Docker changes
the ephemeral published port on restart, so the test refreshes the actual address
before readiness and readback. This corrects a failed test attempt without
changing runtime behavior or increasing its readiness deadline.

Ten restore cases cover the current schema and affected Identity/Workspace
readers. The configuration restore now also compares both Directory operations
through separate function-only Directory and Briefs credentials, verifies
read-only transactions and denies a subsequently inactive actor. The first run
passes three restore cases but skips seven lacking their explicit opt-in; all
seven pass in the follow-up. Initial cleanup refuses to proceed; a later owned
inventory confirms zero connections and the unchanged ownership markers, after
which the same destruction guard removes only that run's database, Redis,
buckets and container. The failed report and cleanup retry remain separate.

Full migration validation passes fresh263,173down/base/up, the89-step reversible
suffix, authentic historical forward conversion and pre-conversion restore.
All four derived databases and the dump are removed. The updated API image
matches186 installed package files and198 application/Native inputs, including
the grant-chain correction. It passes nonroot, read-only, no-network and resource
restrictions, exact OpenAPI, closed Directory schema and head0263 checks. Both
proof containers are removed. Previous Project query image evidence is not reused
as Directory qualification. These local ARM64 checks do not establish production
capacity or an SLO.

The final selected-check union has37 distinct passing cases, combining native,
temporal and ACL checks, current-provider replay, existing API regressions,
installed-process faults, container and restore checks, and the migration
validator. Earlier failed or skipped attempts are retained and have matching
passing follow-ups; repeated runs are not added to this count. Source/installed
package suites and transport/OpenAPI checks remain separate evidence.

This is a selected directory boundary. Project `/access`, class-usage reads,
class rename, guide-object I/O, Inference-owned named pipelines and other
cross-domain consumers remain separate work. Shared PostgreSQL and a trusted
coordinator remain constraints. Complete Workspace, other modules, another
language provider, production capacity and primary cutover are not qualified by
these checks. Go remains an independent experiment.

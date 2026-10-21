# 0140 — Workspace Directory Query Owner

- **Status:** Accepted; selected directory verification in progress
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

All thirteen frozen HTTP observations match through borrowed, function-only DSN
and noneditable HTTP providers. The two caller autoflush probes deliberately use
the borrowed binding. Two installed-process cases verify optional-profile
isolation, profile/data grant loss, no fallback, stop and restart. With existing
member-profile and mention regressions, that run passes fifteen selected tests
without skips. Nine transport cases and exact public OpenAPI also pass.
Noneditable Workspace passes226 tests and matches all42 source files; its
Identity, Inference and shared-package manifests also match. Documentation builds.

Restricted owner/API images and a directory backup/restore check remain pending
for this new artifact; the previous Project query image evidence is not reused
as Directory qualification.

This is a selected directory boundary. Project `/access`, class-usage reads,
class rename, guide-object I/O, Inference-owned named pipelines and other
cross-domain consumers remain separate work. Shared PostgreSQL and a trusted
coordinator remain constraints. Complete Workspace, other modules, another
language provider, production capacity and primary cutover are not qualified by
these checks. Go remains an independent experiment.

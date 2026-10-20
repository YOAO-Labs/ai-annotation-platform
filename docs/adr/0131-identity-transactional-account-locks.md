# 0131 — Identity transactional account lock facts

- **Status:** Proposed
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None

## Context

Workspace, Labeling, Inference, Learning and Identity workflows need current
account facts while holding the same account locks through a later domain write.
Moving the lock to an HTTP request or an independent Identity connection would
release it before the Workspace/Labeling/Inference transaction commits. Passing
User ORM rows across service boundaries also couples those consumers to the
Identity table shape.

Credential admission, post-admission actor context and transaction-time account
locks have different responsibilities. The previously qualified actor-context
slice uses the existing AUTH owner to check the admitted account, but it does
not provide a write-time lock or permission to mutate another domain.

## Decision

### Use a closed value contract for same-transaction locks

The portable Identity package adds `account_lock_contract.py` with protocol
`aap-identity-account-locks/v1`. `AccountLockRequest` contains `schema`, a list
of account UUIDs, one of `share|update|key_share`, and strict boolean `nowait`.
Input order and duplicate IDs are accepted; locks and replies use sorted unique
IDs. There is no artificial count cap and no token, credential, admitted role,
operation ID, audit context or arbitrary SQL input.

The closed `AccountLockReply` contains only current existing account facts:
`id`, raw `name`, raw `email`, raw `role` and `is_active`. Missing IDs are
omitted; inactive accounts and historical/unknown role strings are returned
without filtering. The reply cannot expose a User row, credential, password,
weekly target, preference or other lifecycle field. Strict/frozen decoding
rejects aliases, extra data, invalid types, malformed/deep/non-finite JSON,
duplicate or unsorted output IDs and IDs that were not requested. The native
schema artifact is exported from the canonical portable contract without
changing existing schema artifacts. The API data adapter maps these five facts
to frozen `IdentityActorFacts` with `weekly_target_default=None`.

The wire reply is exactly `schema/accounts`; it is checked against the sorted
requested ID set while allowing missing accounts. Invalid native/portable data
fails closed as unavailable without an ORM fallback. The account facts use
strict strings for `name/email/role` and a strict boolean for `is_active`.

### Keep the lock and following write in the caller transaction

Migration 0251 adds exactly three `VOLATILE SECURITY DEFINER` functions under
`aap_identity_v1`, with a safe fixed `search_path` and PUBLIC execution revoked:

| Function                                  | Lock mode   |
| ----------------------------------------- | ----------- |
| `lock_accounts_share(uuid[],boolean)`     | `SHARE`     |
| `lock_accounts_update(uuid[],boolean)`    | `UPDATE`    |
| `lock_accounts_key_share(uuid[],boolean)` | `KEY SHARE` |

They return the same closed account-facts envelope. Missing database rows are
not errors. `nowait=false` waits and then returns the current locked row version.
A NULL array, NULL ID entry or NULL `nowait` may raise `22023 invalid_request`;
an empty array succeeds without locking rows. These functions are owned by the
NOLOGIN Identity owner. None writes account state, audit, receipts or additional
tables.

`IdentityAccountLockData` executes these functions on the caller's existing
connection and transaction. It never commits, rolls back, retries, wraps a
savepoint or catches SQLSTATE failures. Lock errors such as `55P03`, `40001` and
`40P01` propagate and leave a raw SQL transaction aborted. Each wrapper keeps
its own established exception mapping and rollback behavior. A malformed or
binding-invalid native reply becomes unavailable (503) without falling back to
the API User ORM.

Lock order remains account UUIDs in sorted order, then Project, membership and
the caller's resource. Existing `SHARE`, `UPDATE`, `KEY SHARE` and NOWAIT/waiting
choices are preserved. A batch lock must not be replaced by a per-account loop.
The functions are a native SQL capability, not an HTTP lock endpoint or a
dedicated connection service.

### Preserve caller policy, status mapping and write-time ORM ownership

`project_write_guard.lock_actor_account_facts` uses `SHARE` and returns facts;
the existing `lock_actor_account` remains a `None`-returning compatibility
wrapper. A single-account conflict keeps `account_busy`; the combined
account/project guard maps either conflict to `project_busy`; project access
continues to distinguish account, project and membership contention. Existing
401/403/404/409 statuses, details and Bearer headers remain caller-owned.

`UserLifecycleService.lock_accounts` uses `UPDATE` or `KEY SHARE` according to
the existing path and returns facts. Its wrapper deliberately rolls back on any
`DBAPIError`, preserving its current busy translation to HTTP 409 detail
`账号正在更新，请刷新后重试`. Other wrappers allow PostgreSQL lock failures to
propagate without rollback inside the data port. `IdentityCommandService`
locking methods also return facts; mutable bootstrap and import User writers
remain unchanged. Existing `lock_active_actor`, `audit_actor` and
`current_principal` functions are not replacements for this contract and remain
unchanged.

`project_access.lock_current_project_rows/access` place `IdentityActorFacts` in
the actor slot but keep Workspace Project/member ORM rows where needed. Identity
command locking methods return the facts value; bootstrap and import-account
writers retain their mutable User ORM path. Template locked-current-actor paths
use facts, while unlocked reads stay as before. No actor fact is itself a
project capability or a substitute for the caller's locked current-scope checks.

### Grant only the typed function capabilities

Workspace owner receives all three functions. Labeling and Learning owners
receive `SHARE`; Inference receives `UPDATE`; the existing workflow LOGIN gets
all three only for same-connection coordination. Grant only the schema usage and
function execution each owner needs, never raw User table or column access.
`application_role` receives these functions only from the LAN production-grants
source; that source is not applied to production. AUTH and credential FENCE
LOGIN capabilities do not change. The existing `read_actor_facts` capability
may be granted to workflow/application roles for the worker's initial status
read; no new LOGIN, DSN or environment selector is introduced.

The local worktree compatibility runtime also needs these three functions and
the existing `read_actor_facts`/`read_actor_context` operations. Its explicit
allowlist in `scripts/worktree_resources.py` is synchronized during runtime
grant refresh. Qualification applies it only to owned disposable state; the
runtime's existing broad table access is not an independent-module boundary.

### Refresh actor facts for every eligible cross-frame write

The frame worker uses the stable job `user_id` and existing
`read_actor_status` path for its initial missing/inactive check, preserving the
existing `ValueError` text. Each eligible frame then obtains fresh account
facts inside the current transaction before rechecking account activity and
project/member/task authority. It must not cache an actor snapshot across frames
or call `db.refresh(User)` as a substitute. Per-frame commit/rollback,
cancellation, source-stale fast paths, task locks, audit and result/error counts
stay in place. Account contention remains the existing `permission_changed`
failure; this slice adds no retry policy. A mid-job deleted account keeps
generic `propagate_failed`; an inactive account remains `permission_changed`.
Authority changes retain their existing phase-specific mappings, including
`source_task_locked` and `target_task_locked`; they are not normalized into
one failure reason.

Unlocked Workspace reads and bootstrap account writers remain future work.
This slice does not broaden scope to those paths.

## Consequences

Positive:

- Identity facts can be read without exposing User ORM rows while the row lock
  remains held through the caller's business commit.
- Existing lock modes, order and API error behavior remain caller-owned.
- Owner credentials gain only typed function execution; there is no new service
  connection that could split a lock from the protected write.

Negative:

- Every caller must keep the same database connection and transaction alive
  across the Identity lock and its Workspace/Labeling/Inference write.
- Raw PostgreSQL lock failures abort that transaction; wrappers that explicitly
  roll back retain their own distinct behavior.
- Unlocked Workspace queries and mutable bootstrap/import User writers remain
  outside this boundary.

## Alternatives Considered

**HTTP lock RPC or independent Identity DSN.** This releases the account lock
before the caller's business write commits and cannot preserve the required
cross-owner serialization.

**Grant the caller raw User table access.** This exposes unrelated Identity
columns and bypasses the fixed facts, lock modes, and narrow SQL owner boundary.

**Reuse admission or audit helpers as locks.** Those contracts filter or reject
different account states, omit required current facts, catch SQLSTATEs, or
reauthenticate. They cannot preserve these callers' existing policy semantics.

## Notes

- Portable contract: `apps/identity/src/aap_identity/account_lock_contract.py`.
- Caller data port: `apps/api/app/services/identity_account_lock_data.py`.
- Native operations: migration `0251`, following `0250`.
- Root's pre-change baseline passed one owned-state case across 15 scenarios;
  captured outputs are in `identity-write-fences-legacy.json` and
  `identity-write-fences-legacy-samples.json`. It is compatibility evidence,
  not qualification of the new functions. Do not rerun it against the new
  implementation.
- The previous actor-context slice's 114 selected cases remain closed and
  separate. This slice's latest union passed 231 distinct selected cases without
  effective skips: native locks and grants, exact baseline parity, transaction
  errors, Node consumption, both transfer/role-change orders, eight restores,
  and a fresh Celery process with three real dispatches. Failed harness attempts
  are retained and replaced by the latest pass rather than counted again.
- Source and noneditable installed suites each passed 595 checks. All 119
  package files match the current source, installed environment and fresh owner
  and API images; the API proof also checks 150 application/native files.
  OpenAPI remains byte-identical, codegen and the API catalog match, and 316
  architecture checks passed with 132 existing inapplicable rules deferred.
  Image timings describe build/offline verification, not startup latency or
  production capacity. The full ten-module/seven-gate objective remains active;
  no production or primary cutover is claimed.

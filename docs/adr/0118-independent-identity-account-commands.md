# 0118 — 自助账户命令与在线状态所有权

- Status: Accepted; local self-account qualification completed
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: None
- Extends: ADR-0115, ADR-0116 and ADR-0117

## Context

Independent admission, API keys and sessions still left self-account reads and
writes on API User ORM objects. Extracting preference schemas alone did not move
their locked merge or compare-and-swap transaction. Presence also had two writers:
the heartbeat HTTP handler and a scheduled bulk update. A language replacement
would otherwise need to import the API or reproduce an unowned transaction.

Preferences must preserve sparse deep merge, atomic map replacement, revision CAS,
workspace downgrade protection and opaque future/corrupt stored values. A schema
validator followed by unconditional JSON replacement does not implement that
contract. Self password change verifies the old password and clears the admin
reset marker, while retaining the existing JWT generation and API keys.

## Decision

Add seven self-account operations to the existing Identity package and private
process using `aap-identity-accounts/v1`: account read, name update, preference read
and patch, heartbeat, password change and avatar-reference update. Public paths,
response models and existing API-key self-service access remain. The API consumes
Principal and value replies for these operations without loading User rows.
Registration, recovery/email, group/admin workflows and multi-owner lifecycle
continue as explicitly unfinished boundaries.

Migration 0238 supplies native prepare/apply capabilities and one account policy
function. The account LOGIN has exactly these three capabilities. Prepare takes
the User lock before credential locks and verifies current active/role state,
generation/JTI/expiry or the verified API-key hash. Preference patches wait on the
latest row; other writes retain nonblocking account contention. Reads can wait
for a shared lock. Portable merge or password verification/hash runs between
prepare and apply on the same owner connection and transaction. Apply locks and
checks again, including the operation's base field, so direct apply cannot skip
current authentication or overwrite an unrelated newer value.

Identity uses the existing Audit-owned context validator and a typed account-event
append capability, never direct Audit table access. Changed name/avatar and
successful password events commit with the mutation and receipt. Preferences and
heartbeat keep their existing generic HTTP audit behavior. Replies and strict JSON
serialization are validated before commit; nonfinite values cannot silently become
null through a serializer. The API strips password validation input/context and
hides SQL bind parameters in debug output.

Successful name, preference, password and avatar commands retain an operation
receipt with a purpose-separated request HMAC and signing-epoch identifier. The
HMAC input is sorted-key compact `ensure_ascii` JSON `{operation,payload}`, with
master `HMAC-SHA256(secret UTF-8, aap-identity-accounts/v1 ASCII)` and epoch
`SHA256(master).hex`. Receipts contain outcomes, not identity passwords, hashes or
bearer tokens. Replay requires current authentication and returns frozen data
with a freshly fenced principal. It does not recheck the obsolete old password,
repeat an edit/audit, rewind current state or revive a credential. Frozen profile
metadata can differ from the current principal's name/role/email. Public commands
do not gain automatic retries or a new idempotency header.

Heartbeat uses database time and creates no receipt per poll. A separate presence
LOGIN can only execute the bulk stale-status update. Celery retains cadence,
dynamic threshold resolution and one cutoff, then calls this owner locally or
through the same private runtime. Readiness checks this function's permission
without executing the update. The original NULL/strict-cutoff predicate is kept.

Avatar normalization and S3 remain in the existing byte provider. An owner read
rejects stale credentials before staging; the final pointer command rechecks them.
Remote SQL transactions never span image processing or S3 IO. A definite rejection
cleans the new staged object. An unknown commit retains it for reconciliation,
because deleting it could invalidate an already committed pointer. Old uploaded
bytes are removed only after a confirmed pointer commit; no-op has no cleanup
target. Cleanup failure leaves an orphan and does not reverse the committed edit.

## Consequences

The existing independent artifact can own this complete self-account command set
without API, ORM, Celery, S3 or image dependencies. Optional account/presence DSNs
have separate function-only LOGINS, no table/TEMP/foreign-module permissions and no
fallback. Embedded SQL assembly uses the same capabilities and synchronizes only
User objects a legacy caller already holds; it does not query them for these routes.

Valid preference payloads retain their previous accepted size/cardinality set.
Account transport does not reuse the session body's 64 KiB or admission reply's
16 MiB ceiling. Deadlines, concurrency and deployed process resources remain
operational bounds; capacity requires measured qualification, not inferred safety
from a small request. Distributed read/write model schemas do not replace the
stateful policy when another language implements the provider.

Retained account receipts prevent a destructive downgrade. Backup/restore must
preserve the ledger and database/schema/function ACLs as well as credentials.
The full Identity boundary, remaining API signers/privileges, other module owners,
an alternate-language provider and sustained capacity remain open. No production
or primary Identity cutover is implied by this decision.

## Alternatives Considered

- Read JSON in one transaction and replace it in another: loses concurrent sparse
  changes or introduces new optimistic conflicts instead of the existing merge.
- Keep password/preference policy at the API and expose setters only: leaves the
  module's business decisions dependent on Python API code.
- Hold remote SQL locks during avatar IO: couples service failure and storage
  latency to database lock lifetime without making the object store transactional.

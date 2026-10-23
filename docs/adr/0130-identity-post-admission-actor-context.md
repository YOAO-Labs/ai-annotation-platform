# 0130 — Identity post-admission actor context

- **Status:** Accepted; shared actor context locally qualified
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None

## Context

After JWT or API-key admission, `deps.get_current_user` currently hydrates the
admitted account from the API's User ORM to check that the account still exists,
is active, and has the exact admitted platform role, then to provide current
profile and scheduling facts. Identity already owns credential admission. The
post-admission context read should use that existing AUTH boundary without
repeating credential authentication or changing the separate account-write
transaction rules.

## Decision

Use one closed, read-only Identity actor-context operation after successful
admission. The private endpoint is `POST /v1/identity/actor-context`, protected
by the existing control bearer. Its `aap-identity-actor/v1` request contains
only `schema`, `actor_id` and the exact admitted `admitted_role`; credentials,
scopes, audit context and caller-supplied profile values are not sent.

The reply envelope is either `{schema, ok: true, data}` or
`{schema, ok: false, error: {code}}`, where error codes are exactly
`invalid_request` and `credential_invalid`. `data` contains exactly the current
account's `id`, `name`, `email`, `role`, `is_active: true` and nullable
int32 `weekly_target_default`. Names and emails remain raw stored values. The
contract carries no preferences, avatar, password or credential material.
Strict/frozen models reject extra fields and aliases. Before accepting success,
the API binds the returned ID and role to the admitted request and requires the
active literal. A mismatch or malformed owner response is unavailable, never a
partial result.

The private route returns HTTP 400 for `invalid_request`, including malformed
or excessively nested JSON. `credential_invalid` uses a typed HTTP 200 failure
envelope; the API maps it to its existing HTTP 401 semantics.

`identity_owner` adds the read-only
`aap_identity_v1.read_actor_context(uuid,text)` function in migration 0250.
It is `STABLE SECURITY DEFINER` with a safe search path and no locks, writes,
audit, receipt or state; PUBLIC execution is revoked. `identity_auth_client`
gains this fifth function in its existing execute-only allowlist; no raw table
or DDL privileges are added. This does not introduce a new role, DSN, setting,
dependency, service or public REST/SDK field.

The API uses the existing `IdentityGateway` binding: remote Identity first,
explicit `IDENTITY_AUTH_DATABASE_URL(_FILE)` next, and the caller transaction
when neither is selected. Embedded SQL honors `autoflush` and
`no_autoflush`, and never commits or rolls back. Dedicated reads own and close a
bounded transaction. The result maps to a local frozen `IdentityActorFacts`,
whose `weekly_target_default` defaults to null when no value is supplied. There
is no new commit from the context read, and the existing API-key admission
commit stays unchanged. A selected remote or DSN failure never falls back to
the API User ORM. The route reuses existing Identity timeouts, concurrency
slots and bounded-body handling.

The read remains after admission. Missing accounts, inactive accounts or an
exact role change map to the existing HTTP 401 and Bearer challenge and clear
`identity_principal`, `api_key_scopes` and `api_key_id`. Owner unavailability
or malformed/binding-invalid data maps to the existing 503. Credential
authentication, expiry/generation/revocation checks, API-key last-used writes,
and admitted scope state are not repeated or refreshed. Name, email and weekly
target are fresh context, not admitted credential snapshots.

This boundary only replaces post-admission hydration. Write-time account locks,
target User queries, and ORM mutation synchronization remain with their current
consumers and transactions. The actor context is not a project capability or a
write-time authorization ticket; project/target access still uses its existing
current-row checks.

## Consequences

Positive:

- Identity owns the current profile facts consumed by the authenticated request
  dependency, using the existing AUTH identity and connection policy.
- Current account/role checks remain fail-closed without exposing User ORM data
  across the Identity boundary.

Negative:

- Each authenticated API context now performs a bounded read after admission;
  embedded and remote deployments have different transaction snapshots.
- Qualification must cover the API dependency's 401/503 compatibility, selected
  binding no-fallback behavior, AUTH grants, and deployed artifact hashes.
- This does not remove User ORM access required for writes, account locking or
  target synchronization, and does not complete Identity or the ten-module goal.

## Alternatives Considered

**Keep post-admission User ORM hydration in the API.** This preserves the current
local read but leaves the authenticated actor's profile check on a second data
owner path. It does not establish the existing AUTH service as the source for
the complete request context.

**Add a second actor-context DSN or LOGIN.** This duplicates connection and
privilege configuration for a read already owned by AUTH. The fifth narrowly
scoped function on the existing AUTH LOGIN is sufficient.

## Notes

- Portable contract: `apps/identity/src/aap_identity/actor_contract.py`.
- Runtime reader: `apps/identity/src/aap_identity/actor_reader.py`.
- API binding: `apps/api/app/services/identity_gateway.py` and
  `apps/api/app/deps.py`.
- Native read: migration `0250` and the `identity-auth-grants.sql` allowlist.
- The latest affected-case union passed 114 selected cases: 92 database/process
  and 22 pure/static, with no skips. The union includes current-account fences,
  caller transaction visibility, actual fifth-capability revocation/readiness,
  both language-peer directions, remote/dedicated API bindings, eight existing
  Identity restore cases, and malformed JSON against the final owner container.
  Targeted retries replace earlier fixture failures rather than adding cases.
- Source and noneditable installed packages each passed 579 checks; all 117
  installed files match current source hashes. The fresh restricted API image
  verified the same 117 package files and 141 API/native files, unchanged public
  OpenAPI and migration head 0250. SDK auth/dashboard/projects passed 42 checks,
  Node self-checks passed 27, and 316 architecture checks passed; 132 existing
  inapplicable architecture rules remain explicitly deferred.
- These are local slice results. No production or primary Identity cutover,
  full Identity qualification or ten-module completion is claimed.

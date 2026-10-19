# 0115 — Identity 准入与持久会话撤权

- Status: Accepted; admission implementation, local qualification in progress
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: Redis as JWT revocation authority; no prior ADR is superseded in full

## Context

REST and WebSocket credential resolution duplicated JWT/API-key validation in
the API. Some paths ignored Redis revocation failures. Logout, password-token
reset and account retirement updated Redis before their SQL transaction could
commit, so rollback could leave credentials revoked independently of account
state. Expiring a blacklist entry at access-token expiry also lost protection
during the seven-day refresh grace.

The complete Identity module includes account/profile/credential commands and
multi-owner invitation/lifecycle coordinators. Moving an entire route file to
another process would split those transactions. Admission is a concrete runtime
boundary, not a claim that all Identity operations are extracted.

## Decision

Use Python for the admission implementation, reusing the existing bcrypt and
JOSE stack. `apps/identity` owns one portable verifier, a closed
`aap-identity/v1` principal and a private HTTP runtime. REST and every socket
handshake/revalidation call that same verifier through local or explicit remote
assembly. A configured remote failure never falls back. Existing per-route
JWT/API-key acceptance, project authority and scope checks remain distinct.

The runtime uses only PostgreSQL and the signing/control secrets. Its LOGIN has
four native capabilities: policy, JWT principal read, key-candidate pagination
and verified-key confirmation. Confirmation rechecks the exact hash under
User-then-key locks before recording usage. A key candidate is not a principal.
Current JWT role, active state, generation and JTI status come from one command
snapshot, not a cached token role. An optional local caller-session adapter
preserves the existing transaction assembly; dedicated DSN and HTTP modes do
not fall back to it.

Migration 0235 adds `users.credential_generation` and durable JTI metadata.
Generation, JTI revocation and API-key/password-reset-token retirement run in
the caller's SQL transaction. The command, final-fence and expired-row operator
LOGINS have separate five/one/one-function surfaces. No admission LOGIN has
table access, role membership or CREATE/TEMP authority. JTI retention extends
through access expiry plus the complete refresh grace; rounding cannot shorten
that interval. No raw bearer is stored.

An admission result observes credentials when resolved. Business commands that
must serialize revocation with their final write must explicitly invoke
`lock_current_principal` on their committing transaction. That function holds
the current User and credential fences. Existing actor-only owner operations
do not acquire that guarantee merely because authentication moved remotely.

The new codec requires issuer `aap-identity/v1` and derives the HS256 signing
key using HMAC-SHA256 of the existing secret with that issuer context. Old JWTs
must log in again. Existing SQL-backed API keys remain valid. This deliberately
avoids reconstructing incomplete Redis revocation history. Stop all old REST,
socket, refresh and issuer processes before cutover; mixed rolling authentication
and rollback to an old accepting validator are unsupported. Downgrade refuses
while any User or retained JTI exists. Recovery preserves state and rolls
forward with the compatible validator.

The wire carries UUIDs, timezone-aware timestamps and exact signed 64-bit
generation integers. Consumers must preserve integer precision; a floating-point
JSON decode that rounds generation is not conformant. Response framing is
bounded and unknown/partial/mismatched native replies fail closed.

## Consequences

The admission process can be installed and deployed independently without API,
SQLAlchemy, Redis, Celery, S3 or ML dependencies. Revocation becomes atomic with
the existing lifecycle coordinators and survives service restart. Authenticated
audit attribution uses the verified principal. Profile hydration rejects a role
change instead of restoring a stale role over newer data.

JWT cutoff requires a new login. Remaining login/registration, key management,
password/email workflows, profile hydration and cross-owner coordinators still
live in the API. Issuance still shares the portable codec and signing secret;
the runtime is not yet the sole credential command owner. The API keeps its
broad transitional database role, and universal final-write credential fencing
remains unqualified. These are unfinished parts of the full module goal.

Native and installed-process/image checks are qualified in owned local resources.
Installed file hashes match source; the read-only nonroot container has no API,
ORM, Redis, queue, object-store or ML dependencies. A real API consumes it over
HTTP for REST/API-key operations and socket revalidation, and refuses admission
when the service stops. Restart preserves revocation. Backup/restore retains
generation, JTI, keys and restricted function privileges; populated downgrade
refuses without changing the revision. A standalone Node client verifies exact
principal bindings and preserves signed 64-bit generations. This is client
interoperability, not a second-language provider implementation. The bounded
local concurrency smoke does not qualify production capacity.

Full command ownership, selected provider replacement and daily cutover remain
separate gates. Current implementation is not promoted to the daily development
environment until its required local gates pass. Production and the Go
experiment are outside this cutover.

## Alternatives Considered

Importing only visible Redis keys could revive credentials whose earlier
revocations had already disappeared. Keeping Redis as a second mandatory
authority would retain the coupling and split transactional writes. A controlled
issuer cutoff gives a verifiable one-way boundary for local development without
claiming seamless session preservation.

# 0117 — 核心会话命令与原子审计

- Status: Accepted; local core-session qualification completed
- Date: 2026-10-20
- Deciders: Maintainers
- Extends: ADR-0115 and ADR-0116

## Context

Independent admission and API-key commands do not own login or JWT renewal. The
API still verifies account passwords, issues session claims, changes presence and
combines generation/JTI changes with explicit Audit rows. Moving only signing
would leave those decisions and their transaction dependent on the API ORM.

Login has password-first gates, a progressive CAPTCHA and a Redis failure counter.
Refresh accepts an unexpired JWT or one within the seven-day grace period, and
does not consume the old token. Logout accepts JWT only; logout-all accepts both
JWT and API keys, advances JWT generation and leaves API keys usable. These
policies must survive extraction.

## Decision

Move login, refresh, logout and logout-all into the existing Identity package and
private runtime using `aap-identity-sessions/v1`. Keep generic public HTTP rate
limits and Turnstile verification at the API edge. The provider owns failed-login
counting and the threshold decision, rechecking the same Redis namespace before
password verification and accepting only a trusted edge's CAPTCHA result. Redis
errors retain the existing fail-open behavior. Provider and edge must use the
same threshold, counter window and Redis instance.

The API routes carry values and consume Principal, without hydrating User or
writing session state. Registration, invitation, password recovery and other
Identity commands remain separate unfinished work. Existing registration and
invitation signers still require the same signing secret and policy; this tranche
does not remove signing authority from the API as a whole.

Migration 0237 introduces a session receipt table and three public native
capabilities: policy, login candidate and command execution. The separate session
LOGIN has exactly those functions. Candidate hashes stay inside Identity; bcrypt
runs outside final locks. Command execution takes the User write lock first,
checks the current hash and email, then current active/verified state. Token
commands validate current generation/JTI or API-key hash under the corresponding
credential lock. Refresh has its own grace-aware fence; ordinary admission stays
strict. Logout obtains a nonblocking exclusive JTI lock before revocation.

Audit receives a dedicated owner capability for the seven session outcomes,
including anonymous invalid credentials and valid-password inactive/unverified
accounts. Identity passes bounded current facts; it does not write Audit's table
or require Audit to read Identity tables. User state, generation/JTI, successful
operation receipt and business Audit commit or roll back together. A validated
failed-login response may still commit its Audit; an invalid reply cannot confirm
or commit the command. Redis is a separate nontransactional security counter.

Successful operation receipts contain frozen JWT claims, never passwords or raw
tokens. JWT JTI is the explicit operation UUID and expiry comes from the database
clock and configured lifetime. A fixed-claims signer reproduces the same token
after response loss or provider restart. The receipt binds actor, operation,
signing epoch and issue policy; login additionally binds a purpose-separated HMAC
of the exact email/password request. No password or stored password hash enters
the receipt. A changed request, policy or signing epoch conflicts.

Frozen JWT serialization uses compact UTF-8 JSON with header fields `alg`, `typ`
and payload order `sub`, `role`, `exp`, `jti`, `gen`, `iss`; UUIDs are canonical
lowercase and numbers are exact decimal integers. The HMAC signing key is the
UTF-8 lowercase hex result of HMAC-SHA256(existing secret, issuer), preserving the
existing codec. Base64URL has no padding. A public test-only known-answer fixture
freezes signing, session derivation and Unicode request fingerprints using Node
standard crypto, checked by the Python package and the standalone Node peer.

Replay always requires current authentication, including current password/gates
for login. A stale or revoked JWT cannot retrieve a receipt. The same owner can
authenticate with another valid credential and replay the frozen outcome; doing
so never logs out the replacement credential or advances generation again. A
historical issued token may already be expired or revoked, and is not refreshed
or revived by receipt replay. Public POST behavior and response shapes remain;
no automatic retry or new public idempotency header is introduced here.

## Consequences

The same command implementation supports embedded assembly and an independently
installed process. Explicit remote or dedicated-DSN failure never falls back to
an API writer. The independent runtime adds a bounded Redis client solely for
`login_failed:*`, with a separate ACL identity; it still needs no API, ORM, Celery,
object storage or ML package.

Provider lifetime and email-verification settings stay operator configuration,
with immutable values in each receipt. Native policy owns issuer, algorithm and
refresh grace. A second database-global policy would create another configuration
source, so policy/secret parity across remaining issuers is a cutover gate.
Populated receipts block downgrade; recovery retains their claims and privileges.

Complete Identity deployment, remaining issuers/account flows, all API database
privileges, a chosen alternate-language provider and sustained capacity remain
part of the full module objective.

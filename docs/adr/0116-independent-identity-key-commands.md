# 0116 — API key 命令与可重放签发

- Status: Accepted; local command qualification completed
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: API-owned key generation and ORM management; extends ADR-0115

## Context

Credential admission already has an independent Identity runtime, but the API
still generates keys and writes their hashes, scopes, expiry and revocation.
Moving only admission leaves a second credential writer and an ORM-dependent
management port. Rotation and scope changes also need to serialize with current
account and credential state, including a key rotated after password verification.

A new process boundary makes an unknown commit possible after a lost response.
Create and rotate return secret material, so retaining only an ordinary metadata
receipt cannot reproduce the successful response. Keeping bearer plaintext in a
receipt would change the existing storage policy.

## Decision

Move list, create, update, rotate and revoke to the existing Identity package and
runtime. Use the closed `aap-identity-keys/v1` protocol and a separate key-command
database binding. The public HTTP adapter forwards values and consumes a verified
principal; it does not hydrate User or mutate key ORM rows. Public response
models, owner isolation, allowed credential kinds and PATCH omission/null rules
remain. The HTTP adapter retains admission before the command for attribution of
failed requests; the command independently verifies credentials and fences its
actual commit. Remote use therefore has both admission and command calls.

Migration 0236 adds an Identity-owned operation receipt table and two native
capabilities: policy and command execution. The dedicated LOGIN has exactly those
functions and no table, DDL, membership or admission-function privileges. The
provider separately holds its existing admission LOGIN. Native functions lock the
current account and credential, compare the verified API-key hash, and lock the
target key without waiting. Hash work happens outside final business locks.

Every mutation has an operation UUID. Same-owner, same-operation, same-payload
replay returns frozen metadata after current authentication and fencing; changed
input conflicts. Key changes and their receipt commit together. Receipts contain
metadata and the derivation identifier, never bearer plaintext or credential
hashes. Their User foreign key stays inside Identity and cascades on account
deletion. Revocation is soft; ordinary key changes do not delete receipts. List
reads current state and does not create a receipt. Frozen replay does not restore
an older key state or reactivate a revoked credential.

Derive a purpose-specific master with HMAC-SHA256 of the existing Identity secret
and `aap-identity-api-keys/v1`. HMAC the canonical actor/operation UUID context,
take 24 bytes and encode URL-safe Base64 under the existing `ak_` prefix. Store
only the normal bcrypt hash in the key row. The master SHA-256 identifies the
derivation in the receipt. This reproduces the same returned secret across a
provider restart without retaining plaintext. The frozen algorithm and context
are language-neutral and must be preserved by a replacement provider.

Existing key hashes keep working after a signing-secret rotation. Replaying an
older issue operation under a different derivation identifier conflicts; it must
not return newly derived material for the old key. Operators must account for
unacknowledged issuance when rotating that secret. No new independent secret or
secret-retention store is introduced by this tranche.

Replay always requires a currently valid credential. A key that rotates itself
cannot authenticate its replay with the old plaintext; the same active owner
must authenticate again using another credential. The public HTTP interface
does not gain an idempotency header or automatic POST retries. Private callers
retain the operation UUID when they need explicit reconciliation.

## Consequences

The independent runtime becomes the unique implementation of API-key commands.
The API can forward them without identity-table privileges. Native reply and
scope validation happens before a dedicated transaction commits, while embedded
assembly commits only after validation. Missing command configuration, unavailable
functions or an explicitly selected remote service fail closed.

The shared PostgreSQL functions remain the transactional data boundary. This
does not extract login, registration, account/profile commands, invitation and
membership coordination, or all other Identity operations. Generic audit
delivery is still its existing separate boundary. The main API keeps its wider
transitional role until those consumers move. Production capacity and a daily
runtime cutover require their own evidence.

## Alternatives Considered

Retaining an ORM writer would preserve the language coupling. Retrying with a
fresh random secret could create an extra key or rotate away a successful key.
Plaintext receipts or encrypted bearer retention require a new secret lifetime
and recovery policy. A deterministic, purpose-separated derivation with explicit
rotation and current-authority rules keeps the existing hash-only key storage.

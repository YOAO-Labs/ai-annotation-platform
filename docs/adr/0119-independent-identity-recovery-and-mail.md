# 0119 — Identity 注册恢复与凭据邮件独立所有权

- Status: Accepted; local implementation is in progress and runtime qualification is pending
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: None
- Extends: ADR-0115 and ADR-0118

## Context

Self-service account commands moved profile and password-change decisions behind
Identity, but open registration, password reset and email verification still
depended on API User/token ORM paths. Moving only the HTTP calls to another
process would fail for registration: a remote owner could not see an uncommitted
User and could not atomically accept its first verification email. Synchronous
SMTP also couples public request latency and account recovery to the mail server.

Existing verification and reset links must remain usable during migration.
Recovery links are credentials, so a provider boundary must never return their
raw token or log URLs. A replacement mail sender must not receive API auth, JWT
signing or cross-module table privileges. This slice does not finish group/admin
or other-user lifecycle ownership and does not itself authorize an environment
cutover.

## Decision

### Public operations and transactions

Keep public routes, CAPTCHA, rate limits and response translation in the API.
Route their business decisions to the existing Identity package using the closed
`aap-identity-recovery/v1` protocol at
`POST /v1/identity/recovery/execute`. It has exactly five operations:

The request is exactly `{schema,operation,operation_id,audit_context,payload}`.
Payload fields and retained public behavior are:

| Operation                    | Payload                                  | Behavior retained at the public boundary                                                                                                                                                                                                                                                                                                     |
| ---------------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `register_open`              | `{email,name,password,captcha_verified}` | Preserve registration disabled 403, CAPTCHA 400, duplicate 409 and success 201. A verification-required registration commits User, challenge, mail intent and `USER_REGISTER` Audit atomically and returns no JWT. Without verification, create a verified `viewer`, no project group, and return the existing JWT shape from frozen claims. |
| `request_password_reset`     | `{email,captcha_verified}`               | Preserve CAPTCHA, three requests per minute and generic 202. The owner accepts before the response and returns the same public result for unknown or inactive email. Save a no-target receipt so replay cannot retarget a later account.                                                                                                     |
| `resend_email_verification`  | `{email}`                                | Preserve generic 202 and three-per-minute policy. Existing inactive, unverified accounts remain eligible.                                                                                                                                                                                                                                    |
| `consume_password_reset`     | `{token,new_password}`                   | Preserve existing password validation and public 400/200 messages. Consume one valid challenge, set the password hash, clear the admin-reset marker, advance JWT generation and append Audit in the same transaction; API keys remain valid. An inactive account's fresh reset challenge is consumed but reports `completed: false`.         |
| `consume_email_verification` | `{token}`                                | Consume a valid challenge and verify even an inactive account without reactivating it, changing generation or revoking keys.                                                                                                                                                                                                                 |

Success replies are exactly `{schema,ok:true,operation,operation_id,data}` with
`data:{account,claims,access_token,email_verification_required,completed}`; each
operation fills only its documented fields. Failure replies contain only the
closed `{code,status_code,detail}` values. They preserve registration-disabled
403, email-exists 409, CAPTCHA 400, policy-validation 422, operation-conflict 409,
invalid-request 400 and unavailable 503. No reply includes a challenge token or
SMTP diagnostic.

Normalize email exactly as the existing API does. Keep existing name rules and
password policy. Both challenge purposes expire when database time is greater
than or equal to `expires_at`. Invalid tokens and validation errors never echo
raw input or request context. There is no new public idempotency header; the
private request UUID and operation receipt handle recovery replay.

### Native storage and capability ownership

Migration 0239 renames existing token columns to `token_hash` and hashes every
stored token in place using SHA-256 of UTF-8. The `issued_email`, expiry and used
metadata preserve scope and current valid links. Consumption checks the frozen
issued address against the account's current email. A populated receipt, outbox
or challenge table blocks downgrade.

Identity generates random challenge tokens in-process and sends their hash to
native apply. The public API/RPC result contains neither raw challenge tokens
nor access tokens from receipts. New receipt rows store a purpose-separated
request HMAC and signing epoch, plus frozen result data; they have no live-User
foreign key, so deleting and recreating an address cannot retarget replay.
Changed payload or HMAC-key epoch conflicts. Registration replay that originally
issued a JWT verifies the current password, active account and same email before
returning frozen claims; it never refreshes expiry or generation.

New links use `secrets.token_hex(32)`. The Identity manager hashes passwords
with its existing bcrypt policy and hashes challenge tokens before native apply;
raw password and challenge token values do not cross the PostgreSQL function
boundary. The private HTTP request can carry these inputs to an independent
Identity process, but replies and logs do not echo them.

The receipt master key is `HMAC-SHA256(secret UTF-8, ASCII
"aap-identity-recovery/v1")`; its epoch is `SHA256(master).hex`. Fingerprints use
HMAC-SHA256 over sorted-key compact `ensure_ascii` JSON `{operation,payload}`.
The deterministic policy defaults the reset challenge TTL to 3600 seconds and
email-verification TTL to 86400 seconds.

The native mail outbox is written in the same transaction as the challenge and
business Audit. It stores the token hash, Fernet ciphertext, encryption key ID,
issued recipient and frontend URL; it does not store a plaintext challenge token.
Existing token hashes remain valid after mail-key rotation, but pending encrypted
messages require their original key to be delivered.

Three pre-created, non-inheriting LOGIN roles have separate function-only
capabilities:

- `identity_recovery_client` can call exactly `recovery_policy()`,
  `prepare_recovery_command(jsonb)` and `apply_recovery_command(jsonb)`.
- `identity_mail_client` can call exactly `mail_policy()`, `claim_mail(jsonb)`,
  `renew_mail(jsonb)` and `finish_mail(jsonb)`.
- `identity_mail_settings_client` can call only
  `aap_settings_v1.mail_delivery_policy(jsonb)`.

The Identity code owner can call only
`aap_settings_v1.identity_recovery_policy(jsonb)` to read the
`allow_open_registration` and `frontend_base_url` settings; the function holds
a Settings SHARE lock to fence registration policy. The MAIL_SETTINGS function
returns exactly `smtp_host`, `smtp_port`, `smtp_user`, `smtp_password` and
`smtp_from` in one coherent snapshot with null-inherit environment fallback
semantics. The MAIL_SETTINGS role does not read Settings tables directly.
Credential mail ignores notification preferences. None of the three LOGINs has
direct User, token, receipt, outbox or Settings table rights.

Embedded mode may use the monolithic API's existing database role after its
Identity function allowlist is installed from
`infra/docker/lan-production-grants.sql`. This adds only the listed callable
capabilities; it does not make the full API database role least-privileged. The
grant-source change has not been applied to production.

Prepare and apply serialize the same operation UUID, then lock User, challenge
and outbox in that order. Recovery waits under its transaction deadline to retain
the existing reset-lock behavior. Password hashing happens between prepare and
apply on the same owner connection; apply rechecks policy, user/challenge scope
and receipt. No SMTP call occurs while a database lock or transaction is held.

### Independent delivery process

Run `aap_identity_runtime.mail_worker` from the same Python Identity image as the
other private processes. It claims one durable intent and commits the lease
before network IO. Claim, renewal and completion use separate short transactions;
the worker renews the fencing lease during blocking SMTP and ignores completion
after losing that lease. Leases and retry delays are bounded to 1–300 seconds.

The worker receives only its MAIL DSN, `MAIL_SETTINGS` DSN, Fernet key and
optional SMTP fallback values `IDENTITY_MAIL_SMTP_HOST`,
`IDENTITY_MAIL_SMTP_PORT`, `IDENTITY_MAIL_SMTP_USER`,
`IDENTITY_MAIL_SMTP_PASSWORD` and `IDENTITY_MAIL_SMTP_FROM`. It receives no AUTH
or Recovery DSN, control bearer, JWT signing secret or general API database
credential. Its health surface is `/health` and `/ready` on port 8171. The
optional Compose profile overrides the image's 8170 readiness check for this
process, uses the same image tag for admission and mail, and runs the mail
container nonroot with a read-only filesystem.

The worker creates one stable Message-ID per delivery:
`<aap.{delivery_id}.{purpose}@aap.invalid>`. Delivery is at-least-once. A
disconnect after sending begins is `unknown`, not proof of non-delivery; retry
reuses the same challenge, ciphertext and Message-ID. The closed SMTP outcomes
are `delivered`, `rejected`, `retry`, `unknown` and `invalid_key`; reasons are
exactly `sent`, `not_configured`, `message_invalid`, `tls_failed`, `auth_failed`,
`recipient_rejected`, `sender_rejected`, `data_rejected`, `connect_failed`,
`transport_failed` and `derivation_unavailable`. Known temporary 4xx responses
retry; explicit permanent 5xx rejections are terminal. The shared transport
uses implicit SSL on port 465, mandatory STARTTLS on 587 or whenever the server
advertises it, and preserves configured plain relays on other ports when
STARTTLS is not advertised.

`claim_mail` accepts `{schema,worker_id,lease_seconds,delivery_id}` and returns
at most one lease. The lease binds delivery ID, actor, purpose, recipient,
frontend URL, token hash/ciphertext/key ID, expiry, attempt, worker ID, lease
token and lease expiry. `renew_mail` and `finish_mail` must present the delivery
ID, worker ID and lease token. Finish also carries outcome, reason and an
optional bounded retry delay. Stale or expired lease tokens cannot complete a
new owner's delivery.

`delivered` is terminal. A definite rejection is terminal and retires the
matching password-reset token only; verification links remain usable. `retry`
and `unknown` requeue the same intent. `invalid_key` requeues it without
destroying the challenge. `not_configured` is a definite rejection. Expiry is
terminal. Operators must preserve old encryption keys until associated pending
outbox rows have drained or been explicitly handled.

### Configuration boundaries

The API accepts eight optional direct-value/file settings in four mutually
exclusive pairs: `IDENTITY_RECOVERY_DATABASE_URL(_FILE)`,
`IDENTITY_RECOVERY_MAIL_KEY(_FILE)`, `IDENTITY_MAIL_DATABASE_URL(_FILE)` and
`IDENTITY_MAIL_SETTINGS_DATABASE_URL(_FILE)`. Recovery uses the API caller
transaction when its DSN is omitted. When a dedicated Recovery DSN is set, both
MAIL and MAIL_SETTINGS DSNs are required together; partial binding fails closed
and cannot silently poll the API database. With no Recovery DSN, both mail DSNs
may be omitted for an embedded kick through the API session. If `IDENTITY_URL`
selects a remote owner, that owner must run its own durable mail worker; the API
does not poll a remote outbox. The producer's explicit Fernet key is separate
from JWT signing; embedded mode has a derived-key fallback, while the independent
Compose profile requires an explicit key file.

Compose host selectors use `IDENTITY_RUNTIME_*_FILE`; they name host paths that
Compose mounts as runtime-specific `IDENTITY_*_FILE` values. They are not the
API's direct-value settings. The producer also receives
`IDENTITY_ALLOW_OPEN_REGISTRATION`,
`IDENTITY_EMAIL_VERIFICATION_REQUIRED` and `IDENTITY_FRONTEND_BASE_URL` from
the runtime configuration. The mail process uses
`IDENTITY_MAIL_DATABASE_URL(_FILE)`,
`IDENTITY_MAIL_SETTINGS_DATABASE_URL(_FILE)`,
`IDENTITY_MAIL_ENCRYPTION_KEY(_FILE)` and the five SMTP fallback variables
`IDENTITY_MAIL_SMTP_HOST`, `IDENTITY_MAIL_SMTP_PORT`, `IDENTITY_MAIL_SMTP_USER`,
`IDENTITY_MAIL_SMTP_PASSWORD` and `IDENTITY_MAIL_SMTP_FROM`. SMTP values from
Settings take precedence over those environment fallbacks. Explicit secret
file failures never fall back to another credential source. The runtime does not
read the shared API `.env` file.

## Consequences

Registration and recovery decisions can move with Identity, including the
first verification intent, without exposing raw tokens to the API or keeping an
SMTP connection inside a SQL transaction. Mail worker credentials can be
rotated and restricted independently from account authentication and recovery
credentials. Existing public payloads and link validity are preserved.

The worker adds a process and SMTP delivery remains at-least-once, so duplicates
are possible after uncertain network outcomes. Credential mail intentionally
ignores notification preferences. Recoverability depends on keeping the Fernet
key used by pending intents. The shared image is still Python; this ADR does not
establish language-neutral provider qualification or production capacity.

Group/admin and other-user lifecycle, other token issuers, the remaining API
signers and broad API role are still open. The full Identity module is not yet
independently deployable or replaceable. No primary cutover, production
deployment, Go expansion or alternate-language provider is implied. Real role,
migration, API, SMTP lost-ACK/restart, lease-takeover and restore qualification
must complete before any deployment decision.

## Alternatives Considered

- **Keep SMTP inside public HTTP handlers:** rejected because SMTP availability
  and latency would continue to govern recovery requests and would couple
  network IO to the transaction lifecycle.
- **Move only password reset after registration remains in the API:** rejected
  because a remote Recovery owner cannot atomically observe the new account and
  accept its first verification-mail intent.
- **Give the mail worker the control bearer or API/JWT secret:** rejected because
  mail delivery requires only a dedicated lease capability, a Settings snapshot
  and the Fernet key for its pending messages.

## Notes

- Protocol and key behavior: `apps/identity/src/aap_identity/recovery_contract.py`,
  `recovery_crypto.py` and `mail_contract.py`.
- Owner transactions and worker: `recovery_postgres.py`, `mail_postgres.py`,
  `apps/identity/src/aap_identity_runtime/mail_worker.py` and `mail_config.py`.
- Native migration and exact grants: `apps/api/alembic/sql/0239_identity_recovery.sql`
  and `infra/docker/identity-recovery-grants.sql`.
- API composition and public compatibility: `apps/api/app/config.py`,
  `identity_recovery_commands.py` and `identity_mail_delivery.py`.
- Remaining full Identity scope: [module deployment status](../../docs-site/dev/concepts/module-deployment.md).

# Identity

This package contains the JWT/API-key verifier, API-key management, core sessions
and self-account command owners, their closed contracts, and one private HTTP process.
The runtime has no application, SQLAlchemy, Celery or object-store dependency.
Admission uses four PostgreSQL capabilities, key management uses two, and
sessions use three plus a scoped Redis login-counter identity. Each configured
capability has a separate database login. Self-account commands use three functions
and presence maintenance uses one separate operator function. Registration,
password recovery and email verification now have five versioned recovery
operations. Group/admin, other-user hydration and multi-owner lifecycle remain
in the API, so this is not yet a complete Identity service.

## Run

Install the current migration head and the exact Identity owner/runtime grants
for the enabled capabilities first.
Provide exactly one direct value or secret file for each of
`IDENTITY_DATABASE_URL`, `IDENTITY_TOKEN` and `IDENTITY_SIGNING_SECRET` (file
variants append `_FILE`). The DSN uses `postgresql://` and the signing secret
matches the API's `SECRET_KEY`; configuration errors never fall back. The
runtime does not load an API `.env` file.

```sh
uv run --project apps/identity --locked --extra runtime python -m aap_identity_runtime
```

`/health` is process liveness; `/ready` calls and checks the native policy.
Private `POST /v1/identity/authenticate` requires the control bearer and a JSON
`{"token":"..."}` body. Valid credential verdicts return a closed success or
failure envelope with HTTP 200. Invalid control credentials return 401, malformed
requests return 400, and owner failures/deadlines return 503. Bodies never echo
the supplied credential. Concurrency and deadlines are bounded. Account preferences
retain their existing accepted size/cardinality set rather than inheriting the
smaller admission/session byte limits.

`docker-compose.identity.yml` starts the optional admission and credential-mail
processes from the same `aap-identity:local` image. The image contains installed
packages, not an application source bind, and both services support a read-only
root filesystem. Session commands require their own database DSN and Redis
identity; admission and API-key management do not use Redis. No capability needs
S3 credentials. See the [private protocol guide](../../docs-site/api/guides/identity-runtime.md)
and [local runbook](../../DEV.md) for configuration and exact permissions.

The monolithic application's Identity function allowlist is included in
`infra/docker/lan-production-grants.sql`; this grant-source change has not been
applied to production. A dedicated deployment uses the separate capability
LOGINS described below.

## Credential cutoff

The codec requires issuer `aap-identity/v1` and derives its HS256 signing key
from the existing signing secret with HMAC-SHA256 and that exact issuer context.
Legacy JWTs must log in again. API keys retain their SQL revocation/expiry state.
Stop every old REST/WS issuer and verifier before the coordinated upgrade;
never roll back to a validator that accepts legacy JWTs. Do not approximate old
Redis revocations by initializing durable generation to zero while accepting
old tokens.

JWT generation and JTI revocation now live in native Identity state. Lifecycle
coordinators mutate them on the same database transaction as their existing
business writes. JTI retention includes the seven-day refresh grace. An
admission verdict describes the credential at resolution time; a business
command requiring a final-write fence must explicitly call the native current
principal lock on its own transaction. Admission by itself is not that fence.

The package distributes admission, key, session, account, presence, recovery and
mail schemas.
Generate/check them with `scripts/export_schema.py` / `--check`. Native data
and privilege contracts live with migrations and the corresponding
`infra/docker/identity-*-grants.sql` files. The Go experiment is not involved
in this service.

## Preference policy

`aap_identity.preferences` owns preference read normalization and sparse patch
validation/merge. `AccountManager` keeps the User lock, portable calculation and
native apply on one owner transaction. The API can bind the same workflow to a
caller transaction or explicitly select the independent private endpoint;
configured remote/dedicated-DSN failures never choose another writer.

The distributed `preferences_write.schema.json` describes the current strict
editable model after legacy-key promotion and opaque stored-value filtering;
it is not a replacement for the stateful patch policy. The read schema allows
opaque future workspace and shortcut data. A replacement must also preserve
atomic map replacement, revision CAS, schema/engine downgrade errors, safe
validation diagnostics and unchanged opaque values. Schema validation alone
does not implement those behaviors.

## Self-account commands

`POST /v1/identity/accounts/execute` accepts `aap-identity-accounts/v1` with a
user credential plus the separate control bearer. The account DSN uses the
three-function LOGIN; `POST /v1/identity/presence/mark-inactive` uses the separate
single-function presence DSN. Both are optional capabilities in the same process.
See [ADR-0118](../../docs/adr/0118-independent-identity-account-commands.md) for
locking, mutation receipts, current-credential replay and avatar commit/cleanup
boundaries. Password verification/hash and preference merge belong to this
package; S3 and image bytes do not.

## Registration and credential recovery

`POST /v1/identity/recovery/execute` uses the private control bearer and the
closed `aap-identity-recovery/v1` protocol. It exposes five operations:
`register_open`, `request_password_reset`, `resend_email_verification`,
`consume_password_reset` and `consume_email_verification`. The API retains
CAPTCHA, the three-per-minute recovery rate limit and existing public status /
response envelopes. Forgot-password and verification resend return the same
generic 202 response for known and unknown addresses after the owner accepts the
operation; SMTP does not run in the request path.

Identity owns challenge consumption, credential generation changes, registration
Audit, successful-operation receipts and the durable mail outbox. Registration
with verification required commits User, challenge, mail intent and Audit
together and returns no token. Otherwise it returns the existing JWT shape from
frozen claims. Role remains `viewer`, with no project group. Reset and verification
tokens are SHA-256 hashed in their token tables; existing plaintext token values
are hashed in place during migration so already issued links remain usable.
The outbox stores only the token hash and Fernet-encrypted token, plus the frozen
recipient and frontend URL. Raw challenge tokens never appear in API / RPC replies
or logs. Recovery operation receipts store a keyed request fingerprint and frozen
result, not passwords, tokens or JWTs.

The recovery LOGIN has exactly `recovery_policy`, `prepare_recovery_command` and
`apply_recovery_command`. The mail LOGIN has only `mail_policy`, `claim_mail`,
`renew_mail` and `finish_mail`. A third MAIL_SETTINGS LOGIN can call only
`mail_delivery_policy` in Settings. These function roles do not receive direct
table access. Identity's code owner can call only
`aap_settings_v1.identity_recovery_policy(jsonb)` for open-registration and
frontend-URL policy; it takes a Settings SHARE lock to fence registration. The
MAIL_SETTINGS function reads exactly SMTP host, port, user, password and sender
in one snapshot, with DB values overriding environment fallbacks;
credential-mail delivery does not use notification preferences.

## Independent credential-mail worker

The `identity-mail` service runs
`python -m aap_identity_runtime.mail_worker` from the same image as
`identity-admission`. It polls the durable outbox, claims one message, commits
its lease before SMTP, and uses short transactions for claim, renew and finish.
SMTP never runs under an SQL lock. The default lease is 60 seconds, renewable
while SMTP blocks; lease and retry intervals are bounded to 1–300 seconds. A
stable Message-ID lets retries reuse the same challenge, ciphertext and message
identity. SMTP is at-least-once: a disconnect after sending begins may deliver a
duplicate, so the worker reports an unknown result and requeues the same intent.

`delivered` and definite `rejected` results are terminal. A definite rejection
retires only the matching password-reset token; verification tokens remain
usable. `not_configured` is a definite rejection. `retry` and `unknown` preserve
the challenge and requeue; `invalid_key` also requeues without destroying the
token. Expired intents are terminal. Keep old Fernet keys available until pending
outbox rows encrypted under them drain; changing the mail key does not invalidate
already hashed links, but those rows cannot be sent without their original key.

The worker listens on port 8171; `/health` is liveness and `/ready` checks both
function capabilities, Settings access and the encryption key. Compose overrides
the image health check to use `/ready` on 8171. The nonroot mail container gets
only its MAIL DSN, `MAIL_SETTINGS` DSN, the shared Fernet key and optional
`IDENTITY_MAIL_SMTP_HOST`, `IDENTITY_MAIL_SMTP_PORT`, `IDENTITY_MAIL_SMTP_USER`,
`IDENTITY_MAIL_SMTP_PASSWORD` and `IDENTITY_MAIL_SMTP_FROM` fallbacks. It receives
no AUTH/control bearer, JWT signing secret or recovery DSN.

For direct process configuration, the API has eight optional
`IDENTITY_RECOVERY_DATABASE_URL(_FILE)`, `IDENTITY_RECOVERY_MAIL_KEY(_FILE)`,
`IDENTITY_MAIL_DATABASE_URL(_FILE)` and
`IDENTITY_MAIL_SETTINGS_DATABASE_URL(_FILE)` settings. A configured pair accepts
one value or one file; explicit file errors do not fall back. With no recovery
DSN, recovery uses the API caller transaction. If a recovery DSN is configured,
both mail DSNs must also be configured together; partial binding fails closed
instead of silently polling the API database. If both mail DSNs are omitted,
the embedded mail kick may use the API session. A remote `IDENTITY_URL` requires
the selected Identity owner to run its own mail worker; the API does not poll a
remote outbox. The recovery key can be derived from the signing secret when the
API runs embedded, but independent Compose deployment requires the explicit
shared Fernet key file. Runtime direct
configuration uses `IDENTITY_RECOVERY_DATABASE_URL(_FILE)` and
`IDENTITY_RECOVERY_MAIL_KEY(_FILE)` for the producer, and `IDENTITY_MAIL_*` DSN,
encryption-key and SMTP settings for the worker. The Compose wrapper names
`IDENTITY_RUNTIME_*_FILE` select host files; inside the containers those mount at
the runtime-specific `IDENTITY_*_FILE` names. The runtime does not read the API
`.env` file. See the [local runbook](../../DEV.md) and
[ADR-0119](../../docs/adr/0119-independent-identity-recovery-and-mail.md) for
the exact file mapping and lifecycle rules.

This implementation is a local architecture slice. It does not complete the
Identity module, authorize a primary cutover or production deployment, or supply
an alternate-language provider. Group/admin workflows and full lifecycle
ownership remain open.

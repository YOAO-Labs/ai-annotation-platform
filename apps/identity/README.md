# Identity

This package contains the JWT/API-key verifier, API-key management, core sessions
and self-account command owners, the group catalog/assignment owner, their closed
contracts and one private HTTP process. Self-deactivation user and due commands
are the next Identity ownership slice; qualification is pending.
The runtime has no application, SQLAlchemy, Celery or object-store dependency.
Admission uses four PostgreSQL capabilities, key management uses two, and
sessions use three plus a scoped Redis login-counter identity. Each configured
capability has a separate database login. Self-account commands use three functions
and presence maintenance uses one separate operator function. Registration,
password recovery and email verification now have five versioned recovery
operations. Self-deactivation adds separate two-function user and due-operator
capabilities; the Notifications publisher is a distinct three-function/Redis
process. Group invitations, user directory/platform administration,
other-user hydration and
multi-owner lifecycle remain in the API, so this is not yet a complete Identity
service.

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

The package distributes admission, key, session, account, presence, group,
recovery and mail schemas.
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

## Group catalog and assignments

`POST /v1/identity/groups/execute` accepts the control bearer plus a user
credential in `aap-identity-groups/v1`. It owns eight operations: `list_groups`,
`query_groups`, `create_group`, `update_group`, `delete_group`,
`preview_group_assignments`, `apply_group_assignments` and `assign_user_group`.
The API consumes the current principal and value replies for these routes; it
does not read Group or User rows directly for them.

`GroupRecord` is exactly `{id,name,description,member_count,created_at}`;
`created_at` is timezone-aware and `member_count` counts active users. List order is name then UUID. Search trims
with Python `str.strip()` semantics and uses ILIKE on name or description;
query page size is 1–500 and there is no new search-length limit. Names trim the
same Unicode whitespace and retain the 1–100 character limit. Descriptions are
nullable and at most 500 characters. Assignment batches contain 1–500 IDs,
preserve duplicates and input order, and return the existing per-item public
DTOs without imposing a response byte cap.

Both super-admin and project-admin roles manage the global group catalog.
Super-admin can assign any target. Project-admin assignment scope is self, a
member of a currently owned project, or any active employee; bulk operations
also retain inactive members of owned projects. Single-user assignment requires
an active target. Out-of-scope targets stay indistinguishable from missing users.
Preview and apply preserve partial per-item outcomes; a requested missing group
is a top-level 404 on apply but a per-item failure in preview.

The `identity_group_client` LOGIN can call only `group_policy()` and
`execute_group_command(jsonb)`. It has no direct Group, User, Project or
ProjectMember table rights. Workspace returns the currently managed user IDs
through its scope capability; Identity does not infer scope from project tables.
Writes lock Group before actor/target User rows. Workspace locks owned Projects
before membership rows and holds the scope through commit. Bulk catalog changes
prelock affected users in UUID order; transient conflicts stay retryable without
leaking SQL errors.

Successful receipts cover create/update/delete, single assignment and bulk apply
(including an all-item-failed result); list, query and preview do not create
receipts. Replay reauthenticates a current manager. Assignment replay checks
current target visibility before returning frozen profile data. State, typed
Audit and receipt commit together. The Audit owner accepts only the five group
and group-assignment events; there is no generic Audit-table grant.

Invitation acceptance remains a legacy coordinator. It calls the private
`resolve_invitation_group(uuid,text)` helper within the existing transaction to
resolve/create the invitation's group; this helper is not exposed through the
GROUP LOGIN. Group migration therefore does not extract invitations, directory
search, admin/lifecycle commands, or multi-owner member transfers. The optional
API/runtime `IDENTITY_GROUP_DATABASE_URL(_FILE)` selects the two-function owner;
an explicit connection failure does not fall back, and a missing configured
capability returns 503. The Compose host selector is
`IDENTITY_RUNTIME_GROUP_DATABASE_URL_FILE`. See [ADR-0120](../../docs/adr/0120-independent-identity-group-catalog-and-assignment.md).

## Self-deactivation and final-super-admin fence

The next self-service commands use `aap-identity-deactivation/v1` for the
current account's request/cancel operations and
`aap-identity-deactivation-due/v1` for a control-bearer-only due page. The user
must be authenticated by a current JWT/API key; an accepted request remains
pending across administrative suspension/reactivation and is cleared only by
explicit cancellation or successful due completion.

An Identity-owned global advisory try-lock protects every operation that could
reduce the active super-admin set. The same fence covers the self request and
legacy role/lifecycle mutations, before ordered User row locks. A new
super-admin request requires another active, unreserved super-admin; due
processing rechecks the actual active count before disabling each one. If that
would disable the last active super-admin, the page records a blocked outcome
and preserves the pending request. Due page limits are per-transaction only:
the existing daily worker drains pages until no progress, with no total user
cap.

The due effect remains the existing soft deletion and does not advance JWT
generation, revoke API keys, or transfer tasks/projects. Approval Audit
redaction is deliberately partial: it nulls only existing actor email/role
columns and keeps detail JSON and old notification identity fields. It does not
claim full GDPR erasure.

The portable user/due command contracts and Postgres owner live in
`aap_identity.deactivation_contract`, `aap_identity.deactivation_manager`, and
`aap_identity.deactivation_postgres`; the runtime exposes both private protocols
through its existing API process.

Identity writes the two fixed deactivation events through the Notifications
owner in the same database transaction. Delivery uses the separate
`notification_publish_client` and the same package's
`python -m aap_identity_runtime.notification_worker` process; this publisher
requires only `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` and
`NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`, with no Identity/auth/signing secret.
Its portable DTO and Postgres execution live in
`aap_identity.notification_delivery_contract` and
`aap_identity.notification_delivery_postgres`; runtime settings are owned by
`aap_identity_runtime.notification_config`.
See [the private protocol](../../docs-site/api/guides/identity-runtime.md#自助注销与最后一名超级管理员围栏),
[ADR-0121](../../docs/adr/0121-identity-self-deactivation-and-notification-delivery.md),
and the [deployment slice status](../../docs-site/dev/concepts/module-deployment.md).

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
API runs embedded. The independent runtime requires an explicit shared Fernet
key whenever recovery is enabled; Compose requires its key file. Runtime direct
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

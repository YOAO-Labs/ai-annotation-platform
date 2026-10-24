# Identity

This package contains the JWT/API-key verifier, API-key management, core sessions
and self-account command owners, the user-directory read owner, administrator
password-reset, account-deletion and offboarding owners, group catalog/assignment
owner, and invitation/mail-v2 owners and their closed contracts. It has one
private HTTP process plus separate private worker entrypoints. The complete
module has passed local functional qualification with the existing Compose
private network, a separately installed API consumer, and all configured
capabilities. See [module deployment](../../docs-site/dev/concepts/module-deployment.md#identity-整模块独立装配)
for the exact evidence and remaining deployment boundaries.
The runtime has no application, SQLAlchemy, Celery or object-store dependency.
Admission uses five PostgreSQL functions, key management uses two, and
sessions use three plus a scoped Redis login-counter identity. Each configured
capability has a separate database login. Self-account commands use three functions
and presence maintenance uses one separate operator function. Registration,
password recovery and email verification now have five versioned recovery
operations. Self-deactivation adds separate two-function user and due-operator
capabilities; the Notifications publisher is a distinct three-function/Redis
process. Invitation commands use a separate three-function Identity capability;
MailV2 uses its own four-function sender role. User directory and UserBrief reads
use separate two-function Identity capabilities. Platform-role preview and change use
their own two-function Identity capability. Account deletion uses a separate
two-function Identity capability and preserves its inactive-target no-op, task
handoff and partial Audit actor redaction; it does not retire credentials.
Offboarding preview and commit use a separate three-function Identity
capability.
The API retains public HTTP aggregation and cross-domain analytics. Account
commands use the selected Identity owner; profile consumers use typed reads or
the two public transactional relations. Other domains retain their own business
writes and compose explicit same-transaction capabilities when required.

## Run

Install the current migration head and the exact Identity owner/runtime grants
for the enabled capabilities first.
Provide exactly one direct value or secret file for each of
`IDENTITY_DATABASE_URL`, `IDENTITY_TOKEN` and `IDENTITY_SIGNING_SECRET` (file
variants append `_FILE`). The DSN uses `postgresql://`. A remote Identity
service owns its signing/derivation secret; it need not equal the API edge's
`SECRET_KEY`. The edge authenticates RPCs with `IDENTITY_TOKEN` and does not
rederive Identity credentials. Keep the existing Identity secret stable for
live issuers and durable receipts; this is not a key-rotation procedure.
Configuration errors never fall back. The runtime does not load an API `.env` file.

For a separate API admission connection, optionally provide
`IDENTITY_ADMISSION_TOKEN` or `IDENTITY_ADMISSION_TOKEN_FILE`. It must be a
canonical bearer value distinct from the normalized `IDENTITY_TOKEN`. This
scoped bearer is accepted only by `/v1/identity/authenticate` and
`/v1/identity/actor-status`; command, session, and administrator endpoints
continue to require the general token. Provide
`IDENTITY_ACTOR_STATUS_DATABASE_URL` or its `_FILE` variant to enable the
second-handshake status read. It uses a dedicated LOGIN with only
`aap_identity_v1.read_actor_facts(uuid)` and has no fallback to the admission
database URL. Without that source, `/v1/identity/actor-status` returns 503.

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

`docker-compose.identity.yml` starts optional admission and Recovery credential-
mail processes from the same `aap-identity:local` image. The opt-in `mail-v2`
profile adds `identity-mail-v2` with its own MAILV2 DSN plus the existing
MAIL_SETTINGS and Fernet key files; only this sender receives SMTP fallback
configuration. These services use installed packages, not an application
source bind, and support a read-only root filesystem. Session commands require
their own database DSN and Redis identity; admission and API-key management do
not use Redis. No capability needs S3 credentials. See the
[private protocol guide](../../docs-site/api/guides/identity-runtime.md) and
[local runbook](../../DEV.md) for configuration and exact permissions.

The monolithic application's Identity function allowlist is included in
`infra/docker/lan-production-grants.sql`; this grant-source change has not been
applied to production. A dedicated deployment uses the separate capability
LOGINS described below.

## Local complete-module qualification

The existing Compose assembly runs one HTTP owner with all 17 configured
capabilities, two mail workers and a notification publisher, alongside a cold API
image. PostgreSQL and Redis use the owned Compose private network. The API's
Identity access is restricted to two public relations and five transactional
functions, with no private table or credential-command access. Its edge secret
differs from the stable owner signing secret.

The noneditable installed package passes 658 checks. File probes match 129 package
files in the installation and both images, plus 1,294 API/Native/metadata inputs.
Full migration validation passes all five phases. The complete real-consumer
case passes twice, including REST/Socket credentials, lifecycle writes, Node
replay, worker restarts, and full database restore with pending mail and
notifications. Existing operation-specific qualifications are retained separately.
The runbook entry is [Identity 整模块私网验收](../../DEV.md#identity-整模块私网验收).

This qualifies local ARM64 functionality on that private-network topology. Earlier
host-forwarded database experiments had intermittent 503 responses whose root
cause remains unconfirmed; private-network passes do not establish a fix for
that path. Primary runtime cutover, production/AMD64/remote-CI qualification and
a complete alternate-language provider remain outside this evidence.

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
directory, invitation, recovery, MailV1 and MailV2 schemas.
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

The earlier group slice exposed only the private
`resolve_invitation_group(uuid,text)` helper to the legacy acceptance workflow;
the helper remains private to Identity and is not granted to `identity_group_client`.
The full invitation contract below now owns resolve/register/accept and its
multi-owner transaction. Group search and remaining admin/lifecycle work stay
outside the invitation slice. The optional API/runtime
`IDENTITY_GROUP_DATABASE_URL(_FILE)` still selects only the two-function GROUP
owner and never the invitation helper. See [ADR-0120](../../docs/adr/0120-independent-identity-group-catalog-and-assignment.md).

## User directory reads

`POST /v1/identity/directory/execute` accepts the control bearer plus a current
user credential in `aap-identity-directory/v1`. It owns four reads:
`list_users`, `query_users`, `user_stats` and `export_users`. The optional
`IDENTITY_DIRECTORY_DATABASE_URL(_FILE)` selects its dedicated PostgreSQL login;
there is no fallback to the application session when this owner is configured.
Readiness requires the exact `directory_policy()` and
`execute_directory_command(jsonb)` capabilities and rejects direct table,
column, sequence and schema-creation privileges, plus every `aap_*` capability,
`SECURITY DEFINER` function and non-`PUBLIC` execute grant inherited by the
directory login. Extension-owned functions and ordinary `public` invoker helpers
available only through the default `PUBLIC` execute grant are tolerated; they
do not grant the login direct relation access. Public schema creation and direct
table, column, sequence or extra callable-function grants remain rejected.

Legacy `/users` listing and management `/users/query` intentionally keep their
different project-admin visibility rules. The owner returns the frozen user
projection and query capability flags; CSV/JSON rendering and HTTP response
formatting stay in the API. Export rows, metadata, audit and the operation
receipt commit atomically. Search uses Python's full Unicode `str.strip()`
semantics and PostgreSQL ILIKE; the owner does not add a row-count or response
byte cap. See `directory_protocol.schema.json` and
`native_directory_protocol.schema.json` for the closed private DTOs.

## UserBrief enrichment

The private `POST /v1/identity/briefs/execute` route supplies the batched six-field
`UserBrief` used by existing task, batch, discussion and history responses. The
API applies each route's existing authorization before sending the selected user
IDs. The control bearer protects this service-only capability; the Identity
brief owner accepts no end-user credential or actor identity.

Identity returns current `users` profile fields without active/status filters,
and asks Workspace for project membership roles through a narrow helper when a
project overlay is requested. Missing users are omitted; users without a
project membership fall back to their global role. `avatar_initial` keeps the
existing Python Unicode and whitespace behavior. The optional
`IDENTITY_BRIEFS_DATABASE_URL(_FILE)` selects its exact two-function,
read-only LOGIN; the owner also preserves the caller transaction when embedded.
The closed wire and native DTOs are `briefs_protocol.schema.json` and
`native_briefs_protocol.schema.json`.

## Administrator password reset

The private `POST /v1/identity/admin-passwords/execute` accepts the control
bearer and a current user credential in `aap-identity-admin-passwords/v1`. It
owns the password hash, reset marker, typed audit entry and idempotency receipt
in one owner transaction. The temporary password is deterministically derived
from the signing-secret epoch, actor, operation UUID and target; bcrypt cost 12
is computed before opening the database transaction. The immutable receipt
stores no plaintext password or usable password hash, and a replay is returned
only while the target's current password state still matches it.

The optional `IDENTITY_ADMIN_PASSWORD_DATABASE_URL(_FILE)` selects the dedicated
two-function owner login. A configured owner never falls back to the API
transaction, and `/ready` verifies the exact policy and least-privilege
capability set. See `admin_password_protocol.schema.json` and
`native_admin_password_protocol.schema.json` for the closed wire and native
DTOs. The public API adapter retains the existing reset response and handles
the temporary password as a secret. Platform-role preview and mutation are
described below.

## Platform-role preview and change

The private `POST /v1/identity/platform-roles/execute` accepts a current user
credential in `aap-identity-platform-roles/v1`; its dedicated two-function
login uses `IDENTITY_PLATFORM_ROLE_DATABASE_URL(_FILE)`. It owns the existing
super-admin-only role preview and change policies while leaving project
membership, work assignments, and account lifecycle state untouched. Preview
keeps the exact twelve-field impact DTO and seven-field project entries;
mutation returns the exact 21-field User projection, including opaque
preferences and historical status values.

Role-change receipts preserve the accepted User snapshot. Replaying an operation
reauthenticates the current super admin and target, then returns that historical
snapshot without changing the current role or writing a second Audit row. New
public requests still receive fresh operation IDs. Readiness validates the exact
policy and function capabilities; a selected unavailable source never falls
back to the API transaction. The wire, native and supporting Workspace and
Labeling fact schemas are `platform_role_protocol.schema.json`,
`native_platform_role_protocol.schema.json`,
`platform_role_workspace_facts.schema.json` and
`platform_role_labeling_facts.schema.json`.

## Invitation lifecycle and MailV2

The private `POST /v1/identity/invitations/execute` speaks
`aap-identity-invitations/v1` and owns thirteen existing actions: single and
batch creation/preview, list/query/stats/export, revoke/resend/send-current-link,
public link resolution/registration and authenticated existing-account
acceptance. Existing HTTP routes, response fields and SDK inputs stay unchanged.
Manager operations and existing-account acceptance require a current user
credential in `token`; public resolve/register use null there and keep the proof
in `payload.invitation_token`. The two tokens are never interchangeable.

Invitation policy rechecks the current inviter, issuance role, project owner,
target account and compatible project role before committing. Project admins
create and manage only their own invitation rows, cannot use global scope, and
may invite only employee/viewer; privileged platform roles require a current
super-admin issuer. User, optional Group, Workspace ProjectMember, invitation
consumption and typed Audit commit in one database transaction. Labels from
Labeling report current active batches as a work hint; they do not create or
assign tasks. Owner/project moves, credential revocation and duplicate member
acceptance are fenced before commit.

Batch preview rolls back the full enclosing transaction and returns no tokens
or links; apply commits successful rows with savepoints and keeps ordered
per-item results. List has no total-row cap; query remains capped at 500 rows
and preserves current date/ILIKE semantics. CSV/JSON encoding stays in HTTP
after the owner has selected, counted and audited the export. Successful
public operations keep the same allowed link/token response fields; receipts
never store a second plaintext proof or generated JWT.

`identity_invitation_client` can execute only `invitation_policy()`,
`prepare_invitation_command(jsonb)` and `apply_invitation_command(jsonb)`.
Workspace owns the project and membership locks, Labeling owns the active-batch
summary, Settings provides a bounded invitation policy snapshot, Audit receives
only fixed invitation events, and Identity privately reuses the Group helper.
The invitation owner locks sorted User authorities, then the email advisory
key, then Project, then Invitation/ProjectMember; it captures project ownership
before locking and revalidates it after locks. This keeps User, Group, Workspace
membership, proof consumption and Audit atomic in the shared PostgreSQL owner.

`POST /api/v1/invitations/{id}/send-email` still returns 200 on completed send
and 502 on failure/timeout, sends the current link, and never rotates or revokes
the invitation. A durable intent is accepted before the bounded HTTP wait
(default 60 seconds, maximum 120). If the wait expires while status is pending,
the sender may deliver later; SMTP rejection retires only that send intent. An
explicit resend/new-link operation is required to rotate a token.

Invitation SMTP is isolated from the Identity producer in the versioned
`aap-identity-mail-v2` protocol. `identity_mail_v2_client` can execute exactly
`mail_policy()`, `claim_mail(jsonb)`, `renew_mail(jsonb)` and
`finish_mail(jsonb)`; the v2 worker handles invitation plus the two recovery
purposes, while the original v1 worker and grants remain valid and recovery-only.
It claims a typed invitation lease and commits before SMTP, with no SQL lock
held across network I/O. Its `user_id` is the issuer; recipient email and
invitation/project values come from the sealed V2 lease. Unknown SMTP outcome
is at-least-once with the same Message-ID; the current invitation state is
rechecked at claim/renew/finish boundaries. Keep old Fernet keys available until
pending messages encrypted with them are delivered or manually handled.

API/runtime invitation DSN uses `IDENTITY_INVITATION_DATABASE_URL(_FILE)`;
the API may optionally configure `IDENTITY_MAIL_V2_DATABASE_URL(_FILE)` for an
embedded kick after commit. Runtime link fallback is
`IDENTITY_INVITATION_FRONTEND_BASE_URL=http://localhost:5173`, separate from
Recovery's 3000 fallback; system `frontend_base_url` still overrides it.
The opt-in Compose `mail-v2` profile runs
`python -m aap_identity_runtime.mail_worker_v2` with its own four-function
MAILV2 DSN, the existing MAIL_SETTINGS DSN and Recovery Fernet key, and SMTP
fallbacks; it receives no AUTH/control/JWT/signing credential. See [the private
protocol](../../docs-site/api/guides/identity-runtime.md#邀请生命周期),
[ADR-0122](../../docs/adr/0122-independent-identity-invitations-and-mail-v2.md),
and the [module status](../../docs-site/dev/concepts/module-deployment.md).

## Self-deactivation and final-super-admin fence

Self-service commands use `aap-identity-deactivation/v1` for the
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

## Administrator suspension and reactivation

The private `POST /v1/identity/suspensions/execute` protocol owns the existing
`POST /api/v1/users/{id}/deactivate` and `POST /api/v1/users/{id}/reactivate`
commands. The optional `IDENTITY_SUSPENSION_DATABASE_URL(_FILE)` selects its
dedicated two-function PostgreSQL owner; a configured source never falls back
to the API transaction. The owner checks the current manager and target under
the shared super-admin fence and preserves the existing project-admin scope
rules. It retires old credentials with each real state change, but successful
replays return their frozen User result without revoking credentials created
after acceptance. Accepted self-deactivation reservations survive both
operations.

Suspension receipts also cover the existing inactive-target no-op, without an
Audit row or credential retirement. The reactivation wire binds the raw reason,
while the native command carries only its Python `str.strip()`-based
`reason_present` boolean; neither Audit nor receipts store the raw reason. The
closed wire and native DTOs are `suspension_protocol.schema.json` and
`native_suspension_protocol.schema.json`.

## Administrator account deletion

The private `POST /v1/identity/deletions/execute` protocol owns the existing
`DELETE /api/v1/users/{id}` command. The optional
`IDENTITY_DELETION_DATABASE_URL(_FILE)` selects its dedicated two-function
PostgreSQL owner; a configured source never falls back to the API transaction.
Successful receipts preserve the original User result and prevent retries from
repeating task transfers, lock release, Audit writes or historical actor
redaction. An already inactive target remains a no-op and is returned as its
historical snapshot. Deletion preserves the legacy task handoff and partial
Audit redaction behavior and does not retire credentials. The closed wire and
native DTOs are `deletion_protocol.schema.json` and
`native_deletion_protocol.schema.json`.

## User offboarding preview and commit

The private `POST /v1/identity/offboarding/execute` protocol owns the existing
offboarding preview and commit commands. The optional
`IDENTITY_OFFBOARDING_DATABASE_URL(_FILE)` selects its dedicated PostgreSQL
owner; it never falls back to the API transaction. The owner preserves the
legacy Python snapshot projection and SHA-256 preview version, rechecks current
manager scope before that version, and commits project ownership, work
assignments, credential retirement, task-lock release, Audit and the private
operation receipt in one transaction.

The versioned LOGIN can execute only `offboarding_policy()`,
`prepare_offboarding_command(jsonb)` and `apply_offboarding_command(jsonb)`;
it receives no direct application-table or sequence privileges. Public retries
still use a fresh operation ID, so a consumed preview remains stale; a private
retry returns its frozen result without repeating work. The closed wire and
native DTOs are `offboarding_protocol.schema.json` and
`native_offboarding_protocol.schema.json`.

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

The recovery LOGIN has exactly `recovery_policy`, `prepare_recovery_command`,
`apply_recovery_command` and `read_registration_status`. The mail LOGIN has only `mail_policy`, `claim_mail`,
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

Recovery and mail have their own operation-level qualification. Complete-module
assembly and its local-only limits are recorded above; this evidence does not
authorize a primary cutover or production deployment, or supply an
alternate-language provider.

`aap_identity.briefs_client.BriefsClient` provides the strict, batched Briefs client
for other owners. Empty requests do not initialize a provider; selected HTTP errors
never fall back to SQL, and a shared deadline covers the selected provider.
uv cache keys include Python/schema sources and metadata so source-only client
changes also invalidate a noneditable wheel.

## Accepted API-key final authority

`aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])` confirms current owner,
revocation, expiry and required scopes for already accepted work holding only a
key ID. It is not credential authentication. The caller first retains its
account/project authority locks, then this capability waits on the key's SHARE
lock until that same caller transaction commits or rolls back. Expiry uses the
real clock after any wait. Hash rotation preserves the existing accepted-key-ID
semantics; neither plaintext nor a key hash crosses this boundary.

The portable request and SQL boolean result descriptor use
`aap-identity-accepted-api-key/v1`; `accepted_api_key_schema()` generates both the
package artifact and the migration snapshot. The fixed function belongs to
Identity; Learning and the transitional application runtime have explicit
execution grants. AUTH, key-management and task-creation clients gain no new
permission. No new runtime, LOGIN, DSN or HTTP endpoint is added.

Current single-account reads in prediction, scheduling, tracker, Mask Repair,
export, sockets, jobs, uploads, events and discussion entry points reuse the
existing `read_actor_facts` or caller-transaction account locks. Existing lack of
an active check in existence-only paths is preserved; project/task authorization
still runs at its original boundary. Focused source/Native acceptance is recorded
in [ADR-0161](../../docs/adr/0161-identity-current-authority-consumers.md).
Complete-module installation/container/restore evidence is recorded above and
in the module-deployment ledger; the focused authority checks remain separate.

## Transactional read relations

`aap-identity-read-relations/v1` publishes two fixed PostgreSQL relations owned
by Identity. `account_profiles` has exactly `id`, `name`, `email`, `role`,
`is_active`, `status` and nullable string `avatar_ref`;
`invitation_audit_targets` has `id` and nullable `project_id`. Both are
security-barrier views. Values remain raw and unfiltered, without credentials,
credential generations, account lifecycle details or invitation tokens.

Trusted backend consumers retain their original scope predicates, joins,
counts, ordering, limits and caller transaction snapshot in the same SQL
statement. The relations do not authorize the caller or replace final account
locks. They expose an enumerable profile projection, so they must never be
granted to PUBLIC, browser roles or ordinary function-only Identity clients.
`identity_read_relation_schema()` generates the package and migration JSON
descriptors. API Core adapters honor ORM autoflush/no-autoflush and never
commit or roll back the caller's transaction.

For an existing API role whose Identity bindings are all remote, apply
`infra/docker/identity-consumer-grants.sql` **after** migration, owner and legacy
grants. Its explicit variables are `expected_database`, `migration_role`,
`identity_owner` and `consumer_role`. It validates ownership and removes direct
table/column Identity privileges and Identity command functions, then permits
only these two SELECT relations plus `read_actor_facts`, the three account-lock
functions and `check_accepted_api_key`. Missing remote bindings fail instead of
falling back to local credential commands.

The embedded/isolated seed profile remains compatible and broad. The consumer
profile does not rewrite its default privileges: run it last after each grant
refresh and reevaluate new Identity relations during migration. It does not
create another runtime, LOGIN, DSN or deployment mode. Focused source and real
LOGIN evidence are in [ADR-0162](../../docs/adr/0162-identity-read-relations-and-consumer-grants.md).

## Registration policy reads

The existing Recovery binding exposes private
`POST /v1/identity/recovery/registration-status` using
`aap-identity-registration-status/v1`. Requests contain only `schema` and
`operation: read_status|read_deployment_default`; successful replies echo that
operation and return one strict `data.allow_open_registration` boolean. The
owner holds its own defaults; callers cannot supply policy values on the wire.

Effective reads reuse the existing Settings policy and SHARE fence through the
new Recovery-owned `read_registration_status(jsonb)` facade. They do not create
users, receipts, audit events or mail. The separate default-only read performs
no database I/O, so admin readback can still repair a corrupt override. An
auth-only runtime can report deployment defaults while its effective registration
read remains unavailable without Recovery data capabilities.

The API status endpoint reads fresh owner truth. Settings management threads the
same one-key owner default through snapshots, metadata, version/CAS, reset and
conflict readback; accepted import limits keep the same management version.
Embedded and dedicated-database modes use the defaults of their existing
RecoveryManager. Remote mode uses the owner and never falls back to API defaults
on failure. Existing email-verification, registration, receipt, CAPTCHA and mail
semantics remain. There is no new credential, DSN, deployment mode or environment
variable. Focused evidence and complete-module qualification boundaries are in
[ADR-0163](../../docs/adr/0163-identity-registration-policy-owner.md).

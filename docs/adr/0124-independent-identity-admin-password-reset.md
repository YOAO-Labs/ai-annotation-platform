# 0124 — Identity 管理员临时密码重置所有权

- **Status:** Accepted; locally qualified slice (2026-10-20)
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0118, ADR-0119, ADR-0120, ADR-0121, ADR-0122 and ADR-0123

## Context

`POST /api/v1/users/{user_id}/admin-reset-password` currently checks manager
authority, selects a temporary password, changes the target bcrypt hash and
`password_admin_reset_at`, records Audit and commits in the API lifecycle
workflow. It does not change JWT generation or revoke API keys. Moving only the
hashing call would leave the sensitive plaintext, role policy, user locks,
volatile project membership scope, Audit and uncertain-response handling split
between owners.

This decision moves only this administrative password-reset operation into
Identity. Role preview/change, deactivate/delete, offboarding/reactivation,
provisioning and other-user profile writes remain separate workflows. It does
not complete Identity or perform a primary cutover.

## Decision

### Public contract and password authority

Keep the existing public path, UUID validation, 3/minute rate limit, successful
HTTP 200 status and `AdminResetPasswordResponse` with exactly three fields:
`temp_password`, `message` and `target_email`. The successful message remains
`请通过安全渠道告知用户首次登录后立即修改密码`. The temporary password is 16
ASCII characters from `A-Z`, `a-z`, `0-9` and `!@#$%^&*?`, with at least one
uppercase, lowercase, digit and symbol. Do not add fixed-prefix characters.
Hide the password from repr, errors, logs and Audit.

Lock actor and target `User` rows in UUID order using `FOR UPDATE NOWAIT`. Under
those locks, revalidate the active actor, current manager role, current
credential and active target before checking policy or replay. Missing or
inactive targets return 404 `用户不存在`; self-reset returns 400
`不能给自己重置密码（请用「修改密码」）`.

Use the frozen strict rank comparison `target_rank > actor_rank`:

| Platform role                      | Rank |
| ---------------------------------- | ---: |
| `super_admin`                      |    0 |
| `project_admin`                    |    1 |
| `reviewer`                         |    2 |
| `employee`, historical `annotator` |    3 |
| `viewer`                           |    4 |
| unknown historical target role     |   99 |

The current role comes from the locked/fenced transaction, not an earlier
admission snapshot. Project-admin may reset any active employee, including an
unassigned or foreign-project employee. Other lower-ranked targets must belong
to a project the actor owns; otherwise preserve 403
`该用户不在你管理的项目内`. Equal or higher rank preserves 403
`只能重置等级低于你的用户的密码`. The current-manager fence failure is 403
`当前账号已无管理权限`.

Workspace supplies one narrow boolean membership-in-owned-project capability.
After the ordered actor/target User locks, its volatile scope adapter locks
relevant Project rows by UUID before ProjectMember rows, then rechecks owner and
membership while the caller transaction holds the locks. Project transfer and
member removal therefore cannot cross the authorization check. Preserve
historical project and membership participation without new active/archive
filters. Use NOWAIT; contention returns 409
`账号正在更新，请刷新后重试`. This command gets no Labeling access and does
not transfer tasks or work.

### Versioned command, deterministic secret and replay fence

The API adapts the public route to private `POST
/v1/identity/admin-passwords/execute` using `aap-identity-admin-passwords/v1`
and operation `admin_reset_password`. The closed request has exactly
`schema`, `operation`, `operation_id`, `token`, `audit_context` and `payload`;
`payload` has exactly `{target_user_id}`. The caller credential is the actual
current JWT/API-key token, not an actor assertion. `audit_context` keeps the
four bounded SessionAuditContext fields `method/path/ip/request_id`; hide the
token.

Native protocol is `aap-identity-admin-passwords-native/v1`; policy schema and
protocol are `aap-identity-admin-passwords-policy/v1` and
`aap-identity-admin-passwords/v1`. Its request has exactly ten fields:
`schema`, `operation`, `operation_id`, `actor`, `expected_key_hash`,
`request_fingerprint`, `derivation_id`, `audit_context`, `payload` and
`new_password_hash`. `actor` is the existing bound Principal;
`expected_key_hash` exists only for API-key credentials and is an opaque
1–255-character value, not a bcrypt-only field. `new_password_hash` is an exact
bcrypt cost-12 hash and is also secret-bearing. Never send the raw credential
to native SQL.

The master is
`HMAC-SHA256(UTF8(signing_secret), ASCII("aap-identity-admin-passwords/v1"))`;
`derivation_id` is its SHA-256 lowercase hex. The request fingerprint is
HMAC-SHA256 over compact, sorted-key, `ensure_ascii=true` canonical JSON for
`{operation,payload}`. Audit context and credentials are excluded. Each public
invocation gets a new operation UUID; there is no public idempotency header or
automatic transport retry. A trusted private caller that retained the UUID can
confirm an accepted operation after a lost response.

Derive the same 16-character temporary secret from the master and operation
identity. Each block is
`HMAC-SHA256(master, ASCII("aap-identity-admin-passwords/password/v1") + NUL +
actor_uuid_bytes + operation_uuid_bytes + fingerprint_bytes + candidate_u32be +
block_u32be)`. Both counters begin at zero and cannot wrap. Accept stream bytes
below 213 and map modulo the 71-character alphabet; continue blocks until 16
characters are available, then reject the candidate and increment its counter
if any required character class is missing. Do not log vector or runtime
passwords. Bcrypt-12 runs outside SQL locks. On replay a newly salted bcrypt of
the same derived plaintext may be supplied, but Native ignores it when replaying.

The synthetic interoperability vector uses secret
`vector-only-admin-password-secret`, actor
`01234567-89ab-4cde-8123-456789abcdef`, operation
`fedcba98-7654-4321-8fed-cba987654321`, target
`aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`, fingerprint
`2191983825cb1874e18c884b5a2ea064e35fcc5eb256c2f948ed275bb1c1cf67`,
derivation ID
`c04ac69f06e9ef556e7fc70c72da2949bb9fd88f8764dda1029920a2abd9e039` and
temporary password `GLwLvE4ri9v0V!#y`. The secret is a test vector only, not a
deployment credential.

The wire success is exactly `{schema,ok:true,operation,operation_id,principal,data}`;
`data` is exactly `target_user_id`, `target_email`, `temp_password` and aware-UTC
`password_admin_reset_at`. Native success adds `replayed:boolean`; native data
contains only `target_user_id`, `target_email` and `password_admin_reset_at`.
The manager validates Native and wire replies before commit and never returns
the hash. Closed errors preserve Identity authentication codes and the exact
business code/status/detail pairs specified by this contract. Malformed,
unknown, duplicate, extra or alias fields, secret echo and principal/operation
binding failures roll back as unavailable; there is no native-diagnostic leak
or fallback writer.

Migration 0244 creates `aap_identity_admin_passwords_v1` and immutable
`public.identity_admin_password_reset_receipts`. A receipt binds operation
UUID, actor, target, fingerprint and derivation epoch; it freezes target email,
reset timestamp and Audit ID, but contains no live User FK, plaintext password,
copied bcrypt hash, bearer or copied Principal. Password hash, reset marker,
typed Audit and receipt commit atomically. The fixed Audit event is
`user.password_admin_reset`, status 200, target type `user`, target ID the UUID,
and detail exactly `{target_email,target_role}`; it never contains the password.

Every replay reauthenticates the actor, reapplies current target scope and checks
both the target's current `password_admin_reset_at` and an operation-bound
`password_state_fingerprint`. Native computes this private receipt digest as
`hex(SHA256(UTF8("aap-identity-admin-password-state/v1:" + canonical_operation_uuid + ":" + applied_bcrypt_hash)))`; it stores neither the usable bcrypt hash nor the plaintext. A self-change, recovery reset or later administrative reset
supersedes the receipt and returns 409 `operation_conflict`, even if timestamps
collide. Profile or request-context changes alone may replay the frozen email
and timestamp. Identity credentials remain valid: do not bump JWT generation or
revoke API keys. Self password change continues to clear the reset marker.
Do not add User columns or triggers for replay state. Populated receipts block
downgrade; backup/restore with the same signing secret preserves replay, while
a changed derivation epoch conflicts safely.

### Native grants and configuration

Migration 0244 creates schema `aap_identity_admin_passwords_v1`. The
non-inheriting `identity_admin_password_client` LOGIN can execute exactly
`admin_password_policy()` and `execute_admin_password_command(jsonb)`, with
schema USAGE and no relation, column, sequence, DDL, extra inherited or foreign
schema capability. Identity owner receives only the User columns required for
the password/marker update plus the narrow Workspace membership and typed Audit
capabilities. Internal current-actor helpers remain private to Identity.

API and runtime optionally select
`IDENTITY_ADMIN_PASSWORD_DATABASE_URL` or
`IDENTITY_ADMIN_PASSWORD_DATABASE_URL_FILE`; Compose host selector is
`IDENTITY_RUNTIME_ADMIN_PASSWORD_DATABASE_URL_FILE`. A blank Compose selector
disables only this capability, leaves existing Identity startup intact and
makes the private route unavailable (503). An explicitly selected missing,
unavailable or misprivileged DSN fails closed with no writer fallback. Local API
binding and remote Identity runtime use the same portable manager/native command.

### Local qualification

The assembled source revision `540d80726f04a230105825dfac1915e9b4931e41`
passed 42 distinct selected database/process cases with zero skips after
targeted fixture reruns. The set comprises 36 database/restore cases—six native,
three role, five existing reset, 18 existing visibility, and one each for admin
restore, Directory restore, Invitation restore and the legacy challenge—and six
live cases: one installed Node owner, remote and dedicated admin API bindings,
remote and dedicated real-rate-limiter bindings, and one existing dedicated
Directory binding. These are a cross-slice compatibility set, not 42 new
admin-reset-only cases.

Portable source and installed packages each passed 420 checks. The noneditable
Identity package contains 87 files with exact hashes and no application,
SQLAlchemy, Celery, Boto3 or Torch dependency; installed Pydantic is 2.14.1.
Node 22 self-checks passed. The API/architecture suite passed 467 checks with
126 explicitly deferred lazy rules. SDK user/CLI/OpenAPI checks passed 35;
public OpenAPI stayed unchanged. Schema and code generation checks passed, and
the environment documentation covers 450 variables in 48 sections.

The nonroot, read-only Identity image
`sha256:58e97125a477558db72e1f2e70a4c5cef0dd5bd53b08333e25e962ebc0292c9a`
contains the 87 package files and passed runtime/API proof. The API cold image
`sha256:6aa45b1892cec307e334570745846e615d8c9647978d743c023f26d3901676f2`
passed in 38.223 seconds with network disabled, read-only filesystem and a
512 MiB limit. It verified 87 package files and 80 API/native inputs, the reset
route plus existing Directory, Invitation, Recovery, Group and Deactivation
routes, unchanged OpenAPI, migration head 0244, and absence of the obsolete
writer/generator. SQL parameters were hidden and the cold container was removed.

The actual Identity container proof used nonroot, read-only, 1 CPU and 256 MiB.
Separate host-process samples with 12 targets and concurrency four measured
33 attempts/21 busy retries in 2.543434 seconds (p50 1.703565, p95 2.543272),
then 34 attempts/22 retries in 2.640446 seconds (p50 1.708559, p95 2.640287).
These include Node, HTTP and bcrypt overhead; they are not measurements under the container
limits and establish neither production capacity nor an SLO. Confirmation left
12 receipts, 12 Audit rows, JWT generation and API-key rows unchanged.

## Consequences

Positive:

- Password, reset marker, typed Audit and a no-plaintext replay receipt share a
  single transaction and current authority fence.
- The public three-field response and existing credential validity remain
  unchanged; retries can detect a superseded password instead of returning a
  stale secret.
- Project-admin scope is serialized against project transfer and membership
  removal without broad Workspace table access or any Labeling dependency.

Negative:

- Password derivation, bcrypt and the receipt fingerprint become versioned
  protocol details; secret rotation must remain coordinated with outstanding
  receipts.
- This slice is locally qualified across its database, runtime, restore, API
  and image checks. The evidence is local only and does not establish
  production capacity or cutover readiness.
- Platform-role writes, offboarding, other-user hydration, full Identity, the
  ten-module/seven-gate objective, production capacity and a replacement
  language provider remain outside this slice.

## Notes

- The public route is in `apps/api/app/api/v1/users.py`, with HTTP composition
  in `apps/api/app/api/v1/admin_password_commands.py`. Its former reset workflow
  and password generator have been removed from `legacy_user_lifecycle.py`.
- The [Identity private runtime protocol](../../docs-site/api/guides/identity-runtime.md#管理员临时密码重置)
  documents the private wire, role rules and optional DSN.
- Directory remains separately locally qualified under
  [ADR-0123](0123-independent-identity-directory-reads.md). Its historical
  38-case evidence remains separate; the current 42-case selection includes
  the explicitly listed Directory compatibility checks.
- Full module progress is tracked in
  `docs/plans/1792160873_backend-module-decoupling.md`.

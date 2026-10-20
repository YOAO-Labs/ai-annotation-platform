# 0132 — Identity operational provisioning and authority reads

- **Status:** Proposed
- **Date:** 2026-10-21
- **Deciders:** Maintainers
- **Supersedes:** None

## Context

Two mutable operational account paths remain on API-side User ORM writers:
bootstrapping a super-admin for one email and ensuring the exact `admin` import
account. They must preserve established defaults, CLI behavior, audit and
import transactions, including the production-only refusal to create the weak
import account. Their callers need only a UUID, not an ORM User or password
hash. The old invitation-specific create-account helper has no remaining
caller and is removed; invitation account creation belongs to the Invitation
owner.

Read-only project authority also has direct API User queries, although Identity
already exposes safe `read_actor_facts` and transaction-bound lock facts. Reuse
those existing capabilities without creating another identity SQL protocol or
changing lock modes, project/member validation, or caller-visible errors.

## Decision

### Give the two operational writes a small Identity owner

The portable protocol is `aap-identity-provisioning/v1`, implemented by
`provisioning_contract.py` and `provisioning.py`. It has only two operations:
read an account by exact email and create one bootstrap/import account. The
manager returns `UUID | None` for bootstrap (`None` means the email already
exists) and `UUID` for import through
`create_bootstrap_super_admin(*, email, name, password, now, request_id)` and
`ensure_import_admin(*, environment)`. It does not import FastAPI, SQLAlchemy, settings,
network clients or transaction-control code. Password hashes are private input
fields (`repr=False`), are never returned or included in Audit or diagnostics,
and are computed only when creation is required.

`ProvisioningLookupRequest` is the closed `{schema,email}` value.
`ProvisioningCreateRequest` has required `schema`, `mode`, `account_id`,
`email`, `name`, `password_hash`, nullable timezone-aware `now`, and nullable
`request_id`; all fields are required, including the nullable ones. Modes are
exactly `bootstrap_admin` and `import_admin`. Bootstrap requires `now`; import
requires the exact `admin`/`超级管理员` values and null `now`/`request_id`.
The owner adds no field length, normalization or password-strength rule beyond
existing database constraints. `ProvisioningReply` carries `schema`, `email`
and nullable `account_id`; the decoder binds the email and, on creation, the
returned account UUID to the request. Lookup returns a null account ID when
absent. Invalid, extra or malformed replies fail closed as unavailable. The
canonical `provisioning_protocol.schema.json` is additive; older protocol
artifacts, including `account_lock_protocol`, remain byte-for-byte unchanged.

`AccountProvisioningData` implements the closed lookup/create data port on the
same caller `AsyncSession` and outer transaction as each existing CLI workflow.
Lookup respects `autoflush` and `no_autoflush`. Creation explicitly flushes
caller-pending ORM work even inside `no_autoflush`, then invokes the Identity
owner; it does not commit, roll back, retry, create a savepoint or clear
unrelated pending state. A malformed or binding-invalid reply maps to the
existing unavailable 503 without ORM fallback; SQLAlchemy/PostgreSQL failures
propagate unchanged. The importer remains responsible for committing all
imported rows or rolling them back together with any newly provisioned account.
The public API and CLI outputs keep their existing shape, and consumers receive
only UUIDs.

### Preserve per-email bootstrap behavior

Bootstrap means “create a super-admin for this email if absent,” not “create
the only account in an empty roster.” Multiple different bootstrap emails may
therefore create multiple super-admins. Existing exact-email rows are returned
unchanged regardless of role, active status, name or password hash; the owner
does not hash a password or write Audit for that path. Only the CLI retains its
existing email lowercase/trim and name trim; the owner adds no normalization,
length rule or password-strength policy.

When absent, the owner creates a `super_admin` account with `status=online`,
active true, null group fields, caller-supplied `email_verified_at`, generation
zero and empty preferences. Other nullable lifecycle fields remain null and
`created_at` uses database transaction time. Account creation and the fixed
`system.bootstrap_admin` Audit are atomic. The Audit actor is the new account
itself; it records actor email/role, `target_type=user`, the UUID text target and
`detail={email}`. Method/path/status/IP are null, request ID comes from
`request_id_var` or is null, and Audit time uses the database default. The
application wrapper only returns its previous boolean result and never writes a
second Audit.

No conflict handler is added. A real concurrent same-email insert still raises
raw PostgreSQL `23505` and aborts the caller transaction; there is no
`ON CONFLICT`, implicit retry, savepoint or adapter rollback.

### Preserve the exact import guard and defaults

Import lookup uses the exact email `admin`. Any existing row is reused
unchanged, including inactive, historical or non-admin accounts. If it is
missing and `environment == "production"` exactly, the owner raises the existing
`RuntimeError` before hashing or creating:

```text
生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。请先手动创建管理员账号再运行本导入脚本。
```

Other environments, including staging, keep the existing creation behavior:
email `admin`, name `超级管理员`, bcrypt of `123456`, role `super_admin`, active
true, status `offline`, and null `email_verified_at`; import provisioning emits
no Audit. No new environment normalization or generic “production-like” guard
is introduced.

### Use narrow SQL functions and existing authority capabilities

Migration 0252 follows 0251. It installs
`0252_identity_provisioning_support.sql` first, then
`0252_identity_provisioning.sql`; the exported snapshot is
`0252_identity_provisioning.schema.json`. It adds the read-only,
`STABLE` `aap_identity_v1.read_provisioning_account(text)` and the mutable,
`VOLATILE` `aap_identity_v1.create_provisioning_account(jsonb)` functions. Both
use a fixed safe `search_path`, `SECURITY DEFINER`, and revoked PUBLIC execution.
The read is exact-email only, without locks or DML. Create validates the closed
request and raises `22023 invalid_request` for invalid schema/operation/value,
writes the old explicit defaults, calls the Audit owner only for bootstrap, and
returns only the bound UUID. SQL
constraint, privilege and duplicate errors propagate and abort the caller
transaction. There is no provisioning receipt, new table or direct
Identity-to-Audit table write.

The narrowly precreated `identity_provisioning_client` LOGIN gets only schema
usage and these two function grants, with no User/Audit table or column access,
AUTH/session/key/FENCE functions, DDL, TEMP or role membership. Identity calls
the fixed Audit capability
`aap_audit_v1.record_identity_bootstrap_admin(uuid,text,text)`; the Audit owner
records only the fixed bootstrap event, never an arbitrary action or target.
Existing AUTH, credential FENCE and workflow `append_client` allowlists are
unchanged. Local compatibility and the LAN application role get explicit
allowlist grants only; no production application is made. This adds no HTTP endpoint, runtime
setting, DSN selector or dependency. `AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL`
is only a test-verifier override, not runtime configuration.

For authority checks, nonlocked `project_access.resolve_project_access` reuses
the existing `IdentityAccountLockData.read_actor_status` backed by
`read_actor_facts`; it returns the local frozen `IdentityActorStatus` value
(`id`, raw `role`, `is_active`) without a new query or protocol. It preserves
account → Project refresh → membership order, Project ORM refresh, and exact
401/Bearer/403/404 behavior without adding locks.
`task_lock`, `batch` and `async_job_access` use the existing account-status and
three lock-mode functions where their current checks require them. Lock mode,
wait/NOWAIT behavior, exceptions, null behavior and policy mapping remain
unchanged. Active-only guards remain active-only and do not gain role checks.
This does not broaden account, member or task ownership, and does not add SQL,
protocol or network surfaces for authority reads.

## Consequences

Positive:

- Identity owns mutable User DML and the fixed bootstrap Audit while callers
  keep their existing transaction and receive only UUIDs.
- Bootstrap and import behavior stay compatible without a caller ORM mirror or
  duplicate application Audit.
- Authority consumers reuse already qualified Identity status/lock functions
  with their existing transaction and error boundaries.

Negative:

- The provisioning adapter must explicitly flush pending ORM work before create
  even under `no_autoflush`; readers must not force that flush.
- Import rollback remains coupled to Assets/Workspace/Labeling writes in the
  caller transaction.
- Unlocked Workspace reads and mutable bootstrap/import writers are still in
  the migration boundary until all consumers use the owner.

## Alternatives Considered

**Keep the API service as the User writer.** This leaves account creation
mutable in the caller domain, returns ORM/password internals, and duplicates
Identity ownership.

**Use a generic registration endpoint for bootstrap/import.** These are
operational commands with fixed legacy defaults and transaction owners, not
public registration flows; routing them through the public/auth flow would
change policy, audit and rollback behavior.

**Create a new authority read/lock capability.** The existing status read and
0251 account-lock functions already provide the needed values and locking modes.
Adding another operation would duplicate policy and could alter lock behavior.

## Notes

- Portable protocol/manager: `apps/identity/src/aap_identity/{provisioning_contract,provisioning}.py`.
- Caller adapter: `apps/api/app/services/identity_provisioning_data.py` and
  `IdentityCommandService`.
- Native migration/artifact/grants: migration `0252`,
  `0252_identity_provisioning.schema.json`,
  `infra/docker/identity-provisioning-grants.sql` and the existing owner/LAN
  grant sources.
- This slice's local qualification is pending Root evidence. The previous
  account-lock 231-case result remains closed and is not a provisioning count.
  The full Identity and ten-module/seven-gate objectives remain unfinished; no
  production or primary cutover is claimed.

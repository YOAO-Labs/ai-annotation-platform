# 0137 — Workspace Template Query Owner

- **Status:** Accepted; bounded local qualification passed
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

The six configuration commands have bounded local qualification. Template
list/detail and postcommit response hydration previously imported API ORM models.
Moving only writes does not make the complete Workspace module independently
deployable or replaceable in another language.

The old reads have observable phase boundaries. Non-super-admin lists first
read all non-deleted organization memberships, even when filtering public or
private scope. Detail loads the template before checking visibility. Only a
non-super-admin organization-scoped detail reads organization membership facts.
Creator names are fetched after visibility, once per result row. Configuration
responses commit before loading the current row and do not recheck visibility.

## Decision

Extend the portable Workspace owner with closed `aap-workspace-template-query/v1`
operations for list, detail and configuration response projection. The API keeps
public query admission, authenticated actor binding, command commit and existing
`ProjectTemplateOut` response validation. The public template callers now use
the owner without importing the read ORM models; the retired template service
has been removed and its named-pipeline callers use the typed Identity fact.

The API is the trusted authentication broker. It passes a closed admitted actor
fact containing ID and role from Identity authentication. The owner computes
visibility; it does not accept visibility or organization-membership booleans.
It preserves the admitted request snapshot rather than introducing another
active-account/role lookup. The private control bearer identifies the broker,
not an end user. Independently verifying an actor outside this trust boundary
would require an Identity admission assertion and is a different contract.

Template list/detail retain their current organization read phases, filters,
`ILIKE` wildcard behavior and `created_at DESC` ordering without a new tie key.
The query keeps one creator-name read per returned template, including repeated
creator IDs. Existing pure template visibility policy remains authoritative.
No pagination, result cap, profile batching, write or lock is added here.

Postcommit projection is a separate internal operation bound to the successful
configuration receipt's operation UUID, operation, actor and result resource.
It reads the current row without a new visibility check. Missing rows retain
the API's internal500 path. This is a broker response-readback capability,
not a new public read endpoint or authorization grant for end users.

Identity provides a narrow organization-ID fact from `organization_members`
where `deleted_at IS NULL`. It does not join Organization or inspect membership
role/account activity. The existing Workspace account projection supplies only
raw creator names; the query does not apply the richer UserBrief validators.
Workspace receives no Identity table grants.

The fixed25 physical template columns travel in JSON text inside a strict
envelope. This avoids imposing the envelope's small depth limit on stored JSON.
The three floating columns use PostgreSQL text with function-local
`extra_float_digits=3` so precision, negative zero and nonfinite values survive.
Timestamp JSON uses UTC within the function. Both caller settings are restored;
timestamp infinities materialize as the legacy naive
datetime extrema. Raw JSONB nulls and other stored values reach the existing
public serializer without early business-DTO validation.

The Native data boundary has a policy RPC and a closed step RPC for
organization facts, template rows, creator names and receipt-bound projection.
The dedicated client has exactly those function privileges. Default API
binding borrows the caller transaction and preserves autoflush/no-autoflush;
standalone DSN/HTTP bindings own a read transaction. Explicit remote failures
must not fall back to another reader. There are no query operation receipts or
business writes of their own.

## Consequences

The same portable policy can serve the API and an independent process without
API/ORM dependencies. Typed Identity facts preserve foreign ownership. Keeping
raw rows until public validation retains committed JSONB-null response failures.

The broker remains responsible for the authenticity of admitted actor facts.
Remote adapters cannot see unrelated pending work on the API connection, and
the module still uses a shared PostgreSQL capability boundary. Cross-database
or cross-service snapshots are not claimed. A new language provider must satisfy
the same protocol and behavior gates; Go is not required for this owner.

## Alternatives Considered

- Reusing UserBrief would add email/role/avatar validation to a name-only read.
- One combined query or profile batching would alter existing read phases.
- Re-reading the current actor would change the admitted role snapshot.
- Converting complete rows directly to native JSON would lose negative zero
  and expose session-zone timestamp formatting.

## Notes

The frozen pre-port fixture records40 real HTTP observations, two caller
autoflush probes and a timezone probe. A separate real PostgreSQL scalar probe
records numeric/timestamp transport differences. All capture resources were
cleaned. The portable source suite passed169 cases, including37 new query cases.
The Identity-only fact passed scoped native behavior/owner/grant checks after
correcting the test's internal-character metadata cast.

Default borrowed, dedicated DSN and independent HTTP paths each match the40
frozen HTTP observations and the timezone probe. The borrowed path also matches
both caller autoflush probes. Four Native cases and three data-binding cases
passed. An additional focused caller-setting probe exposed float rounding under
`extra_float_digits=0`; after the local encoding fix both caller cases pass and
the original setting remains unchanged.

The affected public API suite passes43 cases and transport checks pass11.
The configuration-response regression still matches106 frozen HTTP observations
and17 borrowed caller records across its three capture groups. Architecture
checks pass328 cases with138 inapplicable parameter cases skipped. The public
OpenAPI snapshot is unchanged. Final read-only integration review found no
additional actionable issue. These groups overlap and are not added together.

Twelve actual process/recovery cases pass without skips at0259: query-only and
configuration/query coexistence, private authentication and malformed requests,
grant-loss503 and recovery, restart and unavailable-owner failure without local
fallback, query/receipt projection after backup restore, plus nine affected
protected restores. Their disposable resources were removed without errors.

Full migration validation passes at0259: fresh installation,173 reversible
downgrades to base,85 suffix downgrades to0174, forward migration and backup
restore. All four derived databases were removed without cleanup errors.

Noneditable installed suites pass169 Workspace and27 Inference cases outside
the source tree. Their29/7 package files plus14 shared dependency files match
source. No API, SQLAlchemy, Celery, Boto3 or Torch package is installed. The first
attempt exposed a test's hardcoded source path; using package resources makes
the same schema comparison valid in both environments.

The owner image matches all50 runtime/schema files. Offline and real HTTP/public
API checks pass as UID10004, read-only, all capabilities dropped, no new
privileges,64 PIDs,1 CPU,256 MiB and32 MiB tmpfs, with exactly two read-only
secret mounts. Public creation commits through a real request session before
remote receipt projection. Detail and post-restart reads match. Initial test
attempts exposed a missing required payload field and an external SAVEPOINT
fixture that hid commits and blocked cleanup; those attempts are retained, and
the corrected committed-session qualification passes. Containers, secrets and
disposable database resources were removed without cleanup errors.

A bounded local profile completes16 detail reads at concurrency4 with16
successes, no failures and no OOM: elapsed403 ms, p50 99 ms and nearest-rank
p95 108 ms. PostgreSQL and the caller are outside the container budget. This is
a local fixture observation, not production capacity or an SLO.

The offline API image matches172 installed package files and139 application/
Native inputs, the unchanged public OpenAPI and migration head0259. Its cold
proof uses UID10001, network none, a read-only filesystem,1 CPU and512 MiB.
The container was removed. Runtime code is qualified at `5136a448b314a583846751098a13762e158f42fe`;
the corrected container test and API image use `6f0f44cebaa383e0dc59b33b2fef74a3aa840e90`.
The intervening commits change only acceptance tests, with runtime hashes equal.

Owner image: `sha256:0f5604c68b995d1e5da474bdfab9ac0e54bdfe4ab2b0aa9f23c70dd5f74d4eea`.
API image: `sha256:aa082f7ce387a7df96822962235c77eab327fa26f4f047d3287d51fe3129b350`.

This closes the bounded local template-query slice. Project reads, mentions,
guide assets, class rename, named pipelines, import creation and adjacent
writers remain separate work. Full Workspace, other modules, complete provider
language replacement and independent databases remain unfinished. Primary source,
shared links and its four original running containers are preserved. There is
no primary/production cutover, push, release or remote CI result.

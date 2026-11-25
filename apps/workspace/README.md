# yoao-workspace

`yoao-workspace` contains portable policy and contracts for project membership,
owner transfer, project and template configuration, and template, project and
directory queries.
The core uses Pydantic and shared Workspace, Inference, task, Audit, and
owner-data contracts. Saved pipeline structure and normalization come from
`yoao-sdk` (`yoao_inference_contract`); the core and conformance image do not install the
Inference provider. The PostgreSQL extra includes the HTTP client used for the
credential-owning Inference probe.

The product API runs all six Workspace paths in process with the same managers
as the portable package. It passes its existing `AsyncSession` to each SQL data
adapter; request authorization, commit, rollback, and public responses remain
with the original caller. The API uses its ordinary application database role,
not a per-operation Workspace URL, DSN, or bearer. Frozen PostgreSQL functions
and the package contracts stay in place. See the [runtime guide](../../docs-site/api/guides/workspace-runtime.md#workspace-deployment)
for the default deployment and the separately planned migration of old split installations.

The manager accepts a small data port that owns transaction boundaries and
executes the Workspace native prepare/apply capabilities. It contains no API,
ORM, task-runner, or storage integration. Role-change snapshots preserve the
existing JSON digest format so a preview can be checked again before a handoff.

The `postgres`, `runtime`, `client`, and `test` extras remain for portable SQL,
HTTP, and package conformance. Standalone HTTP or Node providers are run in
isolated conformance fixtures, not as services for the product API. Use only
the existing function-scoped test roles and never put an owner or migrator DSN
in the API environment. See [module boundary acceptance](../../docs-site/dev/testing.md#module-boundary-acceptance)
for the supported checks.

Owner transfer uses `aap-workspace-owner-transfer/v1`. The caller-session
adapter never commits; the standalone conformance adapter owns its transaction
under the separate two-RPC credential. Matching immutable receipts allow a lost
reply to be retried without reapplying an old transfer. The optional
[Node conformance provider](conformance/node/README.md) tests the owner-transfer
transport against the same Native business policy.

Project/template configuration values and pure kind, clone and authorization
policy also live here. The API directly consumes those models and policy; its
configuration calls use the portable manager and fixed Native writes. The
four initial Native cross-owner configuration dependencies are complemented by
phased facts and a six-command owner under `aap-workspace-configuration/v1`.
Its fixed step capability and immutable receipts still run in the original
caller transaction. Class rename preserves the same-transaction Labeling write;
Guide commands leave byte I/O to the existing managed storage adapter. The old
configuration ORM writers are removed. ADR-0136 records the earlier bounded
package, restricted database, process, container, restart, restore and concurrency
qualification; it does not qualify a new host arrangement. Current scope is
tracked in the [module ledger](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账).

Template list/detail and receipt-bound response projection now have a portable
query contract, policy manager and raw scalar materializer. The API borrows
the caller's transaction, retaining autoflush and response projection after the
original configuration commit. ADR-0137 records the frozen 40-response baseline
and the earlier package, restricted SQL, process, restore and migration evidence.
Its container sample does not establish production capacity.

Project access values and capability policy live in the shared
`yoao_workspace_contract.access` module, including the membership compatibility
rule. Workspace, API, service and worker consumers import this owner directly;
current SQL authority resolution and locks remain with their existing adapters.
Fifteen typed foreign read capabilities support the Project query owner. See
ADR-0138 for the frozen baseline and remaining integration boundaries.

uv build-cache keys include package Python/schema sources and README metadata,
so source-only changes also invalidate a noneditable cached wheel. Independent
artifact verification compares installed files against the source manifest.
Hatchling packages the current source directory directly, preventing an old
setuptools staging directory from retaining removed modules in a later wheel.

Project query requests, typed read facts, raw value codecs, and row authority
phases live in the shared `yoao_workspace_contract` package. Workspace retains
the query manager, data/SQL adapters, and portable runtime bindings. Project
and directory reads in the API borrow the same session. Display data still
comes from the existing Identity UserBrief adapter, not from direct User-table
reads; empty results and reads that do not need briefs skip that dependency.
ADR-0139 records authority phases, the 50 frozen HTTP observations, and earlier
restricted package, process, restore and migration evidence. That split-runtime
evidence is not a deployment recommendation or a production capacity claim.

Configuration managers no longer read backend URL, auth, token, or
`extra_params`, or send HTTP `/setup` requests directly. The backend fact is
exactly `{id, probe_subject}`; the injected
Inference probe returns only modalities. The host calls the retained `inference`
service using `INFERENCE_BACKEND_PROBE_URL`,
`INFERENCE_BACKEND_PROBE_TOKEN(_FILE)`, and `INFERENCE_BACKEND_PROBE_TIMEOUT_SECONDS`.
The `WORKSPACE_INFERENCE_PROBE_` settings are only for standalone conformance
and are not read by the host API. There is no credential fallback.
A missing provider is unavailable only when a modality probe is needed. Backend
setup failure keeps legacy fail-open behavior; internal provider failure aborts,
and legacy materialization failure retains generic 500/rollback. See ADR-0149.

Adjacent import and point-cloud quality writes use three fixed SQL capabilities
in `yoao_workspace_v1`, described by `aap-workspace-project-maintenance/v1`.
The nuScenes caller retains its stable display ID, ORM creation defaults and
outer Assets/Labeling/calibration transaction, consuming only a Project UUID.
Point-quality promotion keeps its caller-held Project lock, evaluation policy
and Audit transaction; the native write synchronizes the API identity map without
another ORM mutation. Neither path commits or rolls back the caller, selects an
independent DSN, retries a unique conflict or adds authorization/CAS policy.

The dedicated `yoao_workspace_project_maintenance_client` LOGIN has exactly these
three application-owned Native capabilities, with no table/column, sequence,
DDL or TEMP access. Standard PostgreSQL/public invoker helpers are outside that
count. Existing task-creation/configuration capability roles are unchanged.
The portable descriptor and migration snapshot are generated together. Source,
restricted Native, actual API/import, Node and transaction/lock checks pass;
complete-module artifact/restore qualification remains a coherent milestone.
See [ADR-0164](../../docs/adr/0164-workspace-transactional-project-maintenance.md).

Project deletion retains the existing `aap-project-delete/v1` Native coordinator
and its caller-owned transaction. `ProjectDeletionManager` owns the portable
command/receipt validation; the API supplies its borrowed SQLAlchemy session.
`ProjectDeletionPostgresData(connection)` borrows an already-active asyncpg
transaction and checks the exact deletion-only LOGIN before mutation. It never
connects, commits, rolls back, retries or closes the caller connection. The command
keeps the original four fields and SQL NULL Audit context; legacy API imports
remain compatibility aliases. The package explicitly depends on the existing
shared Audit contract.

The native boundary passes focused Python/Node/public-API, permission, failed-
commit and replay checks. A retirement gate now protects canonical Jobs before
cascade; legacy no-proof recovery and complete Workspace qualification remain
open.
See [ADR-0165](../../docs/adr/0165-workspace-caller-transaction-project-deletion.md)
and the one module ledger for current scope.

Dataset unlink uses the existing unlink/preview Native capabilities through
`DatasetUnlinkManager` and portable command/receipt values. The API retains its
borrowed SQLAlchemy session, explicit write flush, cache invalidation and commit.
`DatasetUnlinkPostgresData(connection)` requires an already-active asyncpg
transaction and checks the exact two-function `yoao_workspace_dataset_unlink_client`
credential. It never opens/closes connections or begins/commits/rolls back/retries
transactions. Preview locks current authority, so neither operation belongs in a
READ ONLY transaction.

The package directly reuses the existing lightweight Assets CleanupBucket
contract; installing the core package does not request its worker extras.
Python UUID/tuple values retain the original validation behavior; raw JSON uses
strict duplicate-key/non-finite rejection. Node conformance forwards original
JSON text to preserve receipt integers above JavaScript's safe range.
Function-only calls, actual API, rollback, failed commit and replay qualify this
local calling boundary. Both destructive coordinators now require current-run
retirement proof or a locked never-started fact. Jobs retains private unstarted
clearances so later deletion can distinguish safe missing Jobs from unknown old
orphans. Started legacy/no-proof records stay409 pending reliable drain/repair;
full module credentials/artifacts/restore remain separate. See
[ADR-0178](../../docs/adr/0178-video-retirement-deletion-fences.md).
See [ADR-0177](../../docs/adr/0177-workspace-caller-transaction-dataset-unlink.md)
and the single module ledger.

Project and directory response decoders live with their value codecs; request,
fact and schema definitions do not import those codecs. The API and portable
managers consume the codec entrypoints directly, without compatibility exports.
The architecture guard checks the Workspace owner and shared contract together,
including function-local imports. Wire schemas and materialization semantics
remain unchanged.

Ordinary API/worker LOGINs can apply the explicit Workspace consumer profile after
owner and legacy application grants. It retains 32 fixed functions and the two
contracted facts views while removing private relation/column, receipt, sequence,
helper and schema-CREATE access. Broad default grants on future public tables and
sequences are removed for that consumer. Labeling counter facts and unrelated
current privileges remain. Trusted seed operators retain a separate profile;
the migrator may retain its existing inheritance of the ordinary runtime role.
Thirteen local cases qualify this credential boundary, including actual HTTP,
Node and worker-service rollback. Eight existing module-role cases also pass;
legacy API grants retain the existing eight cleanup-worker capabilities. Complete-module cold artifacts/assembly/restore
remain separate. See ADR-0179 and the single module ledger.

Default embedded LAN/worktree credentials now share a private-storage ACL:
private relations/columns/partition descendants, receipts, related sequences,
schema CREATE and broad owner defaults are fenced. Only the two facts views
remain readable, plus the existing template fixture view in test/E2E databases.
Effective inherited/owned access rejects the grant transaction. LAN applies this
fence before checking the cleanup account's effective privileges.
Existing fixed import/seed/scale/maintenance functions and test-only fixture
functions remain supported; this shared runtime is not the strict 32-function
consumer. The default storage boundary has separate local qualification in the
single module ledger; no daily or independent-topology cutover is implied.

In the default profile, guide allocation, exact mounted-file lookups, clone
snapshots and retirement use fixed Workspace capabilities. The API still
resolves the saved storage target and checks current authority before and after
physical I/O. Optional guide uploads do not expand the strict 32-function
profile; a missing allocation capability fails before creating a pending row
or uploading bytes.

The portable conformance image checks its configured `/ready` endpoint. This
checks the enabled SQL capabilities, not the lazy Inference or Identity
dependencies. There is no Workspace runtime readiness endpoint to deploy for
the host API.

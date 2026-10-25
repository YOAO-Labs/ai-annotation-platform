# aap-workspace

`aap-workspace` contains portable contracts and policy for project membership
creation, role previews, role changes, member removal, project owner transfer,
and six project/template configuration commands. The core package
uses Pydantic and shared Workspace, Inference, task, Audit and owner-data contracts.
Saved pipeline structure and normalization come from `aap-inference-contract`;
the core and standalone image do not install the Inference provider. Runtime and PostgreSQL bindings are optional;
the PostgreSQL extra includes HTTP dependencies for the credential-owning Inference probe.

The manager accepts a small data port that owns transaction boundaries and
executes the Workspace native prepare/apply capabilities. It contains no API,
ORM, task-runner, or storage integration. Role-change snapshots preserve the
existing JSON digest format so a preview can be checked again before a handoff.

Install `aap-workspace[postgres]` for the PostgreSQL client layer,
`aap-workspace[runtime]` for a standalone service, or
`aap-workspace[client]` for HTTP clients. Test dependencies are available via
`aap-workspace[test]`.

Owner transfer uses `aap-workspace-owner-transfer/v1` and a separate two-RPC
function-only credential. The caller-session adapter never commits; standalone
adapters own their transaction. Matching immutable receipts allow a lost reply
to be retried without reapplying an old transfer. The runtime can enable
membership, owner transfer, or both, with readiness checking every enabled
capability. The optional Node conformance provider under `conformance/node`
is a local transport replacement experiment; Native still owns business policy.

Project/template configuration values and pure kind, clone and authorization
policy also live here. The API directly consumes those models and policy; its
configuration calls use the portable manager and fixed Native writes. The
four initial Native cross-owner configuration dependencies are complemented by
phased facts and a six-command owner under `aap-workspace-configuration/v1`.
Its fixed step capability uses function-only credentials and one transaction;
the standalone HTTP runtime can enable configuration without membership or
owner transfer. Public API callers use the same owner with a borrowed session
by default; the old configuration ORM writers are removed. These six commands
passed bounded local package, restricted database, process, container, restart,
restore and concurrency qualification. See ADR-0136 for evidence and scope.
Current query and maintenance capabilities are described below; complete-module
status is maintained in the [module ledger](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账).

Template list/detail and receipt-bound response projection now have a portable
query contract, policy manager and raw scalar materializer. Public callers now
select the same owner with borrowed, dedicated DSN or HTTP bindings. All three
match the frozen40 HTTP observations; default caller/GUC probes pass. Query-only
runtime startup and the function-only read credential are verified. Installed
packages, restricted owner/API images, real container reads and response
projection, fault recovery, backup restore and full migration qualification pass.
The bounded local concurrency sample is not a production capacity claim;
see ADR-0137 for scope.

Project access values and capability policy live in the shared
`aap_workspace_contract.access` module, including the membership compatibility
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
phases live in the shared `aap_workspace_contract` package. Workspace retains
the query manager, data/SQL adapters, and runtime bindings. API callers select
borrowed, dedicated PostgreSQL or HTTP bindings. Native0261 and its
function-only credential pass owned database checks; the standalone runtime
resolves Identity Briefs through its explicit public client dependency. Fifty
frozen HTTP observations match all three bindings; noneditable package
manifests and bounded process-failure/restart checks pass. Restricted owner/API
images, real container response projection and restart, backup/restore and full
migration261 pass. The bounded local concurrency sample does not establish
production capacity. Remaining complete-module scope is maintained in the
module ledger rather than inferred from this query qualification.
See ADR-0139 for authority phases and the current acceptance boundary.

Configuration managers no longer read backend URL/auth/token/extra_params or execute
setup HTTP directly. The backend fact is exactly `{id, probe_subject}`; the injected
Inference probe returns only modalities. Standalone configuration uses
`WORKSPACE_INFERENCE_PROBE_URL`, `WORKSPACE_INFERENCE_PROBE_TOKEN(_FILE)` and
`WORKSPACE_INFERENCE_PROBE_TIMEOUT_SECONDS`. It has no credential fallback; missing
provider is unavailable only when a backend modality probe is needed. Backend
setup failure keeps legacy fail-open behavior; internal provider failure aborts,
and legacy materialization failure retains generic500/rollback. See ADR-0149.

Adjacent import and point-cloud quality writes use three fixed SQL capabilities
in `aap_workspace_v1`, described by `aap-workspace-project-maintenance/v1`.
The nuScenes caller retains its stable display ID, ORM creation defaults and
outer Assets/Labeling/calibration transaction, consuming only a Project UUID.
Point-quality promotion keeps its caller-held Project lock, evaluation policy
and Audit transaction; the native write synchronizes the API identity map without
another ORM mutation. Neither path commits or rolls back the caller, selects an
independent DSN, retries a unique conflict or adds authorization/CAS policy.

The dedicated `workspace_project_maintenance_client` LOGIN has exactly these
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
transaction and checks the exact two-function `workspace_dataset_unlink_client`
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

Ordinary API/worker LOGINs can apply the explicit Workspace consumer profile after
owner and legacy application grants. It retains 32 fixed functions and the two
contracted facts views while removing private relation/column, receipt, sequence,
helper and schema-CREATE access. Broad default grants on future public tables and
sequences are removed for that consumer. Labeling counter facts and unrelated
current privileges remain. Seed/screenshot operators retain a separate profile;
the migrator may retain its existing inheritance of the ordinary runtime role.
Thirteen local cases qualify this credential boundary, including actual HTTP,
Node and worker-service rollback. Eight existing module-role cases also pass;
legacy API grants retain the existing eight cleanup-worker capabilities. Complete-module cold artifacts/assembly/restore
remain separate. See ADR-0179 and the single module ledger.

The image health probe follows the existing `WORKSPACE_PORT` setting (default
8172), including owner transfer on8173 and configuration on8174. It checks the
selected service's `/ready` endpoint; lazy external dependencies still need their
actual operation acceptance.

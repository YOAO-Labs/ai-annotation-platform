# aap-workspace

`aap-workspace` contains portable contracts and policy for project membership
creation, role previews, role changes, member removal, project owner transfer,
and six project/template configuration commands. The core package
uses Pydantic, the shared Workspace projection contract and Inference's portable
configuration validation. Inference transitively imports shared protocol FastAPI
error helpers and lifecycle crypto. Runtime and PostgreSQL bindings are optional;
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

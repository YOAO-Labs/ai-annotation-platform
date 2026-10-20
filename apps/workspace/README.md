# aap-workspace

`aap-workspace` contains portable contracts and policy for project membership
creation, role previews, role changes, member removal, project owner transfer,
and six project/template configuration commands. The core package
uses Pydantic, the shared Workspace projection contract and Inference's portable
configuration validation. Inference transitively imports shared protocol FastAPI
error helpers and lifecycle crypto. Runtime and PostgreSQL bindings are optional;
the PostgreSQL extra includes HTTP dependencies for existing backend setup probes.

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
API-side read projections, adjacent writers and the full Workspace module remain open.

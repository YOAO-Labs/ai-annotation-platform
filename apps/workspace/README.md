# aap-workspace

`aap-workspace` contains portable contracts and policy for project membership
creation, role previews, role changes, member removal, and project owner transfer. The core package
uses Pydantic and the shared Workspace projection contract; runtime, PostgreSQL,
and HTTP dependencies are optional.

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
configuration writes still use the existing caller-owned ORM transaction. The
four Native cross-owner configuration dependencies do not add a standalone
configuration command or runtime selector. See ADR-0135 for this boundary and
the remaining owner extraction.

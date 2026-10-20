# aap-workspace

`aap-workspace` contains portable contracts and policy for project membership
creation, role previews, role changes, and member removal. The core package
uses Pydantic only; runtime, PostgreSQL, and HTTP dependencies are optional.

The manager accepts a small data port that owns transaction boundaries and
executes the Workspace native prepare/apply capabilities. It contains no API,
ORM, task-runner, or storage integration. Role-change snapshots preserve the
existing JSON digest format so a preview can be checked again before a handoff.

Install `aap-workspace[postgres]` for the PostgreSQL client layer,
`aap-workspace[runtime]` for a standalone service, or
`aap-workspace[client]` for HTTP clients. Test dependencies are available via
`aap-workspace[test]`.

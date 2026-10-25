# Owner-transfer transport conformance provider

This private package is a local single-command language-replacement experiment.
It uses `pg` directly over PostgreSQL TCP and does not import or proxy Python.
Native SQL owns business authorization, locks, the owner write and receipts.
It does not replace the complete Workspace module or select a production language.

Run `pnpm install --ignore-workspace --frozen-lockfile --ignore-scripts` in this directory, then `pnpm run check`. Start with
`node provider.mjs`; supply `WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE` and
`WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE`. The DSN must use the exact-two-function
`workspace_owner_transfer_client` LOGIN. `WORKSPACE_HOST` defaults to loopback
and `WORKSPACE_PORT` to 8173. The Dockerfile is built from the repository root.

`GET /health` reports liveness. `GET /ready` checks policy and actual grants.
`POST /v1/workspace/owner-transfer/execute` requires the private bearer and the
closed `aap-workspace-owner-transfer/v1` request. Policy and reply validation run
in the same transaction as the command. Business refusals roll back; successful
responses are sent only after commit. Untagged data failures are HTTP 500,
unavailable or malformed owner replies are 503, and tagged lock conflicts retain
the frozen failure envelope. No diagnostics or credentials are returned.

For owned tests only, `AAP_WORKSPACE_OWNER_TRANSFER_DROP_OPERATION_ID` drops the
first successful reply for the selected UUID after commit. Restarting the
provider and retrying that UUID exercises Native receipt recovery. Keep this
fault-injection setting unset outside that explicit local test.

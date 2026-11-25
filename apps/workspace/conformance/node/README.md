# Owner-transfer transport conformance provider

This private package is a local single-command language-replacement experiment.
It uses `pg` directly over PostgreSQL TCP and does not import or proxy Python.
Native SQL owns business authorization, locks, the owner write and receipts.
This is package conformance only, not a product provider or a deployment
option for the API.

Run `pnpm install --ignore-workspace --frozen-lockfile --ignore-scripts` in this
directory, then `pnpm run check`. To start `node provider.mjs` against a disposable
conformance database, supply `WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE` and
`WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE` only to the fixture process. The DSN
must use the exact-two-function `yoao_workspace_owner_transfer_client` LOGIN
from the [owner-transfer grant script](../../../../infra/docker/workspace-grants.sql) (`-v grant_owner_transfer=1`).
`WORKSPACE_HOST` defaults to loopback and `WORKSPACE_PORT` to 8173. The Dockerfile
is built from the repository root. Never use production or daily credentials.

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

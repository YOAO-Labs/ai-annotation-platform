# Identity

This package contains the JWT/API-key verifier, API-key management, core sessions
and self-account command owners, their closed contracts, and one private HTTP process.
The runtime has no application, SQLAlchemy, Celery or object-store dependency.
Admission uses four PostgreSQL capabilities, key management uses two, and
sessions use three plus a scoped Redis login-counter identity. Each configured
capability has a separate database login. Self-account commands use three functions
and presence maintenance uses one separate operator function. Registration,
recovery/email, group/admin and multi-owner lifecycle coordinators remain in the
API, so this is not yet a complete Identity service.

## Run

Install the current migration head and the exact Identity owner/runtime grants
for the enabled capabilities first.
Provide exactly one direct value or secret file for each of
`IDENTITY_DATABASE_URL`, `IDENTITY_TOKEN` and `IDENTITY_SIGNING_SECRET` (file
variants append `_FILE`). The DSN uses `postgresql://` and the signing secret
matches the API's `SECRET_KEY`; configuration errors never fall back. The
runtime does not load an API `.env` file.

```sh
uv run --project apps/identity --locked --extra runtime python -m aap_identity_runtime
```

`/health` is process liveness; `/ready` calls and checks the native policy.
Private `POST /v1/identity/authenticate` requires the control bearer and a JSON
`{"token":"..."}` body. Valid credential verdicts return a closed success or
failure envelope with HTTP 200. Invalid control credentials return 401, malformed
requests return 400, and owner failures/deadlines return 503. Bodies never echo
the supplied credential. Concurrency and deadlines are bounded. Account preferences
retain their existing accepted size/cardinality set rather than inheriting the
smaller admission/session byte limits.

`docker-compose.identity.yml` starts only this optional process. Its nonroot
image contains installed packages, not an application source bind, and supports
a read-only root filesystem. Session commands require their own database DSN
and Redis identity; admission and API-key management do not use Redis. No
capability needs S3 credentials. See the [private protocol guide](../../docs-site/api/guides/identity-runtime.md)
and [local runbook](../../DEV.md) for configuration and exact permissions.

## Credential cutoff

The codec requires issuer `aap-identity/v1` and derives its HS256 signing key
from the existing signing secret with HMAC-SHA256 and that exact issuer context.
Legacy JWTs must log in again. API keys retain their SQL revocation/expiry state.
Stop every old REST/WS issuer and verifier before the coordinated upgrade;
never roll back to a validator that accepts legacy JWTs. Do not approximate old
Redis revocations by initializing durable generation to zero while accepting
old tokens.

JWT generation and JTI revocation now live in native Identity state. Lifecycle
coordinators mutate them on the same database transaction as their existing
business writes. JTI retention includes the seven-day refresh grace. An
admission verdict describes the credential at resolution time; a business
command requiring a final-write fence must explicitly call the native current
principal lock on its own transaction. Admission by itself is not that fence.

The package distributes admission, key, session, account and presence schemas.
Generate/check them with `scripts/export_schema.py` / `--check`. Native data
and privilege contracts live with migrations and the corresponding
`infra/docker/identity-*-grants.sql` files. The Go experiment is not involved
in this service.

## Preference policy

`aap_identity.preferences` owns preference read normalization and sparse patch
validation/merge. `AccountManager` keeps the User lock, portable calculation and
native apply on one owner transaction. The API can bind the same workflow to a
caller transaction or explicitly select the independent private endpoint;
configured remote/dedicated-DSN failures never choose another writer.

The distributed `preferences_write.schema.json` describes the current strict
editable model after legacy-key promotion and opaque stored-value filtering;
it is not a replacement for the stateful patch policy. The read schema allows
opaque future workspace and shortcut data. A replacement must also preserve
atomic map replacement, revision CAS, schema/engine downgrade errors, safe
validation diagnostics and unchanged opaque values. Schema validation alone
does not implement those behaviors.

## Self-account commands

`POST /v1/identity/accounts/execute` accepts `aap-identity-accounts/v1` with a
user credential plus the separate control bearer. The account DSN uses the
three-function LOGIN; `POST /v1/identity/presence/mark-inactive` uses the separate
single-function presence DSN. Both are optional capabilities in the same process.
See [ADR-0118](../../docs/adr/0118-independent-identity-account-commands.md) for
locking, mutation receipts, current-credential replay and avatar commit/cleanup
boundaries. Password verification/hash and preference merge belong to this
package; S3 and image bytes do not.

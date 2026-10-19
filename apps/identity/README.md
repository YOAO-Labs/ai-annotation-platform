# Identity admission

This package contains the single JWT/API-key verifier, a closed
`aap-identity/v1` principal contract and an independent private HTTP process.
The process has no application, SQLAlchemy, Redis, Celery or object-store
dependency. Its PostgreSQL login executes four admission capabilities only.
Login, registration, API-key management and profile/lifecycle coordinators
remain in the API and are not qualified as a complete Identity service.

## Run

Install migration 0235 and the exact Identity owner/runtime grants first.
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
the supplied credential. Request bytes, concurrency and deadlines are bounded.

`docker-compose.identity.yml` starts only this optional process. Its nonroot
image contains installed packages, not an application source bind. It supports
a read-only root filesystem and requires no Redis/S3 credentials.

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

The portable schema is distributed as `aap_identity/protocol.schema.json`.
Generate/check it with `scripts/export_schema.py` / `--check`. Native data and
privilege contracts live with migration 0235 and `identity-auth-grants.sql`.
The Go experiment is not involved in this service.

# Assets source runtime

`aap-assets-source` is a private Python control plane for source uploads and
deletion. Its public values live in the Pydantic-only `aap_assets_source`
package; the server runs from `aap_assets_source_runtime`. The runtime uses
asyncpg and boto3, and it does not import the API, SQLAlchemy, Celery, Redis,
or model packages.

The gateway calls three internal operations: `POST /v1/source/upload-init`,
`POST /v1/source/upload-complete`, and `POST /v1/source/delete`. The delete
operation handles both Dataset and DatasetItem deletion. All three require a
dedicated bearer token. The gateway supplies an actor UUID from its verified
request principal; Assets SQL rechecks current active status, account role,
manager authority where required, and the current Dataset/Item scope.

Each operation uses one PostgreSQL transaction through native reply validation,
bounded S3 work, and response serialization. Upload initialization reserves a
modern intent before signing a short-lived staging PUT. Completion verifies the
staging object, detects duplicates, reads at most 256 KiB for image dimensions,
conditionally copies to the frozen publication key, and records native
completion. Legacy completion reads the already stored source and never copies
or deletes it. A lost response can be replayed through the existing receipt.
Delete writes its audit receipt and cleanup outbox transactionally; this
service never deletes source or derived objects. The gateway dispatches media
work only after a committed successful response, preserving its existing
replay behavior.

At startup, the PostgreSQL login must have `EXECUTE` on exactly the nine source
functions in `aap_assets_v1`, no relation or sequence privileges, no owner-role
membership in either direction, and no database/schema creation rights. A
broadened login prevents startup. The S3 identity can presign PUTs and read the
configured staging and historical source objects, and can write only the
`dataset-sources/` prefix in the configured Dataset bucket. It has no delete,
list, bucket-management, or media-cache write permission. The media-cache
bucket is passed only to the transactional cleanup outbox.

## Configuration

All settings are explicit. Generic `DATABASE_URL`, `AWS_*`, MinIO, and API
settings are never fallback sources.

| Setting                                                                             | Purpose                                                                       |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `ASSETS_SOURCE_DATABASE_URL` or `_FILE`                                             | Dedicated PostgreSQL login. Accepts PostgreSQL and `postgresql+asyncpg` URLs. |
| `ASSETS_SOURCE_TOKEN` or `_FILE`                                                    | At least 32 ASCII non-whitespace characters, shared only with the gateway.    |
| `ASSETS_SOURCE_S3_ACCESS_KEY` or `_FILE`                                            | Narrow S3 identity used for staging and publication operations.               |
| `ASSETS_SOURCE_S3_SECRET_KEY` or `_FILE`                                            | Matching S3 secret.                                                           |
| `ASSETS_SOURCE_S3_ENDPOINT`                                                         | Internal S3-compatible object endpoint.                                       |
| `ASSETS_SOURCE_S3_PUBLIC_ENDPOINT`                                                  | Public origin used to sign browser PUT URLs.                                  |
| `ASSETS_SOURCE_S3_REGION`                                                           | Signing region; defaults to `us-east-1`.                                      |
| `ASSETS_SOURCE_DATASETS_BUCKET`                                                     | Original Dataset source objects and publication destination.                  |
| `ASSETS_SOURCE_IMPORT_BUCKET`                                                       | Temporary upload staging objects.                                             |
| `ASSETS_SOURCE_MEDIA_CACHE_BUCKET`                                                  | Bucket name recorded for deferred pyramid cleanup; no S3 access is used.      |
| `ASSETS_SOURCE_HOST`, `ASSETS_SOURCE_PORT`                                          | Bind address and port; defaults to `0.0.0.0:8767`.                            |
| `ASSETS_SOURCE_MAX_REQUEST_BYTES`, `ASSETS_SOURCE_MAX_RESPONSE_BYTES`               | JSON body bounds; defaults to 1 MiB and 16 MiB.                               |
| `ASSETS_SOURCE_POOL_MIN_SIZE`, `ASSETS_SOURCE_POOL_MAX_SIZE`                        | Bounded asyncpg pool; defaults to 1 and 8.                                    |
| `ASSETS_SOURCE_POOL_TIMEOUT_SECONDS`, `ASSETS_SOURCE_SQL_TIMEOUT_SECONDS`           | Connection and statement bounds.                                              |
| `ASSETS_SOURCE_S3_CONNECT_TIMEOUT_SECONDS`, `ASSETS_SOURCE_S3_READ_TIMEOUT_SECONDS` | Bounded object-store calls.                                                   |

Set either a secret value or its `_FILE` path, never both. Startup performs no
bucket probes, object listing, or S3 writes.

`GET /health` is liveness. `GET /ready` checks PostgreSQL readiness. Business
errors use the strict `aap-assets-source/v1` response envelope. Oversized
requests are rejected with 413, invalid service credentials with 401, and
unexpected storage/database failures with a generic 503 response.

Run locally with the server extra installed:

```sh
uv sync --extra server
python -m aap_assets_source_runtime
```

Build the standalone image from the repository root with:

```sh
docker build -f apps/assets-source/Dockerfile .
```

## Dataset import worker

`Dockerfile.import` builds a separate worker process, started with
`python -m aap_assets_source_import`. It uses its own PostgreSQL LOGIN role,
which must have execute access to exactly the seven Dataset import functions;
it exposes no HTTP port. Source S3 reads are conditional on the listed ETag,
SFTP uses known-host verification and checks the listed file identity, and the
destination client uses only multipart create, part, complete, abort, and empty
object PUT operations.

Build the worker image from the repository root with:

```sh
docker build -f apps/assets-source/Dockerfile.import .
```

The worker buffers at most one 64 MiB multipart part. S3's 10,000-part limit
therefore caps one object at about 625 GiB. If the accepted job budget permits
a larger individual object, it is recorded as a per-file destination write
failure before any destination upload starts. The accepted per-job byte budget
remains the value frozen in the import request. Supporting larger individual
objects requires a separate capacity design.

# Assets catalog runtime

`aap-assets-catalog` is a private Python control plane for the Assets catalog.
Its portable values use Pydantic and public Workspace/Source value packages.
Source supplies the shared frame and Scene planner; no API-private code is imported. The server uses asyncpg and boto3;
it does not import the API application, SQLAlchemy models, Celery, Redis, or ML
packages.

The service exposes the frozen internal operations `POST /v1/catalog/list`,
`/get`, `/items`, `/create`, `/update`, `/dataset-projects`, and
`/project-datasets`. The last two return the two current Assets association
projections in the separate `aap-assets-associations/v1` envelope. Every
operation requires the gateway's dedicated `Authorization: Bearer ...` credential. The caller supplies
`actor_id` only from its already verified principal; it must never copy the
field from user input. The Assets SQL functions recheck active status and
current manager role in the write transaction. User API-key scopes and public
HTTP errors remain the gateway's responsibility.

The PostgreSQL login is checked on startup. It must have only `EXECUTE` on the
29 catalog, association and spatial functions, no table/column/sequence grants, no other owner-schema
access, no role memberships, and no database or schema creation rights. A
failed or broadened login prevents startup.

Dataset create and update are database-only. The service does not create
buckets or write folder marker objects. It does not move stored files when a
Dataset name changes. Presigned item URLs use a separate, explicit read-only
S3 identity and only `GetObject` for the configured datasets and media-cache
object namespaces. The signer never lists, creates, writes, or deletes objects.
Source publication and deletion use the separate Assets Source runtime.
Spatial Scene/frame/pose/calibration state uses this Catalog runtime and the
`aap-assets-spatial/v1` contract; task/annotation/link composition stays with Labeling.

## Configuration

All values are explicit; generic `DATABASE_URL`, `AWS_*`, MinIO, or application
settings are never used as fallbacks.

| Setting                                                                     | Purpose                                                                                           |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `ASSETS_CATALOG_DATABASE_URL` or `_FILE`                                    | Dedicated PostgreSQL catalog login. Accepts `postgresql://` and `postgresql+asyncpg://`.          |
| `ASSETS_CATALOG_TOKEN` or `_FILE`                                           | At least 32 ASCII non-whitespace characters, shared only with the API gateway.                    |
| `ASSETS_CATALOG_S3_ACCESS_KEY` or `_FILE`                                   | Read-only S3 signing identity.                                                                    |
| `ASSETS_CATALOG_S3_SECRET_KEY` or `_FILE`                                   | Read-only S3 signing identity.                                                                    |
| `ASSETS_CATALOG_S3_PUBLIC_ENDPOINT`                                         | Browser-reachable origin used to create SigV4 URLs. The service signs against this host directly. |
| `ASSETS_CATALOG_S3_ENDPOINT`                                                | Fixed origin for server-to-server calibration reads; defaults to the public origin.               |
| `ASSETS_CATALOG_S3_REGION`                                                  | Signing region; defaults to `us-east-1`.                                                          |
| `ASSETS_CATALOG_DATASETS_BUCKET`                                            | Bucket containing original Dataset objects.                                                       |
| `ASSETS_CATALOG_MEDIA_CACHE_BUCKET`                                         | Bucket containing generated thumbnail/video/pyramid keys.                                         |
| `ASSETS_CATALOG_HOST`, `ASSETS_CATALOG_PORT`                                | Bind address and port; defaults to `0.0.0.0:8766`.                                                |
| `ASSETS_CATALOG_MAX_REQUEST_BYTES`                                          | Maximum JSON request body; defaults to 1048576.                                                   |
| `ASSETS_CATALOG_MAX_RESPONSE_BYTES`                                         | Maximum JSON response body; defaults to 16777216.                                                 |
| `ASSETS_CATALOG_POOL_MIN_SIZE`, `ASSETS_CATALOG_POOL_MAX_SIZE`              | Bounded asyncpg pool; defaults to 1 and 8.                                                        |
| `ASSETS_CATALOG_POOL_TIMEOUT_SECONDS`, `ASSETS_CATALOG_SQL_TIMEOUT_SECONDS` | Connection and statement bounds.                                                                  |

Set either a secret value or its `_FILE` path, never both. The S3 identity needs
only `GetObject` for the catalog's object signing scope. No bucket probing is
performed at startup because it would require broader S3 permissions.

`GET /health` is liveness. `GET /ready` verifies the PostgreSQL connection;
S3 signing failures remain best-effort and yield null URLs as in the API.
Typed catalog business failures use the `aap-assets-catalog/v1` JSON envelope.
Malformed JSON is 422, oversized requests are 413, unauthorized service calls
are 401, and unexpected native/database failures are 503 without raw details.

Run locally with the server extra installed:

```sh
uv sync --extra server
python -m aap_assets_catalog
```

The standalone Compose definition is `docker-compose.assets-catalog.yml`. It
does not publish a host port; the API gateway should call it on the private
Compose network.

## Spatial operations

The packaged `spatial_contract.schema.json` freezes twenty-two native capabilities
and four owner workflows. HTTP operations use `POST /v1/spatial/<native_name>`;
whole workflows are `backfill_scenes`, `sniff_axis`, `prepare_pointcloud` and `update_calibration`.
Bounded pages bind scope, cursor, complete native snapshot and total; consumers
validate monotone unique records and completion before exposing the whole read.
Public REST response shapes remain unchanged.

Mutations save immutable replay receipts. Calibration CAS, append-only history,
current metadata merge and Audit remain in one transaction. Persisted digests stay
opaque because JSONB normalization can lose negative zero; only virtual legacy
revision one computes the historical normalized digest on read. Current document
SHA verification is required for virtual legacy state; new document SHA is always
verified. Accepted point-cloud preparation retains original execution authority,
while unlink/cancel fences final apply.

Conditional calibration reads use only the fixed datasets bucket, a one MiB body
cap and bounded network timeouts. Capture does not add spatial row locks across
S3 I/O. Explicit caller-owned composition uses public native capabilities on the
same SQL connection; normal remote errors never switch to local writes.

Upgrade SQL authorities before the main spatial artifact, reapply owner and
Catalog grants, then restart the service. Exact-role preflight requires the
current 29-function surface. This is a spatial slice, not full Assets or
production/language-replacement qualification.

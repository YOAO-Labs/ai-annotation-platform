# Assets source runtime

`aap-assets-source` is the existing private Python control plane for source
uploads, deletion, synchronous ZIP ingest, and legacy bucket scanning. Its
public values live in the Pydantic-only `aap_assets_source`
package; the server runs from `aap_assets_source_runtime`. The runtime uses
asyncpg and boto3, and consumes the Pydantic-only `aap-assets-media` image
contracts and policy. It does not import the API, SQLAlchemy, Celery, Redis,
or ORM model packages.

The same runtime also owns the six storage-connection operations:
`POST /v1/connections/list`, `read`, `create`, `update`, `delete`, and `test`.
The API keeps its authenticated public endpoints and sends actor UUIDs and
request values to this internal bearer-authenticated interface. When
`ASSETS_SOURCE_URL` is configured, a remote error is returned to the caller;
the gateway does not retry through the local implementation.

The gateway calls five source operations: `POST /v1/source/upload-init`,
`POST /v1/source/upload-complete`, `POST /v1/source/delete`,
`POST /v1/source/upload-zip`, and `POST /v1/source/scan`. Delete handles both
Dataset and DatasetItem deletion. ZIP accepts a bounded `application/zip`
stream; scan accepts a typed JSON scope. All five require a dedicated bearer
token. The gateway supplies an actor UUID from its verified request principal;
Assets SQL rechecks current active status, account role, manager authority for
scan and deletion where required, and the current Dataset/Item scope. ZIP
requires an active authenticated account with `datasets:write`, not a
manager-only role. Scan retains the existing `super_admin` and
`project_admin` manager set plus `datasets:write`.

Each operation uses one PostgreSQL transaction through native reply validation,
bounded S3 work, and response serialization. Upload initialization reserves a
modern intent before signing a short-lived staging PUT. Completion verifies the
staging object, detects duplicates, reads at most 256 KiB for image dimensions,
conditionally copies to the frozen publication key, and records native
completion. Image identity binding and durable media admission commit with Item
registration; legacy Task completion similarly admits work in the task-status
transaction. Legacy completion reads the already stored source and never
copies or deletes it. A lost response replays the existing receipt without
creating a second image job.
Delete writes its audit receipt and cleanup outbox transactionally; this
service never deletes source or derived objects. ZIP and scan also commit each
Item, Dataset count, Labeling append receipt, optional Scene/frame updates,
operation receipt, and media-delivery intent in one outer transaction. ZIP
copies use immutable keys under
`dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}` and store the
logical path in `metadata.source_ingest.relpath`; the workflow never overwrites
or deletes an existing source. Scan reads only the exact accepted
`{Dataset.name}/` prefix in the configured datasets bucket and does not adopt
modern publications or publication objects outside that exact legacy prefix.
The API maintenance relay delivers remaining committed non-image media intents
after the transaction, so a lost response can replay the first receipt without
duplicating Items, source writes, or image jobs. Image admission is part of the
registration transaction.

The Source PostgreSQL login executes exactly twenty-six functions in
`aap_assets_v1`: fourteen source/ingest capabilities, nine connection
capabilities, and three image-source/admission capabilities. It has no direct
relation or sequence privileges, no access to other owner schemas, no
owner-role membership, and no database/schema creation rights. A broadened
login prevents startup. The two media-delivery claim/ack
capabilities are for the API maintenance relay only and are not granted to the
Source LOGIN. The connection import worker has its own login limited to nine
import capabilities; the Catalog login remains separate with twenty-nine
capabilities.

The local image and nuScenes importer scripts register Dataset/Item state
through six native Assets functions. A separate trusted `asset_operator_client`
login is published for consumers of those six functions. The complete scripts
keep their existing composition connection: the six-function login cannot run
their Identity, Workspace or Labeling steps. This is not part of the Source
runtime or its 26-function login:
the importer scripts call the native SQL contract on their caller-owned
transaction and do not use the Source HTTP API or fall back through
`ASSETS_SOURCE_URL`. The operator login has no direct table privileges and no
other owner-schema access. Existing owner UUIDs record attribution; possession
of this operator credential supplies the SQL execution authority. Import
scripts retain their current validation and ownership selection.

Item registration is ordered and split into batches of at most 500 entries.
Request bodies are limited to 8 MiB; reads return bounded pages of at most 500
items and 15 MiB of item JSON, within a 16 MiB response. Re-import updates use
opaque source snapshots as compare-and-set values and merge only the
requested nuScenes metadata, dimensions, size, or hash facts. Database
nuScenes imports still include the caller's spatial and Labeling writes, but
object-store uploads and overwrites are external I/O: SQL rollback leaves
those bytes in place. The protocol and grants are documented in the
[same-database module data guide](../../docs-site/api/guides/module-data.md)
and [ADR-0104](../../docs/adr/0104-operator-source-registration.md).

Connection list/read responses contain public facts and `secret_set`, never
ciphertext. A capture operation returns an encrypted-secret snapshot to the
trusted provider. Create, update, delete, and test results are recorded with
Audit in the same caller-owned SQL transaction; immutable connection receipts
have no live-resource foreign keys. Deleting a connection leaves accepted
import snapshots intact, while later imports must still resolve the current
connection. The Source provider checks the host and performs DNS/probe I/O
before final application. Settings' database allowlist overrides the Source
environment fallback; a short `SHARE` lock rechecks the policy at final commit
without spanning DNS or connector I/O. Name-only and null-patch updates do not
consult the host policy or require the encryption key.

The S3 identity can presign PUTs and read configured staging and historical
source objects. Scan additionally needs `ListBucket` on its own configured
datasets bucket and conditional HEAD/GET access to the accepted Dataset
prefix. ZIP writes only under `dataset-sources/`, with multipart abort
permission for its own publication objects. The identity has no source delete,
foreign-bucket listing, bucket-management, or media-cache write permission.
The media-cache bucket is passed only to the transactional cleanup outbox.

## Synchronous ZIP and bucket scan

The public ZIP endpoint remains synchronous and keeps its existing response
shape. It accepts an optional UUID `Idempotency-Key`; reuse the same key after
an uncertain response. ZIP request identity includes the SHA-256 digest of the
raw archive, so reusing a key for a different actor, Dataset, operation kind,
or archive conflicts. The first completed receipt is retained for replay.

ZIP processing is bounded to 200 MiB compressed and expanded, 5,000 file
entries, 100 MiB per entry, a 100:1 compression ratio, and 10,000 central
directory entries. The runtime admits one ZIP upload at a time and spools the
authenticated raw stream before opening the database transaction. The
standalone Compose service is limited to 512 MiB memory and a 256 MiB `/tmp`.
It preserves the existing hidden/macOS and unsafe-path
handling, MIME-based type inference, content-hash dedupe, per-entry errors,
basename labels, logical subdirectory layout, and ZIP-only automatic Scene
inference. A temporal Dataset with no inferred Scene fails the SQL transaction;
already copied source bytes remain in their immutable publication paths.

The synchronous scan operation uses the exact Dataset name plus `/` prefix in
the fixed datasets bucket. It keeps the manager-only permission and its
existing `{status, new_items, linked_tasks}` response. It streams object-list
pages, skips already registered paths, and never infers or asserts Scenes. It
does not adopt modern `dataset-sources/` publications or objects outside that
exact legacy prefix.
The scan command has a frozen protocol fingerprint, so a retried operation
with the same optional key returns the original receipt.

Neither operation creates a connector Job or uses the connector-import
protocol. ZIP runs in the existing Source HTTP runtime and uses its existing
configuration; no bulk-ingest environment variables are added.

## Configuration

### Source operations

All settings are explicit. Generic `DATABASE_URL`, `AWS_*`, MinIO, and API
settings are never fallback sources.

| Setting                                                                             | Purpose                                                                                                                  |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `ASSETS_SOURCE_DATABASE_URL` or `_FILE`                                             | Dedicated PostgreSQL login. Accepts PostgreSQL and `postgresql+asyncpg` URLs.                                            |
| `ASSETS_SOURCE_TOKEN` or `_FILE`                                                    | At least 32 ASCII non-whitespace characters, shared only with the gateway.                                               |
| `ASSETS_SOURCE_S3_ACCESS_KEY` or `_FILE`                                            | Narrow S3 identity used for staging and publication operations.                                                          |
| `ASSETS_SOURCE_S3_SECRET_KEY` or `_FILE`                                            | Matching S3 secret.                                                                                                      |
| `ASSETS_SOURCE_S3_ENDPOINT`                                                         | Internal S3-compatible object endpoint.                                                                                  |
| `ASSETS_SOURCE_S3_PUBLIC_ENDPOINT`                                                  | Public origin used to sign browser PUT URLs.                                                                             |
| `ASSETS_SOURCE_S3_REGION`                                                           | Signing region; defaults to `us-east-1`.                                                                                 |
| `ASSETS_SOURCE_DATASETS_BUCKET`                                                     | Original Dataset source objects and publication destination.                                                             |
| `ASSETS_SOURCE_IMPORT_BUCKET`                                                       | Temporary upload staging objects.                                                                                        |
| `ASSETS_SOURCE_MEDIA_CACHE_BUCKET`                                                  | Bucket name recorded for deferred pyramid cleanup; no S3 access is used.                                                 |
| `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY` or `_FILE`                                 | Optional Fernet key; set the same value as `CONNECTOR_ENCRYPTION_KEY` for connection creation and credential decryption. |
| `ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST`                                            | JSON string array fallback, default `[]`; the Settings database override wins.                                           |
| `ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE`                                               | Optional readable known-hosts file; unknown SFTP host keys remain rejected.                                              |
| `ASSETS_SOURCE_HOST`, `ASSETS_SOURCE_PORT`                                          | Bind address and port; defaults to `0.0.0.0:8767`.                                                                       |
| `ASSETS_SOURCE_MAX_REQUEST_BYTES`, `ASSETS_SOURCE_MAX_RESPONSE_BYTES`               | JSON body bounds; defaults to 1 MiB and 16 MiB.                                                                          |
| `ASSETS_SOURCE_POOL_MIN_SIZE`, `ASSETS_SOURCE_POOL_MAX_SIZE`                        | Bounded asyncpg pool; defaults to 1 and 8.                                                                               |
| `ASSETS_SOURCE_POOL_TIMEOUT_SECONDS`, `ASSETS_SOURCE_SQL_TIMEOUT_SECONDS`           | Connection and statement bounds.                                                                                         |
| `ASSETS_SOURCE_S3_CONNECT_TIMEOUT_SECONDS`, `ASSETS_SOURCE_S3_READ_TIMEOUT_SECONDS` | Bounded object-store calls.                                                                                              |

Set either a secret value or its `_FILE` path, never both. The Source connector
key is optional at startup and is separate from `ASSETS_SOURCE_TOKEN`. Use the
same Fernet value as the local API and importer so existing ciphertext remains
readable. Startup performs no bucket probes, object listing, or S3 writes.
Operations that create credentials or decrypt an existing credential need the
key. A plain name-only or null patch can proceed without the key or a host-policy
lookup. Set the Source fallback allowlist as
JSON, such as `[".example.org", "10.20.0.0/16"]`; it is used only when no
database override exists. Mount a known-hosts file read-only and ensure the
Source container user can read it.

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
which must have execute access to exactly nine Dataset import functions;
it exposes no HTTP port. Source S3 reads are conditional on the listed ETag,
SFTP uses known-host verification and checks the listed file identity, and the
destination client uses only multipart create, part, complete, abort, and empty
object PUT operations.

Image admission uses the ETag, optional VersionId and measured byte count from
the successful write receipt. Missing or invalid receipt metadata fails the
entry; the write-only destination client does not need a subsequent HEAD or GET.
Transient `dataset_busy` heartbeats retry the same lease only until its last
confirmed expiry, including the renewal call itself. Lease loss, cancellation
and uncertain renewal stop I/O and leave recovery to the lease protocol.

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

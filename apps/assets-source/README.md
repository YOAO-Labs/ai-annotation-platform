# Assets source runtime

`yoao-assets-source` is the existing private Python control plane for source
uploads, deletion, synchronous ZIP ingest, and legacy bucket scanning. Its
public values live in `yoao_assets_source`; importing them does not require
the optional storage SDK. Managed-original I/O uses boto3/botocore from the
server or importer extra. The server runs from `yoao_assets_source_runtime`,
uses asyncpg and boto3, and consumes the Pydantic-only `yoao-assets-media` image
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
token. The gateway supplies the authenticated actor and the original
credential's non-secret authority, including its API-key verifier revision or
JWT generation and expiry, and the admitted Dataset lifecycle revision.
Assets SQL fences the current credential and Dataset before publishing. ZIP
normally requires `datasets:write`; a Dataset with a directory source also
requires current directory management authority. Scan retains the existing
`super_admin` and `project_admin` manager set plus `datasets:write`.

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
seals accepted members across every batch before publishing any business metadata.
Native staging validates the entire batch before changing its settlement markers.
Verified creations remain owned for controlled retirement if publication is rejected.
Scan also finishes physical reads before acquiring the final publication fences.
ZIP copies use immutable keys under
`dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}` and store the
logical path in `metadata.source_ingest.relpath`; the workflow never overwrites
or deletes an existing source. Scan reads only the exact accepted
`{Dataset.name}/` prefix in the configured datasets bucket and does not adopt
modern publications or publication objects outside that exact legacy prefix.
The API maintenance relay delivers remaining committed non-image media intents
after the transaction, so a lost response can replay the first receipt without
duplicating Items, source writes, or image jobs. Image admission is part of the
registration transaction.

The Source PostgreSQL login executes exactly thirty-seven functions in
`aap_assets_v1`: fifteen source/ingest capabilities, eleven connection
capabilities, three image-source/admission capabilities, and eight managed-original
capabilities. It has no direct
relation or sequence privileges, no access to other owner schemas, no
owner-role membership, and no database/schema creation rights. A broadened
login prevents startup. The two media-delivery claim/ack
capabilities are for the API maintenance relay only and are not granted to the
Source LOGIN. The connection import worker has its own login limited to nine
import capabilities; the Catalog login remains separate with twenty-nine
capabilities.

The local image and nuScenes importer scripts register Dataset/Item state
through the Assets operator functions. The trusted `asset_operator_client`
login can also execute Identity's bounded `read_first_seed_account()` for local
image attribution. It receives no Identity table or profile-relation access.
The nuScenes importer additionally composes Workspace, Labeling and spatial
capabilities in its caller transaction; the Assets operator login alone cannot
perform those steps. Neither importer falls back through `ASSETS_SOURCE_URL`.
Existing owner UUIDs record attribution; the operator credential grants the
corresponding SQL execution authority.

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
| `ASSETS_SOURCE_IMPORT_VERIFY_TOKEN` or `_FILE`                                      | Dedicated importer-verification bearer; distinct from the gateway token and at least 32 ASCII non-whitespace characters. |
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
| `ASSETS_SOURCE_IMPORT_VERIFY_TIMEOUT_SECONDS`                                       | Full importer-original verification budget, 0.1–600 seconds; default 120.                                                |

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
python -m yoao_assets_source_runtime
```

Build the standalone image from the repository root with:

```sh
docker build -f apps/assets-source/Dockerfile .
```

## Dataset import worker

`Dockerfile.import` builds a separate worker process, started with
`python -m yoao_assets_source_import`. It uses its own PostgreSQL LOGIN role
with the exact functions in `infra/docker/assets-import-grants.sql`; it exposes
no HTTP port. Source S3 reads are conditional on the listed ETag,
SFTP uses known-host verification and checks the listed file identity, and the
destination client uses only multipart create, part, complete, abort, and empty
object PUT operations.

The importer never reads destination objects. After a conditional write and its
durable storage-owner receipt, the Source service performs a full, version-pinned
readback using its own saved target and verifies the frozen SHA-256, MD5 and
size. Importer authenticates to the two finite verification endpoints with
`ASSETS_IMPORT_VERIFY_TOKEN`; Source uses the matching
`ASSETS_SOURCE_IMPORT_VERIFY_TOKEN`, which must differ from the gateway token.
`ASSETS_IMPORT_VERIFY_URL` is the Source origin reachable by the importer.
The Compose overlays mount separate default files:
`.secrets/assets-source-import-verify-token` for Source UID 10003 and
`.secrets/assets-import-verify-token` for Importer UID 10004. Prepare both with
identical bytes and each runtime's read access; do not broaden file permissions
to share one path. A Source-only Compose deployment still prepares its mounted
verification secret, even if no importer is running.

For local Compose, combine both existing overlay files in one project and set
`ASSETS_IMPORT_VERIFY_URL=http://assets-source:8767`; the default private network
supplies the service name. A host-loopback published port is not a Source origin
reachable from the Importer container. See the root `DEV.md` deployment recipe.

Requests name only the accepted job, current lease, entry, item, creation and
storage operation; Source resolves the target itself. Source's
`ASSETS_SOURCE_IMPORT_VERIFY_TIMEOUT_SECONDS` bounds admission and the full read
independently of its S3 per-socket timeout; the importer retains its own finite
wait budget. The original Source request retries NOWAIT admission contention
within that budget and rechecks the exact lease before starting any read.
Source durably records drained read evidence before final owner settlement.
An uncertain initial acknowledgement, including a gateway error, damaged reply
or internally valid receipt that disagrees with the locally frozen original,
recovers only the exact operation's status; it never reposts verification.
The importer compares SHA256, size, ETag and version against those frozen facts,
and also MD5 for a fresh write. These expectations stay local; the Source request
still has exactly seven identity fields. A mismatching status receipt fails.
Temporary status disconnections, including a truncated declared-length body,
and HTTP 502/503/504 remain bounded by the original wait budget and confirmed
lease. Malformed status framing, including illegal chunk lengths, authoritative
authentication/request rejection and invalid status contracts fail meaningfully
instead of repeatedly reclaiming the job.
A failed HTTP call alone does not settle physical I/O: writes remain owned until proven drained,
and read reservations close only on proof of being unstarted or fully drained.
Accepted execution keeps its frozen authority after account deactivation or
demotion; current lease, cancellation, parent and publication fences still apply.
The current copied-entry contract is selected by `IMPORT_COMPLETION_SCHEMA_RESOURCE`
and packaged as `0453_import_original_completion.schema.json`; the earlier SQL
schema remains a historical migration artifact. Source independently revisits
drained evidence and stale unstarted read reservations, including after lease
loss. For a terminal, expired, or superseded import scope whose acknowledged
write has no Source read receipt, recovery atomically fences late admission and
settles the exact write permit from its retained write receipt; it neither
deletes the object nor releases a potentially started read. A Source-owned
admission identifier lets the original request recover its own uncertain
pre-read admission after an inner database timeout, within its original budget.
On cancellation or failure, Source shields settlement of that exact unstarted
admission; it never settles a different admission or assumes an unclosed read.

For the built-in datasets target, the Source S3 identity needs `s3:GetObject`
and `s3:GetObjectVersion` on the existing `dataset-sources/*` managed-original
scope and on the Dataset scan roots it is already allowed to read. Scan adoption
reads the observed exact version to verify bytes; multipart ETags are not content
hashes. This does not grant writes or deletes on those scan roots.
Connection-backed targets must grant their own configured Source credentials
the same read operations on their saved managed-original scope.
The built-in importer identity does not receive destination read permissions.
Connection-backed destinations retain the existing shared connection principal:
the importer can decrypt those configured credentials for writes, so any read
permissions already held by that principal are also available to the importer.
There is no separate external read/write credential boundary in this contract.

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

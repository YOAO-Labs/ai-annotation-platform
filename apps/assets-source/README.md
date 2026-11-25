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
The public API always embeds `list` and `read`. Setting `ASSETS_SOURCE_URL`
selects the credential-owning host for `create`, `update`, `delete` and `test`
only. A remote failure never retries through the API's local implementation.
The Source process retains exact import verification, recovery and Native
signing with operation-specific credentials.

The portable server retains five source operations: `POST /v1/source/upload-init`,
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

Public upload, delete, ZIP and scan use these same owner workflows on the
API's request transaction, not the Source origin. Keep shared PostgreSQL
receipts during cutover and reconcile uncertain acknowledgements by key.

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

The Source PostgreSQL login executes exactly forty functions in
`yoao_assets_v1`: fifteen source/ingest capabilities, eleven connection
capabilities, three image-source/admission capabilities, eight managed-original
capabilities, and three Native pyramid signer capture capabilities. It has no direct
relation or sequence privileges, no access to other owner schemas, no
owner-role membership, and no database/schema creation rights. A broadened
login prevents startup. The two media-delivery claim/ack
capabilities are for the API maintenance relay only and are not granted to the
Source LOGIN. The connection import worker has its own login limited to nine
import capabilities; the Catalog login remains separate with twenty-nine
capabilities.

The local image and nuScenes importer scripts register Dataset/Item state
through the Assets operator functions. The trusted `yoao_asset_operator_client`
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

The ordinary S3 identity can presign PUTs and read configured staging and
historical source objects. Scan additionally needs `ListBucket` on its own
configured datasets bucket and conditional HEAD/GET access to the accepted
Dataset prefix. ZIP writes only under `dataset-sources/`, with multipart abort
permission for its own publication objects. The identity has no source delete,
foreign-bucket listing, bucket-management, or media-cache write or delete
permission. The media-cache bucket reaches the transactional cleanup outbox
and the explicitly configured Native signer only.

## Native pyramid storage signing

The runtime also owns five finite Native pyramid signing routes. They reuse the
Images success/failure envelope and never return connection configuration,
ciphertext or plaintext credentials:

- `POST /v1/native-pyramids/write-reference` and
  `POST /v1/native-pyramids/verify-reference` use the writer token and bind one
  exact reserved member operation, its current job/generation/lease and its
  submitted or recorded receipt.
- `POST /v1/native-pyramids/read-references` uses the reader token and returns
  either internal conditional HEAD/GET requests for one active read permit or
  public GET URLs for its already verified members.
- `POST /v1/native-pyramids/retirement-references` and
  `POST /v1/native-pyramids/retirement-prefix-reference` use the cleanup token
  and bind one exact prepared retirement page, its live lease and its immutable
  member IDs. The prefix route signs only the empty terminal page.

Each route captures its private context through one of the three Source-only
SQL capabilities (`capture_native_pyramid_write_io`,
`capture_native_pyramid_read_io`, `capture_native_pyramid_retirement_io`),
releases that transaction, resolves and checks the saved target, then captures
and compares the exact scope again before signing. Saved endpoint, bucket,
prefix and Native member receipts come from the output allocation; current
purpose bindings are irrelevant for old outputs. A mutated or missing saved
location is refused instead of redirected.

The builtin target signs with a separate Native S3 identity from
`ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY` and
`ASSETS_SOURCE_NATIVE_S3_SECRET_KEY`, limited to Native Media-cache output
prefixes. The ordinary Source identity keeps its existing permissions and
never gains Native deletion rights. Connection-backed targets use their
registered encrypted secret only inside Source, under the current connector
key and host policy. A database host policy overrides the Source environment:
an explicit empty policy refuses every host, while an absent policy falls back
to `ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST`. The signer does not perform
PUT/GET/HEAD/DELETE or listing I/O, does not accept arbitrary buckets, keys,
methods or queries, and cannot be used as a generic S3 signing service.

The captured context retains the complete frozen `IMAGE_PYRAMID_URL_EXPIRY_SECONDS`
policy value. Every issued SigV4 capability and public URL is capped at the
SigV4 maximum of 604800 seconds, and an explicitly requested lifetime above
that cap is refused rather than shortened silently.

The feature is all-or-nothing. Configure
`ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN[_FILE]`,
`ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN[_FILE]`,
`ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN[_FILE]`,
`ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY[_FILE]` and
`ASSETS_SOURCE_NATIVE_S3_SECRET_KEY[_FILE]` together. The three tokens must be
distinct and distinct from `ASSETS_SOURCE_TOKEN` and
`ASSETS_SOURCE_IMPORT_VERIFY_TOKEN`; the Native access key must differ from
`ASSETS_SOURCE_S3_ACCESS_KEY`. Leaving every Native setting empty keeps the
feature disabled and all five routes return `source_unavailable`. A partial
Native configuration fails startup. There is no fallback signer or configured
cache-bucket substitution for Native callers.

For deployment, keep the five signer files separate from the ordinary Source
credentials and readable only by the Source runtime user (UID 10003 for the
optional container). Attach `infra/docker/native-source-minio-policy.json` to
the separate builtin Native S3 identity; replace `prod-media-cache` in the
template with the deployment's media-cache bucket. It grants only the
saved-output scopes `image-pyramids/objects/*` and
`_aap_managed/media-cache/image-pyramids/objects/*`, bounded object/version
listing and bucket-scoped multipart listing. It explicitly denies
`AbortMultipartUpload` and `ListMultipartUploadParts`, including on peers that
expand `PutObject` into additional multipart actions. It does not grant bucket
administration or access to unrelated objects, and it must not be attached to
the ordinary Source, Images, or cleanup identities. Dedicated Native
connection credentials need the same explicit denials; they remain inside Source.

The finite signer routes are consumed by three independent runtimes with one
distinct role token each: the image worker and API-local executor use
`ASSETS_IMAGES_NATIVE_STORAGE_URL` with the writer token; embedded API control
or explicitly separate Media control uses `ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL` with the reader
token, and Assets Cleanup uses `ASSET_CLEANUP_NATIVE_STORAGE_URL` with the
cleanup token. Each caller reads its token from its own file; the values must
equal the corresponding Source token, but the files are not shared across
runtime users. Cleanup requires both Native settings, so a deployment that runs
the cleanup worker also enables this signer. The repository's
`docker-compose.assets-native.yml` supplies these bindings on top of the
Source, media and cleanup overlays.

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

Neither operation creates a connector Job or uses the connector-import protocol.
The synchronous public ZIP path runs within the API's existing bounded spool
and Source workflow; the standalone HTTP operation is retained for portability.

## Registered S3/SFTP physical operations

The existing Source process also handles configured, non-builtin storage. The
ordinary API continues to admit the operation in its own transaction and passes
only owner evidence through `POST /v1/saved-io/{family}/{action}`. Reference,
outputs, media, training, migration and binding probes have finite actions and
fixed database functions. Source resolves the saved revision and decrypts the
registered connector locally. It does not return credentials or a generic
signer; only existing public signed transfers may return a URL for the exact
authorized method/object/version. Builtin locations retain their existing
paths. Metadata capture does not open a connection or resolve DNS.

Enable the Source channel with `ASSETS_SOURCE_SAVED_IO_DATABASE_URL[_FILE]`
and the distinct `ASSETS_SOURCE_SAVED_IO_READ_TOKEN[_FILE]`,
`ASSETS_SOURCE_SAVED_IO_WRITE_TOKEN[_FILE]`, and
`ASSETS_SOURCE_SAVED_IO_CLEANUP_TOKEN[_FILE]`. After the Assets migrations,
run `infra/docker/source-channel-saved-io-grants.sql` with psql variables
`expected_database` and `assets_source_saved_io_login`. It installs all physical
operation grants in one transaction. The Saved I/O login cannot read relations or
execute management or unrelated owner functions; an incomplete or broadened login prevents startup. Keep the
connector key (`ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY[_FILE]`), saved-I/O DSN,
and SFTP host trust (`ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE` and, for jailed
references, `STORAGE_SFTP_ROOTS`) only in Source. Configure the existing Source
allowlist fallback; an explicit database policy, including an empty policy,
takes precedence. Source pins checked network addresses and preserves TLS
hostname/SFTP host key verification.

Consumers receive `ASSETS_SOURCE_URL` and only the bearer values needed for
their own work. Configure `ASSETS_SOURCE_CONNECTIONS_TOKEN[_FILE]` separately
on Source and the API for existing connector create/update/test; this credential
does not activate retired Source submission routes or authorize saved I/O. The
optional `ASSETS_SOURCE_SAVED_IO_CUTOVER_TOKEN[_FILE]` goes only to Source and
the migration cutover CLI. Omitted cutover configuration does not enable the
operation using a different bearer.

The binary channel bounds headers, command payloads and concurrent work. It
spools one seekable upload at a time to the explicitly configured writable
`ASSETS_SOURCE_SAVED_IO_SPOOL_DIR` (Source-owned, outside the legacy `/tmp`
tmpfs). `ASSETS_SOURCE_SAVED_IO_MAX_UPLOAD_BYTES` defaults to `68719476736`
(64 GiB); size scratch to the selected limit per Source instance. A lower site
limit refuses larger uploads before provider I/O. Downloads stream without
spooling their complete contents in the shared transport; an interrupted
receipt never triggers an automatic replay or substitutes a local connection.
Mixed managed-original operations that use the existing builtin annotations
bucket also need `ASSETS_SOURCE_ANNOTATIONS_BUCKET` set on Source.

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
with the exact functions in `infra/docker/assets-grants.sql` (`-v grant_import=1`); it exposes
no HTTP port. Install the lease-bound import capabilities and grants with the
Source Saved I/O capabilities before starting the worker.

The existing Source process enumerates and opens registered S3 and SFTP objects.
It admits the exact job and unexpired lease and checks the durable observation
and entry before completing a read. Source checks the configured host policy,
pins S3 reads to the observed provider identity, and checks the SFTP host key,
file identity and path. The importer gets only the observed manifest and the
bytes needed to write a builtin original. It never receives the connector
ciphertext, decryption key, outbound host allowlist or known-hosts file.

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

For local Compose, combine `docker-compose.assets-source.yml` and
`docker-compose.assets-import.yml` in one project. Set both `ASSETS_IMPORT_VERIFY_URL`
and `ASSETS_IMPORT_SAVED_IO_URL` to `http://assets-source:8767` on the default private
network. Do not use a host-loopback published port from inside the importer.

Supply two more distinct, scoped token pairs using read-only files:

| Action                                   | Source (UID 10003)                               | Importer (UID 10004)                      |
| ---------------------------------------- | ------------------------------------------------ | ----------------------------------------- |
| Enumerate/read an accepted import        | `ASSETS_SOURCE_SAVED_IO_IMPORT_READ_TOKEN_FILE`  | `ASSETS_IMPORT_SAVED_IO_READ_TOKEN_FILE`  |
| Copy into a saved registered destination | `ASSETS_SOURCE_SAVED_IO_IMPORT_WRITE_TOKEN_FILE` | `ASSETS_IMPORT_SAVED_IO_WRITE_TOKEN_FILE` |

For each row, the two files hold the same token bytes, but the read token, write
token and verification token must differ from one another and from the Source
management and general Saved I/O tokens. Only Source receives its Saved I/O
database LOGIN, connector key, host allowlist and SFTP known-hosts file.
`ASSETS_IMPORT_SAVED_IO_TIMEOUT_SECONDS` (default 120) bounds HTTP waits; it
does not grant a lease extension or authorize retrying an uncertain copy.

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
The builtin importer identity remains write-only (multipart create, part,
complete, abort and conditional empty PUT). For a registered S3 destination,
Source resolves the exact saved target and streams the conditional multipart
copy itself. Its configured connection principal needs those write operations
and `s3:GetObject` / `s3:GetObjectVersion` for independent verification, scoped
to the saved managed-original prefix. The importer does not hold that
principal and cannot use its scoped import tokens for unrelated Source I/O.
Source durably records write evidence; an unavailable or ambiguous copy
response never authorizes retrying the request or falling back to local I/O.
The importer retries a PostgreSQL original-record statement only on a confirmed
lock rollback (`55P03`). Persistent contention leaves the current import
entry pending for lease recovery, rather than reporting a file read error.

Transient `dataset_busy` heartbeats retry the same lease only until its last
confirmed expiry, including the renewal call itself. Lease loss, cancellation
and uncertain renewal stop I/O and leave recovery to the lease protocol.

Build the worker image from the repository root with:

```sh
docker build -f apps/assets-source/Dockerfile.import .
```

Each writer uses multipart parts of at most 64 MiB. S3's 10,000-part limit
allows objects up to 625 GiB; larger entries fail before destination upload,
even if the accepted job budget would otherwise permit them. The accepted
per-job byte budget and 16 MiB manifest limit are unchanged.

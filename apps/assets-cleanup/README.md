# Assets cleanup worker

This package independently consumes five Assets cleanup lanes: the committed object-deletion outbox, image-prefix garbage leases, Native pyramid saved-output retirement, accepted-image-input attempt tombstones, and accepted-video-input attempt tombstones. It has no API application, ORM, Celery, Redis, or broker dependency. The function-only PostgreSQL LOGIN is limited to eleven owner capabilities: the four existing outbox/image-garbage functions, two accepted-image-input garbage functions, two accepted-video-input garbage functions, and three saved-output retirement functions (claim, prepare page, complete page). Each lane keeps its own failure handling so one unavailable storage scope does not prevent the others from running. Image claims use `aap-assets-images/v1`; accepted-image input claims use `aap-assets-inputs/v1`; video claims use `aap-assets-video-input-execution/v1`. Native input claims perform bounded retention sweeps internally, so cleanup has no system-sweep permission. Existing whole-pyramid cleanup rows wait until their producer's `not_before` deadline and remain retained for periodic resweeps; image and input attempt prefixes are deleted only from explicit owner-issued leases.

The media-cache lane validates only `image-pyramids/{asset_id}/`, `image-pyramids/{asset_id}/gN/`, `image-pyramids/{asset_id}/gN/a{lease}/`, and `image-assets/{job_id}/gN/a{lease}/` prefixes. It lists/deletes in bounded pages of at most 1,000 keys, checks partial-delete failures, and acknowledges only after the exact prefix is gone. The accepted-image lane receives only native image attempt prefixes and delegates version-aware cleanup to `S3InputStorage`. The video lane receives only `accepted-video-inputs/{input_id}/g{generation}/a{lease_token}/` prefixes and uses `S3VideoInputStorage` with a separate video-only allowlist. Both input adapters abort multipart uploads, list versions and delete markers under their exact attempt, and send individual VersionId-scoped deletes; neither deletes a current key.

The Native lane does not use a local object-store credential. It runs only in this independent process; API and Celery have no Native cleanup fallback. It claims one saved output, asks Source to sign the exact prepared member page, verifies each member's recorded receipt with a conditional full GET (size, SHA-256, ETag and recorded version), deletes only that exact object/version with the signed conditional request, and then proves absence with a conditional HEAD. A saved member that was written but not verified is retirable only after its own producer operation has provably drained and the write closure has removed that operation's permit; a known receipt alone is not enough, and `submitted` or `unknown` members are never admitted. Raw version literal `null` remains distinct from an absent version. After the page is empty it obtains bounded ListObjectsV2, ListObjectVersions and ListMultipartUploads proofs for the exact saved prefix: any entry, delete marker, upload or truncated page refuses completion, and foreign material is left untouched. Multipart listing has a documented provider-dependent prefix-filter limitation, so this is a bounded exact-ownership proof rather than a promise that one prefix-filtered upload listing is sufficient on every peer. Unknown page preparation, a lost acknowledgement, cancellation drainage failure or unknown I/O keeps the durable responsibility; lease expiry or process loss is not evidence of physical closure. Public URLs keep their frozen retention bound before retirement.

Each lane commits its database claim before object-store I/O and acknowledges only after its exact cleanup succeeds. Failed outbox rows or leases remain retryable; accepted-input tombstones are retained for later rescans, so late versions and abandoned multipart uploads are removed by subsequent owner-issued claims. The outbox keyset cursor advances past failed rows during each scan so one persistent object error cannot block later records.

## Configuration

The process reads only these explicit settings. It never falls back to `DATABASE_URL`, general MinIO settings, ambient AWS credentials, or API configuration.

| Setting                                                             | Required | Default     | Purpose                                                                                                                                                   |
| ------------------------------------------------------------------- | -------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ASSET_CLEANUP_DATABASE_URL` or `ASSET_CLEANUP_DATABASE_URL_FILE`   | yes      | —           | PostgreSQL URL for a minimal cleanup LOGIN.                                                                                                               |
| `ASSET_CLEANUP_BUCKET`                                              | yes      | —           | Bucket accepted from existing pending outbox rows.                                                                                                        |
| `ASSET_CLEANUP_INPUTS_BUCKET`                                       | yes      | —           | Versioned bucket used by the input runtime; match `ASSETS_MEDIA_INPUTS_BUCKET` and keep distinct from datasets, legacy, cache and `ASSET_CLEANUP_BUCKET`. |
| `ASSET_CLEANUP_S3_ENDPOINT`                                         | yes      | —           | Explicit S3-compatible HTTP(S) origin.                                                                                                                    |
| `ASSET_CLEANUP_S3_ACCESS_KEY` or `ASSET_CLEANUP_S3_ACCESS_KEY_FILE` | yes      | —           | Dedicated list/delete credential.                                                                                                                         |
| `ASSET_CLEANUP_S3_SECRET_KEY` or `ASSET_CLEANUP_S3_SECRET_KEY_FILE` | yes      | —           | Dedicated secret for that credential.                                                                                                                     |
| `ASSET_CLEANUP_S3_REGION`                                           | no       | `us-east-1` | S3 signing region.                                                                                                                                        |
| `ASSET_CLEANUP_POLL_SECONDS`                                        | no       | `5`         | Delay between full outbox scans.                                                                                                                          |
| `ASSET_CLEANUP_PAGE_SIZE`                                           | no       | `100`       | Keyset page size, from 1 through 1000.                                                                                                                    |
| `ASSET_CLEANUP_NATIVE_STORAGE_URL`                                  | yes      | —           | Process-reachable Source origin that signs saved-output retirement requests.                                                                              |
| `ASSET_CLEANUP_NATIVE_STORAGE_TOKEN` or `..._TOKEN_FILE`            | yes      | —           | Source cleanup-role token; same value as `ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN`, but stored in a file readable only by this runtime user.           |
| `ASSET_CLEANUP_HEALTH_HOST`                                         | no       | `127.0.0.1` | Health server bind address; must be IPv4 loopback.                                                                                                        |
| `ASSET_CLEANUP_HEALTH_PORT`                                         | no       | `8765`      | Loopback-only readiness port.                                                                                                                             |

Mount database and object-store credentials as files where the runtime supports secret mounts. The existing cleanup identity needs `ListBucket` constrained to `image-assets/*` and `image-pyramids/*` in the configured media bucket and `DeleteObject` under those prefixes. It does not need object reads, writes, bucket administration, source access, or access to another bucket.

The Native lane performs no direct object-store I/O and receives no Native object credentials. Source's separate builtin Native identity holds exact-prefix `GetObject`/`GetObjectVersion`, `PutObject`, `DeleteObject`/`DeleteObjectVersion`, bounded `ListBucket`/`ListBucketVersions` and bucket-scoped `ListBucketMultipartUploads` for its saved output scope; external registered targets keep their own credentials inside Source. Do not add Native object reads or deletes to the ordinary four-lane cleanup identity, and do not give it the Source cleanup token used by another runtime.

The existing cleanup S3 identity may additionally access the separate `ASSET_CLEANUP_INPUTS_BUCKET`. Allow `GetBucketVersioning` and bucket-scoped `ListBucketMultipartUploads`; constrain `ListBucketVersions`, `DeleteObjectVersion` and `AbortMultipartUpload` to `accepted-inputs/*` and `accepted-video-inputs/*`. The local MinIO policy parser does not support a prefix condition on multipart listing; the runtime first lists the leased prefix, then uses bounded bucket-list pages when that peer treats Prefix as a complete object name. It filters every abort to the exact owner-issued attempt, leaves other uploads intact, and keeps abort/delete permissions restricted to the two accepted namespaces. MinIO also requires `s3:DeleteObject` for version deletion; condition it on `s3:versionid` being present, nonempty and not `null`. Do not allow unversioned `DeleteObject`, multi-object delete, `GetObject`, `PutObject`, or access to any other bucket. The input bucket must have versioning enabled and must match the bucket configured on the Assets Media control/worker. The image cleanup adapter remains limited to image prefixes; it does not accept video prefixes.

The worker has separate bucket configuration for existing media-cache cleanup and accepted-input cleanup, while using the existing cleanup S3 credential identity with narrowly restricted actions on each bucket. The input cleanup bucket must equal `ASSETS_MEDIA_INPUTS_BUCKET` (normally sourced from `MINIO_INPUTS_BUCKET`) so the cleaner retires the same exact-version objects accepted and consumed by the image and video workers. When changing either cleanup bucket, keep its old bucket and applicable prefix permissions available until pending work and retained tombstones have drained; add the new bucket with its narrowly scoped policy before switching.

At startup, the worker verifies that its login is a LOGIN without elevated attributes or role memberships, has no application table/sequence privileges, has no other module capability grants, and can execute exactly the eleven cleanup functions. It creates the Native HTTP client from its required URL and token; Source must be reachable and configured with the matching cleanup token and Native signer. A partial Source Native configuration refuses Source startup, and an unconfigured signer fails the Native lane explicitly instead of selecting another credential. The worker probes the media bucket through the two allowed image prefixes and verifies that the separate accepted-input bucket has versioning enabled before reporting ready.

`GET /health/live` reports process liveness and `GET /health/ready` reports whether the loop has passed its database and storage checks. Both bind only to loopback inside the container. SIGTERM stops pagination between S3 pages and exits without acknowledging a partially deleted prefix.

## Build and run

Build from the repository root. The image contains the cleanup worker, portable Assets contracts, and only the input database, garbage-collection and exact-version storage runtime modules. Accepted-image cleanup is owned by `yoao_assets_inputs_runtime.garbage`, independently of the input materializer and its original-read admission dependencies. The image does not install API/application modules, image/video execution workers, or media image-generation extras such as Pillow or libvips:

```sh
docker build -f apps/assets-cleanup/Dockerfile -t aap-assets-cleanup:local .
```

For Compose, `ASSET_CLEANUP_INPUTS_BUCKET` follows `MINIO_INPUTS_BUCKET` (default `accepted-inputs`), matching the Assets Media workers. For a direct process, set `ASSET_CLEANUP_INPUTS_BUCKET` to the same dedicated, versioned bucket. Grant the existing cleanup S3 identity only the version-scoped input actions above. The Native lane is configured through `ASSET_CLEANUP_NATIVE_STORAGE_URL` and `ASSET_CLEANUP_NATIVE_STORAGE_TOKEN` (or its file); both are required, and this overlay mounts the token as its own secret. Combine the Source, Media and Native overlays so the Source signer and the other callers are configured:

```sh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.assets-source.yml \
  -f docker-compose.assets-media.yml \
  -f docker-compose.assets-cleanup.yml \
  -f docker-compose.assets-native.yml \
  up -d --build assets-source assets-cleanup
```

The Compose `assets-cleanup` container runs as UID 10001 and must be able to read only its own `.secrets/assets-cleanup-native-storage-token` file. The Source container reads the three signer tokens plus the separate Native S3 key pair as UID 10003; do not widen file modes or share the Source signer token files with cleanup.

The worker has no exposed host port; the loopback readiness endpoint is used by the container health check.

## Contract tests

From this directory:

```sh
uv run --locked --extra worker --group dev pytest
uv run --locked --extra worker --group dev ruff check .
uv run --locked --extra worker --group dev ruff format --check .
```

The whole-asset protocol remains frozen in `../api/alembic/sql/assets.contracts.json` (`0210_asset_storage_cleanup.schema.json`); its four-field row and timestamped keyset-page wire are unchanged. Image garbage uses strict `aap-assets-images/v1` claims with only a garbage ID, exact prefix and lease token. Native saved-output retirement uses the same native envelope and the three saved-output functions (claim, prepare page, complete page); it has no configured-store field, and its page carries only immutable member IDs and receipts from the owner. Accepted-image cleanup consumes strict `aap-assets-inputs/v1` rows. Accepted-video cleanup consumes strict `aap-assets-video-input-execution/v1` claims with only a garbage ID, attempt prefix and lease token; the worker never receives source URLs, current source keys or arbitrary bucket names.

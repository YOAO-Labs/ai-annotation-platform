# Assets media

This package contains the independent video, image and accepted-input runtimes.
The portable `aap-assets-media/v1`, `aap-assets-images/v1` and
`aap-assets-inputs/v1` contracts import only Pydantic and the standard library;
their native installations are owned by migrations 0219, 0220 and 0221.

Video, image and input HTTP routes share one control process and one function-only
control LOGIN. The process verifies the full current control/system function set
(including Native read admission) before becoming ready. The video worker LOGIN
has its own restricted execution and sweep set. Image execution uses a dedicated
polling worker with 16 image and six input functions, its own SQL LOGIN and its
own S3 credentials. The image worker uses
Pillow for small thumbnails and the bounded libvips child process for pyramid
generation. Neither worker imports the API ORM, Celery or Redis.

Original-read permits and cache-output ownership are separate: an ordinary
image or video job can read a fixture-owned original while writing only to its
ordinary cache. Each output write requires its worker's matching execution
scope; fixture-native executions require the fixture writer and its private
target. Missing or mismatched output authority fails closed, and fixture source
reads retain their existing settlement and uncertainty fences.

Native pyramid attempts allocate their saved output and media-cache target at
claim time, before any member write, and publish only after every reserved
member returns its actual ETag/VersionId receipt. Each reservation's ordinary
PUT executes only the Source-issued reserved-key request; the worker validates
the raw HTTP ETag/VersionId, records that receipt, then fully reads back the
recorded saved version (size, digest, ETag, version) before it verifies the
member. Fixture-native writes keep their existing writer/private-target receipt
and full-byte proof, and are never re-read.

The saved target, not the current media-cache binding, owns each output's
endpoint, bucket, prefix and storage identity. Switching the management
binding from one location to another sends new outputs to the new location
while earlier outputs continue to read and retire at their saved location; no
separate Images or Cleanup bucket reconfiguration is involved. Editing the
registered location of the old target is not a retargeting operation: existing
outputs still require their saved endpoint, bucket, prefix and storage identity
to match, and a saved location that no longer matches the registered
location/connection is refused rather than redirected. No configured-cache
bucket, ambient credential or reconstructed key is ever substituted for a
Native read or write.

The Source process is the only storage-credential owner and finite signer.
Ordinary callers hold only operation-scoped tokens:

- image worker (writer): `ASSETS_IMAGES_NATIVE_STORAGE_URL`,
  `ASSETS_IMAGES_NATIVE_STORAGE_TOKEN` or `..._TOKEN_FILE`
- media control (reader): `ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL`,
  `ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN` or `..._TOKEN_FILE`
- Assets Cleanup (cleanup/retirement): `ASSET_CLEANUP_NATIVE_STORAGE_URL`,
  `ASSET_CLEANUP_NATIVE_STORAGE_TOKEN` or `..._TOKEN_FILE`

A partial Native token/URL configuration fails startup; an installation with
no Native signer configuration never falls back to another signer. Source holds
the three distinct tokens plus its separate Native S3 identity
(`ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN[_FILE]`,
`ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN[_FILE]`,
`ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN[_FILE]`,
`ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY[_FILE]`,
`ASSETS_SOURCE_NATIVE_S3_SECRET_KEY[_FILE]`). Registered connection targets
keep their own secrets inside Source. Deployment must provision these settings
and the matching IAM policy; the repository defaults do not.

For Compose, combine `docker-compose.assets-native.yml` with the Source, media
and cleanup overlays. The Native overlay mounts the writer binding into the
image worker and the API-local image executor, the reader binding into shared
control, and the signer configuration into Source; the cleanup overlay carries
its own required cleanup binding. Each caller file is readable only by its own
runtime user; token values match the Source role token without sharing the
Source files. Cleanup requires its URL and token even when automatic pyramid
generation stays disabled, so running that worker also requires the Source
signer. Attach `infra/docker/native-source-minio-policy.json` to the separate
builtin Native identity; do not extend the ordinary media or cleanup
identities with Native object reads or deletes.

Native generation and asset references use the selected saved output, a
durable read permit, saved member receipts, Source-issued inspection requests,
existing strict manifest validation and a final owner/source fence. Inspection
(`purpose=inspect`) completes before URL publication (`purpose=publish`) so a
slow manifest read does not consume the advertised URL lifetime, and a permit
is settled even when the read fails. Public references expose only URLs and
report the shortest constituent lifetime for combined original/thumbnail and
overview/tile references. `expires_in` defaults to null so the owner selects
its bounded lifetime; an explicit requested lifetime is returned exactly, and
an explicit request above the frozen policy bound is refused instead of
silently clamped. Fixture-owned outputs remain on the fixture read owner;
production has no fixture resolver and therefore refuses them.

The retained Celery `generate_image_pyramid` callers (ordinary-thumbnail
auto-generation, the operator backfill script, and fixture-independent
execution) are not replaced by this path; they allocate through
`acquire_generation_lease` and publish through `publish_generation`, which
require the live lease, current generation and `building_generation` fence
before setting `output_id`. No retained caller may publish `ready` with an
unowned output.

`ready_source_reference` is the strict reference path for callers that have
already admitted and verified output work. Its video and image HTTP routes are
`/v1/ready-source-reference` and `/v1/images/ready-source-reference`. A stale
publication or missing cached object fails without invalidating or scheduling
repair work. Ordinary `source_reference` retains its existing lazy-repair
behavior. Fixture originals deliberately have no ordinary original URL;
fixture callers verify original bytes through their existing scoped owner.

Image backfill uses `only_missing` to check the selected output before admitting
work. A thumbnail job probes unknown dimensions itself and publishes the
thumbnail and dimensions together. For known pyramid-eligible dimensions,
thumbnail backfill preserves automatic pyramid generation when enabled; its
`only_missing` check uses the current pyramid generation. Without that policy,
an ineligible thumbnail job terminates with `not_eligible`. A pyramid overview
is a separate generation output and does not itself publish a thumbnail path.
Backfill `ready` means the scan and child admission finished, not that all
children succeeded; consumers must inspect the child or its current output.

The image worker also materializes accepted input bytes into the dedicated,
versioned `ASSETS_MEDIA_INPUTS_BUCKET`. Its normal poll processes both lanes;
`--once` processes at most one claim, preferring an input claim. `--job-id` selects
an image job and `--input-id` selects an input reservation. These selectors are
mutually exclusive. Input URLs bind a real S3 VersionId and the recorded digest;
materialization never falls back to a newer source after acceptance. The input
control exposes only `/v1/inputs/find`, `/accept`, `/record`, `/reference` and
`/verify-source`; worker and private source operations are not HTTP routes.

Acceptance-history source fencing is a same-transaction internal Assets owner
capability, packaged as `sql/0430_acceptance_source.sql`. It returns the existing
DatasetItem source snapshot, generation and object identity under a bounded row
lock; only the Labeling owner may execute it. It is not a media HTTP route and
does not expand any runtime LOGIN or private table privileges.

The optional `apps/assets-media/Dockerfile` targets are `control`, `worker` and
`image-worker`. The shared control app is defined in
`docker-compose.assets-media.yml`; its `assets-images-worker` service uses an
explicit image database URL, image S3 identity and legacy annotation bucket.
The default Compose stack remains unchanged. `IMAGE_PYRAMID_AUTO_GENERATE`
defaults to false; enabling it changes admission behavior and should follow the
image capacity runbook.

Video poster and exact-frame extraction each use one decoder, filter and encoder
thread, rather than FFmpeg's host-CPU-based defaults. Container PID, CPU and memory
limits remain independent constraints. A failed FFmpeg command keeps its stable
public error code and writes a bounded stderr tail to the owning process log;
that diagnostic can contain source paths and is not a public response.

Video has passed isolated local HTTP/S3/FFmpeg and bounded container acceptance
with separate restricted identities. Image runtime integration does not itself
qualify database migrations, production S3 permissions or sustained image
capacity. The broader ownership and remaining work are tracked in the
[backend module plan](../../docs/plans/1792160873_backend-module-decoupling.md),
[ADR-0105](../../docs/adr/0105-independent-video-media-owner.md) and
[ADR-0106](../../docs/adr/0106-independent-image-media-owner.md). Accepted-input
consumer integration and its required local qualification remain tracked in
[ADR-0107](../../docs/adr/0107-versioned-accepted-media-inputs.md).

Retained video inputs have a separate portable contract and 24 native capabilities.
Their eight new Assets/Labeling state tables, leases, frame plans, lineage pins
and delayed garbage partitions are qualified against owned PostgreSQL resources.
Frame rows are requested lazily. Ten private control routes under
`/v1/video-inputs` and the `VideoInputClient` share the existing control process.
The existing video worker fairly rotates media, original-input and frame lanes;
`--job-id`, `--input-id` and `--frame-id` are mutually exclusive, and `--once`
executes at most one claimed unit. It verifies its exact SQL profile before
creating storage clients. Its byte transport uses only the existing Media
settings; it does not require image-worker environment variables.

Restricted real processes and an independent Node caller qualify original-video
retention, frame extraction, all ten HTTP operations, exact-version reads,
source replacement, lost versions and Cleanup multipart/late-version rescans.
A revoked requester terminalizes only its queued unit and does not block later
claims. Prediction/overwrite/failure-retry consumers are not migrated yet; this
process qualification does not establish that complete application slice. See
[ADR-0109](../../docs/adr/0109-retained-video-inputs-and-frame-plans.md).

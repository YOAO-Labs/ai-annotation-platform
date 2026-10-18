# ADR-0106: Independent image materialization and source-bound delivery

- Status: Proposed
- Date: 2026-10-19
- Deciders: implementation team

## Context

Video execution has an independent Assets Media runtime. Image thumbnails, image
dimensions and the complete pyramid workflow still use API ORM, storage and
Celery. Their public Python port does not cover acceptance or execution. Current
thumbnail keys are mutable; pyramid publication checks a lease token but not its
expiry or current source binding. Task unlink already delays prefix deletion
until known writer leases expire, but completed deletion records do not retain
responsibility for late writes. Project deletion does not durably queue those
prefixes. These paths must move with image reads and technical projections.

DatasetItem dimensions are registered source facts as well as probe results.
Useful imported dimensions must not disappear merely because no thumbnail has
been materialized. Legacy direct Tasks own a different source: their object is
under the fixed annotations bucket, and their thumbnail fields belong to
Labeling. Task workflow status is not an image-source revision.

## Decision

Extend the existing `apps/assets-media` artifact with an independent image
contract, image control handlers and a dedicated image execution process. Keep
the frozen video protocol unchanged. Reuse Python, Pillow, blurhash and libvips;
no additional language or new control-service pair is needed. Image execution
keeps its own low-concurrency process and restricted SQL/S3 identities.

```text
Public API authentication and scope
  -> Assets Media image control -> Assets image native operations
         |                           -> Identity / Workspace / Labeling facts
         -> conditional source HEAD/GET and source/derived references
Source or Labeling upload transaction -> native image acceptance / durable intent
Image worker -> native claim / heartbeat / publication
             -> conditional source read -> local Pillow or libvips child
             -> immutable derived PUT -> source and lease CAS
Assets Cleanup -> exact prefix/key tombstones -> derived object deletion
```

The image wire identifier is `aap-assets-images/v1`; image HTTP routes have their
own namespace in the existing control process. Core types depend only on
Pydantic and the standard library. Native and HTTP responses use strict typed
envelopes, UUIDs, explicit source selectors and captured image policy. Requests,
replies and record pages retain the existing 8 MiB, 16 MiB and 15 MiB/500-record
budgets. Tile references accept logical coordinates, never caller-supplied keys.

The image native capability families are:

| Role                | Operations                                                                                                                                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Current actor       | `read_image_source`, `observe_image_source`, `read_image_summaries`, `prepare_image_media`, `read_image_generation`, `read_image_work`, `retry_image_media`, `invalidate_image_media`, `cancel_image_media` |
| Trusted composition | `resolve_internal_image_source`, `schedule_image_media`, `read_internal_image_work`, `scan_image_backfill`, `read_image_usage`                                                                              |
| Image execution     | `claim_image_media`, `heartbeat_image_media`, `finish_image_media`, `fail_image_media`, `advance_image_backfill`, `reconcile_image_media`                                                                   |
| Derived cleanup     | `claim_image_garbage`, `complete_image_garbage`                                                                                                                                                             |

The shared control login gains fourteen image capabilities; the video execution
login keeps its ten. A separate image execution login receives only six image
capabilities. The cleanup login gains two scoped image-garbage capabilities. No
login gains private table access, other-owner schema access or DDL. The image
worker has conditional read-only source access to datasets and the fixed legacy
Task bucket, plus derived PUT/GET/LIST. Assets Cleanup owns derived deletion.

Source and connector-import producers use two trusted image capabilities on
their existing transaction connection: resolve/bind the pre-observed published
source, then admit its captured-policy work. Their published Source/import wire
shapes remain unchanged. Independent producers read the existing
`IMAGE_PYRAMID_*` configuration names; the API binding captures its validated
settings. Frozen thumbnail source limits are not configurable.

Migration 0220 adds image jobs, immutable operation receipts and retained image
garbage responsibilities. Jobs are authoritative for accepted work, attempts and
terminal state; existing pyramid Asset/Generation rows become the atomically
maintained delivery projection. There is no second independent scheduler.
Migration and SQL artifacts through 0219 remain unchanged. Existing accepted
legacy pyramid writers must drain before migration; their original source scope
and authority cannot be reconstructed after the fact.

DatasetItem images reuse existing source generation and object identity.
Labeling gains a dedicated Task source generation, object identity and drift
state, plus native source/access and thumbnail-publication capabilities. Its
generation advances on source path/type, project or DatasetItem-binding changes;
ordinary Task workflow or assignment changes do not retarget image input.
Upload completion captures the observed identity and durably admits image work
or its delivery intent in the owning transaction. Publication rechecks current
source facts, existence, generation, unexpired attempt lease and exact output
scope. A Task that becomes linked cannot receive a legacy Task-source result.
DatasetItem-owned work remains shared across Tasks and projects.

First observation binds a registered source. Later unexpected byte identity
changes mark drift and prevent implicit adoption. Reads and downloads use
captured ETag/version/size and final identity checks. No business/source row lock
spans object I/O. Accepted work survives initiating-account disable or role loss;
explicit cancellation and changes to its owning source fence later publication.

Backfill acceptance freezes its selection and cutoff. Its worker first receives
a bounded source page and batch token, then observes objects outside SQL locks.
An observation commit compares that page's source snapshots, binds identities
and admits child jobs atomically. The cursor advances with a retained batch
receipt so a lost response cannot skip or duplicate source admission. Storage
outages defer this phase; they do not turn into missing-source skips.

Each accepted operation captures `ImagePolicy`, including the existing resource
and eligibility thresholds. Small thumbnails retain EXIF transpose, RGB,
256-pixel longest side, LANCZOS, WebP quality 80 and 32x32/4x3 blurhash. Pyramids
retain libvips sequential normalization, autorotation, sRGB/ICC, alpha,
multi-page rejection, WebP quality 88 and full-resolution-first 512/1 tile
geometry. Large sources never fall back to Pillow pyramid generation. A bounded
child process owns libvips work and must stop before cancellation is acknowledged.

Output prefixes include an attempt token. Public pyramid generation numbers and
`aap-image-pyramid/v1` manifests remain stable; storage prefixes stay private.
Failed refreshes preserve usable old output without hiding their error. Source
dimensions and thumbnail/pyramid readiness are separate facts. Missing objects
are conditionally invalidated and durably repaired rather than signed blindly.

Existing whole-asset deletion remains in `asset_storage_deletions`. Extend its
newly queued responsibilities with lease-aware delays and retained resweeps;
preserve the existing row wire and completion acknowledgement. New attempt and
thumbnail scopes use image garbage leases. Source deletion, Dataset unlink,
project deletion and explicit Task removal queue cleanup atomically and use the
same source fences. Business references retain the existing 409 detach rule.
Raw sources and uncertain legacy thumbnail keys remain retained.

## Consumers and qualification

This is one image slice spanning more than eight files. It includes Task and
DataManager summaries, file URLs, Catalog/dashboard image references, the three
pyramid endpoints, upload/import relays, thumbnail/dimension/pyramid backfills,
seed/status tools, compatibility Celery registrations and cleanup. Frozen source
admission for already accepted inference/tracker/training/evaluation inputs is a
separate prerequisite; replacing their captured input with a current URL would
retarget work. General Mask, training, export, avatar, comment, bug-report, guide,
audit and spatial storage lifetimes remain their owners' work. Unconditional API
bucket provisioning remains visible in the whole-module ledger.

Qualify real conditional S3 reads, source changes during decode/upload, expired
or superseded leases, Task relinking/deletion, actor changes, response loss,
duplicate acceptance, process takeover and late output cleanup. Compare actual
thumbnail pixels/blurhash and pyramid grids/EXIF/ICC/alpha output against the
existing algorithms. Validate complete batches and backfills beyond one page,
public response/cache/ETag behavior, restricted native logins, clean core wheels,
independent images and resource limits. Run all database-backed checks only in
verified owned disposable environments. Do not infer production capacity from
small fixtures.

The current UI can skip a manifest read when a required large image has no
summary and then fall back to the original image. Its resolution is being
confirmed separately while independent backend work proceeds; it is not counted
as a passed browser gate.

This decision does not complete Assets or the whole-module goal. No default Go,
production rollout, push or release is implied.

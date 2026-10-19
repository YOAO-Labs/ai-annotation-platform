# ADR-0109: Retained video inputs and source-bound frame plans

- Status: Accepted; implementation in progress
- Date: 2026-10-19
- Deciders: implementation team

## Context

Frame preannotation creates a durable Job but does not capture its video source.
The worker obtains current frame counts, and both extraction and segment
execution reload the current Task and DatasetItem. Internal frame requests carry
only the item ID and rendition. A relink or replacement can retarget accepted
work, including mixing generations while collecting a segment's URLs.

Overwrite currently clears predictions before video preparation. Empty or
unavailable current metadata can then finish as `no_frames`, having removed the
old predictions. Transient write contention retains its original prediction
result, but other frame failures only increment statistics. Existing frame-index
deduplication does not distinguish source generations, and the general failed
prediction path does not have original video provenance.

The qualified image input and image manifest contracts explicitly require image
sources. DatasetItem media snapshots also exclude timetable revisions. Neither a
fake ImageSource nor an expected DatasetItem snapshot alone fixes this boundary.

## Decision

Add `aap-assets-video-inputs/v1` within the existing Media artifact and processes.
Use the dedicated versioned input bucket and reuse binary transfer, source
verification, video decoding and exact-version cleanup primitives. Use the
separate `accepted-video-inputs/{input}/g{generation}/a{lease}/` prefix. Do not add
a service or a language, or widen the deployed image-only contracts.

Capture bounded video manifest membership in the Job submission transaction.
The member contains Task version and source generation/snapshot, project and
DatasetItem link, raw Task path/type, and the DatasetItem source generation,
snapshot, object identity and timetable revision. Add narrow Labeling and Jobs
projections; Assets must not directly SELECT their private tables. Project/batch
membership remains complete and paged, rather than an unbounded broker map.
Frame sampling policy is fixed at admission instead of re-reading worker config.

Materialize the exact original video version, measure its SHA and size, and
derive video facts and the selected decoded-ordinal-to-PTS entries from those
retained bytes. Known source identity and timebase facts must match. Previously
unknown object identity is captured at the first successful HEAD; do not claim
that unknown historical bytes were frozen at HTTP submission. Captured Task,
source or timebase changes fail explicitly, without selecting the current file.

Seal only after each member has original bytes and a complete frozen frame plan,
or a fixed member failure. Job claim, fan-out and per-Task overwrite follow this
barrier. Failed members keep their previous predictions. Keep existing selection
semantics: decoded frame ordinals start at zero, step applies before truncation,
and the cap counts selected frames. Frame geometry continues using `frame_index`.
PTS is signed integer evidence; do not reconstruct it from average FPS or require
monotonic rounded timestamps. Decoding uses the first video stream.

Frame work reads a sealed member/input/plan and derives from its retained blob.
Publish attempt-scoped, versioned frame objects under the video-input prefix;
do not place accepted results in the mutable DatasetItem-keyed frame cache.
Requests carry identifiers and bounded ranges. Receipts retain the original
input, decoded ordinal, PTS/timebase, rendition and measured frame object version
and SHA. Missing retained versions fail rather than use a current source URL.

Carry that provenance through predictions, failures, retry and candidate
adoption. Final Labeling writes keep current actor/project/Task/batch/editor and
real Job checks, and fence the accepted Task link/source, DatasetItem generation
and timebase. Add video-specific reference and Annotation pin projections; do
not interpret an untyped input UUID as evidence for the image ledger. Current
request actors may differ from the original retained input actor. Values are
attribution and integrity checks, never authorization tokens.

The existing accepted-input garbage ledger can hold immutable video attempt
prefixes. Extend its exact allowlist and make garbage claim sweep video expiry
as well as image expiry. Referenced videos and frames remain retained; expiry,
pins, claim/ack, late versions and multipart uploads need the same real-storage
race qualification as image inputs. Keep Cleanup's narrow ownership.

## Portable value foundation

The current foundation publishes values, their JSON Schema and an optional
video/frame versioned storage adapter. The adapter shares the existing binary
transfer implementation while retaining a separate video key/prefix allowlist
and typed video/frame receipts. The existing image adapter does not gain video
write or cleanup scope. Native admission, HTTP operations and consumer migration
are still required; no video path is activated by importing the package. All SQL
through 0222 stays frozen; new native capabilities start at 0223 or later.

Values include a ready original input, measured video facts, sampling policy,
sealed frame plan, complete ordered time entries/pages and exact-version frame
references. All protocol integers fit the exactly representable JSON integer
range, and decoded frame indices retain their signed 32-bit upper bound. Pages
contain at most 100 entries, with 64 KiB requests and 2 MiB replies. Sampling
counts use arithmetic before iteration, so a capped plan does not allocate an
array for every source frame.

Timebase hashing streams every selected entry and checks count and decoded
ordinal continuity. Full-page verification rejects missing, repeated, crossed or
changed time entries before execution. Digests bind logical input/source and
sampling identity; canonical native records still own blob locator and authority
checks. Schema budgets do not substitute for transport enforcement.

The seed is the following ordered ASCII fields, separated and terminated by LF:
protocol, input ID, origin Job ID, origin actor ID, Task ID, project ID, Task
version, Task source generation, Task source snapshot, Dataset ID, DatasetItem
ID, Assets source generation, Assets source snapshot, timetable revision,
original blob SHA, original byte size, measured frame count, width, height,
duration in milliseconds, video stream ordinal, maximum selected frames, step,
target width and format. UUIDs use lowercase canonical spelling; integers use
unsigned decimal without leading zeroes.

`timebase_digest` is SHA-256 of `timebase\n`, the seed, and each selected entry's
ordinal, frame index, signed PTS in milliseconds and keyframe flag (`0` or `1`),
each terminated by LF. `plan_digest` is SHA-256 of `plan\n`, the seed and the
lowercase hexadecimal timebase digest followed by LF. Different grouping of
entries into pages does not change either digest. Creation timestamps and
physical blob locators are provenance fields outside this logical plan digest.

## Qualification

The owner authority contract additionally publishes the following SQL interfaces
for the Assets owner. These are Labeling/Jobs projections, not direct table
grants or Media control operations:

| Interface                                      | Arguments                                                                                                                       | Reply schema                         |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `aap_labeling_v1.read_video_input_task_source` | Task ID, current actor ID                                                                                                       | `aap-labeling-video-input-source/v1` |
| `aap_labeling_v1.lock_video_input_task_source` | Task ID, current actor ID, expected Task binding JSON                                                                           | `aap-labeling-video-input-source/v1` |
| `aap_jobs_v1.project_video_input_job`          | Job ID, optional Task ID, origin actor ID, project ID, canonical selection JSON, optional selection digest, require-active flag | `aap-jobs-video-input-job/v1`        |

Source replies contain the request actor, current binding and upload readiness.
Lock compares all source binding fields except the ordinary Task workflow
version. Job replies distinguish a pruned Job from a present mismatched Job.
Historical FK nulling never grants active execution; retained project identity
must still match. The durable admission fields are
`accepted_video_inputs_protocol`, `accepted_video_manifest_id`,
`accepted_video_selection` and `accepted_video_selection_digest`; the selection
contains explicit batch/Task selectors, predict mode, execution scope and sampling.
New original Jobs use their own ID as broker ID and manifest ID, and explicitly
declare `execution_unit=frame`. Legacy rows cannot invent these accepted facts.

`authority.schema.json` freezes the request/result shapes, SQL argument order,
caller owner and response envelopes for these interfaces. Until native
installation and qualification finish, this remains a contract specification.

The values foundation requires strict malformed/crossed value rejection,
source/timebase digest fences, incomplete/changed page rejection, exact-version
frame references, large-count bounded planning, packaged schema and independent
Node digest reproduction, including variable and negative PTS. A cold base wheel
must import without API, ORM, HTTP, database or task-runtime dependencies.

The complete slice additionally requires native role/SQL and source-retention
proof; real versioned-video and frame bytes; bounded project/batch/explicit
capture; lost responses and lease takeover; source/timebase replacement; overwrite
ordering; per-frame failure and original-input retry; current authority and
terminal fencing; source-aware deduplication; Annotation evidence/GC races;
installed processes and Node HTTP consumption; migration phases; and affected
pipeline/Celery acceptance. Qualifying values alone does not complete the slice
or the whole independent-module goal. No production deployment or Go rollout is
authorized by this decision.

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
through 0224 stays frozen; new owner state and capabilities start at 0225 or later.

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

## Owner execution contract

`execution.schema.json` specifies the retained-input owner's operation matrix
separately from ready values and Labeling/Jobs authority projections. The
function-only database binding enforces that matrix; it does not install SQL or
activate a consumer. The owner ledger, process wiring and HTTP admission remain
implementation work until qualified together.

| Caller scope | Operations | Responsibility                                                                                                       |
| ------------ | ---------- | -------------------------------------------------------------------------------------------------------------------- |
| Workflow     | 1          | Capture complete manifest membership in the original Job transaction.                                                |
| Control      | 9          | Read header/member pages, observe/seal, read original records/private work/time pages, require/read a frame.         |
| Worker       | 9          | Claim, heartbeat, stage selected times, finish/fail original input; claim, heartbeat, finish/fail frame extraction.  |
| Labeling     | 2          | Fence the ready original input/plan before overwrite, and the exact frame/time entry/retained object before writing. |
| System       | 1          | Sweep eligible expired original inputs and their frame attempts.                                                     |
| Cleanup      | 2          | Claim and acknowledge only video attempt tombstones; the image claim remains image-only.                             |

Raw captured Tasks can retain a missing DatasetItem link as a failed member;
ready bindings always require that link. Captured Assets source facts are narrow
video values, without mutable metadata or image-owner predicates. Preparing a
member records either the first complete object observation or a fixed source
failure. A known object identity and the independent timetable revision must
still match. Pending inputs cannot assert a ready plan, and a ready member must
have a complete measured plan. `prepared_count` accounts for terminal ready or
failed members, not reservations still copying bytes.

Manifest `actor_id` attributes the original admission. Subsequent requests carry
the current actor separately; native operations must authorize that actor rather
than treating the original actor or a digest as a permission token. Capture
replay still matches the original actor and selection. The separate accepted
background Task-creation policy does not relax current prediction permissions.

Time staging is bounded to 100 contiguous selected entries and bound to the
input generation and lease token. Replaying a page returns that page's same
end cursor, even when later pages have been staged. Final publication must
verify all staged entries and their digest before setting the member ready.
Probe processing must count decoded frames without dropping missing timestamps
and renumbering later rows. Missing PTS for a selected decoded frame is a fixed
preparation failure; negative, repeated and nonmonotonic valid PTS remain valid.

Frame extraction has a separate lease and exact attempt key. It reads the
retained original version and does not re-download the current DatasetItem.
Frame replies bind the plan, decoded ordinal, time entry and rendition. The
Labeling fence additionally compares the exact expected object version and SHA;
historically ready records can remain readable after expiry but cannot grant a
ready write fence. Native retention must preserve references before sweeping.

## Portable manifest digests and native state

Admission and sealed outcomes use bounded SHA-256 hash chains. Each field is
encoded as `N;` for null, or `S<byte length>:<UTF-8 bytes>;` otherwise. Integers
use canonical decimal, booleans `0`/`1`, UUIDs canonical lowercase. Every hash
begins with its ASCII domain and LF. This distinguishes null from empty strings
and delimiter-containing paths without relying on database JSON formatting.

The `selection` seed fields are execution schema, Job, origin actor, project,
selected batch, execution scope, predict mode, sampling maximum/step/width/format,
explicit Task count (null for implicit selection), each sorted explicit Task,
candidate count and selected count. Each `member` hash includes previous digest,
ordinal; captured Task/project/batch/version/source generation/item/snapshot/path/
type; captured Assets item/dataset/generation/snapshot/timetable revision;
observed ETag/size/version (null when unknown); drift flag/path/type/declared size/
content hash; and the fixed capture error. The initial capture error is stored
separately from later preparation errors, so preparation never changes admission.

The `sealed` seed fields are execution schema, Job, origin actor, project,
selection digest, selected count, ready count and failed count. Each `outcome`
hash includes previous digest, ordinal, Task, state, fixed error, optional input
ID, and the ready plan digest/timebase digest/original SHA/size/selected-frame
count. All captured members and all final outcomes must appear exactly once in
UUID order. Byte expiry preserves ready outcome metadata and its digest.

Native state is authored in new SQL artifacts starting at 0225: manifest/member,
original input/attempt, selected time, and frame/attempt tables. They retain
immutable source receipts without live foreign-resource keys. Worker operations
must validate canonical membership, current authority/source, lease generations,
staged times and exact publication keys before changing state. Native timebase
verification bounds canonical selected-entry text to 64 MiB before constructing
its SHA-256 input; page/request/reply limits remain unchanged. The worker's
existing JSON spool budget is at least as strict for normal selected entries.
Migration 0225 now installs the native owner state and capabilities. This does
not activate retained-video admission in the process or prediction consumers.

The control SQL now captures complete UUID-ordered membership through Labeling's
cursor, computes the portable admission digest, and prepares each member against
current manager, Job, Task and Assets source facts. First observation may establish
an unknown object identity. A confirmed mismatch preserves the Assets drift marker
and a fixed member failure in the same transaction. Repeated preparation returns
the original receipt before resolving a current path. Capture replay also works
before Jobs stores the returned digest; subsequent preparation and execution
require that digest in the original Job. Sealing recomputes complete membership
and outcome chains after every selected member reaches a terminal state.

Read operations retain original transport facts and selected times. Frame requests
and Labeling write fences require a sealed manifest, the original plan, current
Task/source/timetable facts and current actor access. Write fences compare the
complete frame entry and exact blob version inside the caller's transaction.
These source fences are composed with Labeling's current Task/batch/editor and
actual execution-Job checks; they do not grant prediction-write authority alone.
Historical original-Job consistency remains distinct from the active retry Job,
so later authorized adoption can retain its original bytes after Job pruning or
ordinary Task workflow changes.
Lock contention rolls back partial changes and remains a transient owner error.
Owned PostgreSQL qualification passes 34 cases with no skips. It covers capture
paging, original identity, replay after sealing, committed drift, lease takeover,
atomic malformed pages, exact-version frame publication, strict role grants,
lineage retention, delayed retirement and migration rollback/reinstallation.
Frame rows are created lazily. Existing same-project annotation clones retain
the original evidence Task even when the clone belongs to another Task; direct
attachment still requires the target Task. Expiry preserves ready blob metadata
and terminalizes pending, failed or expired-lease frames without leaving leases.
The Media control/worker and Cleanup preflights match the expanded exact grant
sets. Real video S3, process and consumer acceptance remain required before
local cutover.

## Retained-byte processing runtime

The control library reads the captured member before external I/O and returns
an existing preparation outcome on replay. First preparation observes that
member's captured Assets path; it does not resolve a new current Task/item path.
The SQL owner must fence the current Task, Assets source and timetable revision
after the observation. Original verification checks the retained blob version and
HEADs the captured original path/identity before the Labeling transaction takes
the input/plan fence and performs overwrite. Private work reads return original
transport facts and never resolve a replacement Task path.

The video source-storage adapter passes only binary transfer facts to the shared
conditional downloader; it does not fabricate an image source or image-owner
predicate. Original materialization uploads and verifies the retained version,
then measures the identical local bytes and stages selected times in bounded
pages. Staging and finish retries retain the exact same observation, page,
measured blob and lease token after contention or response loss.

The processor streams every decoded frame from the first video stream and
counts rows independently of timestamps. It ignores metadata/FPS frame-count
estimates. Only selected time entries are spooled, under a finite disk budget.
Presentation time uses decimal milliseconds with nearest, ties-to-even rounding;
DTS never substitutes for missing PTS. Missing selected presentation time fails
preparation, while an unselected timestamp gap never renumbers later decoded
indices. Decoding excludes playlists and external network protocols.

Frame extraction downloads and hashes the exact retained original version,
then produces the captured JPEG/WebP rendition. It checks the published object's
complete key against the claimed attempt. A missing version fails explicitly;
a newer object at the same key and current DatasetItem URLs cannot replace it.
Heartbeat loss cancels active work and reaps FFmpeg/ffprobe before removing the
private workspace. An unconfirmed initial heartbeat starts no I/O and leaves
lease takeover to the owner instead of failing another writer's work.

The existing Media control process now exposes ten private HTTP operations under
`aap-assets-video-input-control/v1`. Capture, private source work, execution,
Labeling fences and garbage operations remain outside HTTP. The video worker
fairly rotates technical media, retained originals and retained frames using one
SQL pool and one owned S3 client pair. Explicit job/input/frame selectors are
exclusive; one-shot mode processes at most one confirmed claim. Its startup
checks the exact SQL capability set before creating storage clients. The
byte-only source adapter uses Media settings without requiring image-worker
environment variables. Cleanup polls a separate fourth video lane with its own
prefix validator and independent failure/acknowledgement lifecycle.

Owned real-process acceptance copies and probes an original video, publishes and
reads a decoded frame, checks all ten operations from an independent Node
consumer, recomputes timebase/plan hashes, rejects foreign namespace writes and
unversioned reads, preserves an older exact frame version when latest changes,
and rejects missing versions and changed originals. The real Cleanup process
retires both raw/frame attempts, aborts only its own multipart upload and rescans
late versions while preserving plan/blob receipts. The local object server does
not implement directory-prefix multipart listing; the byte transport can scan
bounded bucket pages and filter aborts to the exact leased attempt. The
[MinIO multipart implementation](https://github.com/minio/minio/blob/master/cmd/erasure-sets.go)
also documents its exact-object interpretation of Prefix. Source objects and
other attempts remain untouched.

Migration 0226 prevents a disabled or invalid-role requester from poisoning a
global claim queue. The affected input/member or frame becomes terminal and
the owner continues to the next eligible unit; busy authority/Task/source locks
remain transient. This current-authority rule does not change the separate
initial Task-creation authorization semantics.

Application prediction/overwrite, per-frame failure/retry and evidence consumer
migration, actual Celery execution and broader capacity acceptance still remain
before the complete slice and primary cutover.

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
caller owner and response envelopes for these interfaces. Migration 0223 installs
the three projections and their four private helpers. Local owned-database
qualification passes 24 native, role and migration cases, including visibility,
source ABA, workflow-version progression, busy locks, strict Job scopes and
historical FK nulling. Assets executes the projections without SELECT rights on
Task, Job, User or Project tables. Media control/worker grants stay unchanged.
Frozen SQL install/rollback, downgrade and upgrade pass; all scoped resources
are cleaned. These projections provide source facts for the next input-owner
implementation, without activating accepted-video execution.

Migration 0224 adds two Labeling-owned projections under
`aap-labeling-video-input-selection/v1`. `open_video_input_manifest_members`
returns a cursor identifier whose first row contains candidate and selected
counts and whose remaining rows contain captured Tasks ordered by UUID. It
streams complete implicit project/batch membership instead of placing all Task
IDs in the Job or an unbounded JSON array. `candidate_count` is measured before
`skip_predicted`; `selected_count` and cursor members are measured after it.
Explicit selections remain canonical and limited to 200 Tasks. A selected
missing DatasetItem link remains a cursor member so the input owner can seal it
as `source_missing`; mixed media, an invalid selector, or changed explicit
scope fails the whole cursor before any member is yielded.

`lock_video_input_manifest_member` rechecks the current project manager and
Task visibility before attempting contention-sensitive locks, then takes the
eligible batch `FOR SHARE NOWAIT`, the Task edit advisory lock and Task
`FOR UPDATE NOWAIT`. It compares captured source identity while ignoring the
ordinary Task workflow version, rechecks batch and `skip_predicted` eligibility,
and applies the existing editor-lock rules: bulk rejects every active editor
lock, while Workbench allows the current actor's lock and rejects another
actor's lock. Workbench requires one explicit Task and a null selection batch;
its current editable Task states and admin-lock checks are preserved. Missing
links produce a fixed `source_missing` error after scope checks, and uploading
Tasks produce `source_not_ready`. Both functions are executable by the Assets
owner only; their cursor rows and lock result are specified in
`0224_video_input_selection.schema.json`. The projections do not install an
input ledger or activate video admission.

The combined native, role and migration qualification passes 38 cases with zero
skips in owned disposable resources. It includes 205 complete implicit members
fetched in batches of 100, strict malformed selectors, missing links, source
ABA, ordinary workflow versions, Workbench editor locks, current authority
before Task contention, and exact public/private EXECUTE boundaries. Labeling
has the lock-holder column needed for its own editor checks; Assets still has
no Task, batch, editor-lock, Job, User or Project table/column access. Frozen SQL
install/rollback, downgrade to 0189, re-upgrade to 0224 and grant reapplication
pass. All verifier PostgreSQL/Redis/buckets/storage identities are cleaned.

The shared versioned transfer implementation has a separate video/frame adapter.
Its 98-case focused regression run covers the new prefixes and existing image
storage/worker behavior, including replaced latest keys, exact-version reads,
missing originals, multipart uploads and exact-attempt cleanup. The new video
storage checks use a simulated versioned store; real video-input S3 roles and
end-to-end byte derivation remain complete-slice qualification requirements.

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

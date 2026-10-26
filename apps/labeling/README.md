# Labeling

This package owns task and batch control, manual and bulk Annotation writes,
compact-video editing, SceneTrack domain contracts, the Project class-usage
query manager, and video-worker finalization.
The base package depends on Pydantic and the portable preannotation-results,
assets-media and mask-content contracts. It does not depend on the platform API,
SQLAlchemy, Celery, an object-storage SDK or ML implementations. PostgreSQL and
HTTP runtime bindings are optional extras.

For class-usage reads, the trusted API peer checks current Workspace management
authority before calling the Labeling projection. This runtime does not accept public credentials or an
`is_manager` flag. Independent providers use read-only READ COMMITTED transactions;
the default API adapter borrows its caller Session without owning commit.

`get_project_class_usage` first reads class counts, then reads every active
annotation's stored attributes JSON text. Class counts are grouped by the
annotation's project ID and include active rows regardless of cancellation,
annotation source, tool unit, or Task project. Attribute counts include only
top-level keys from JSON objects; scalar JSON values are still fully decoded.

The strict private request/reply schema is
`aap-labeling-class-query/v1`. Its schema file is distributed beside the Python
package.

The reply echoes request and Project identity. Count maps reject duplicate keys,
extra fields, booleans and nonpositive integers; map order and JSON whitespace are
not part of the contract. Late attribute decoding failure remains an internal500,
including the frozen legacy large-integer case, without returning partial counts.

Run `uv run --project apps/labeling --extra runtime python -m aap_labeling` with
`LABELING_CLASS_QUERY_DATABASE_URL(_FILE)` and
`LABELING_CLASS_QUERY_CONTROL_TOKEN(_FILE)`. It listens on port8178 by default.
The private route is `POST /v1/labeling/class-queries/execute`; `/ready` verifies
the policy and the exact three-function credential. The standalone Compose file
uses two read-only secret files and binds only localhost.

Use `uv run --project apps/labeling --extra runtime --extra test pytest
apps/labeling/tests` for the package suite. Native permissions, coordinated API
replay, installed peers, images and restore remain separate acceptance gates.
See [ADR-0142](../../docs/adr/0142-labeling-class-usage-query-owner.md) for their
current status and limits.

## Task control in the caller transaction

The closed contracts `aap-labeling-task-locks/v1` and
`aap-labeling-task-workflow/v1` call
`aap_labeling_v1.task_edit_lock_command(jsonb)` and
`aap_labeling_v1.task_workflow_command(jsonb)`. Their packaged SQL and JSON
schemas match the frozen migration resources. Portable data clients accept a
callback and do not import the API or SQLAlchemy.

Edit locks retain the existing advisory-lock namespace, account suspension
fence, takeover rules, lease renewal, expired-lock cleanup, and ordered batch
final-write check. Lifecycle commands cover submission, skip, withdrawal,
reopening, review and contributor evidence. They recheck current actor and
Task/Batch identity; the API retains current Workspace authorization, Audit,
counter updates, QC and notification ordering.

The API borrows its existing connection and synchronizes its ORM cache from the
owner result. Neither data client commits or rolls back. These transaction locks
must not use an HTTP call or a separate connection: they must survive until the
same business transaction ends. A language-neutral PostgreSQL command is the
boundary for these operations.

Apply current migrations and `infra/docker/module-data-grants.sql`, then the
existing LAN/worktree runtime grants. A dedicated trusted caller can use
`infra/docker/labeling-task-control-grants.sql`; it grants exactly these two
functions and rejects direct data access, role membership, CREATE and TEMP.
It does not grant public end users access to the commands. Full module qualification and
local adoption are tracked in the module deployment ledger; this profile alone
is not a deployment claim.

## Manual Annotation writes

The contracts `aap-labeling-annotation-writes/v1`,
`aap-labeling-scene-track-domain/v1` and
`aap-labeling-annotation-statistics/v1` use three fixed JSONB commands in
`aap_labeling_v1`: `annotation_write_command`, `scene_track_domain_command`
and `annotation_statistics_command`.

Manual create, field updates, soft deletion and create receipts retain the
caller transaction. Task statistics use active, non-cancelled, non-sensor
annotations; the existing optional Batch transition order remains in the API.
SceneTrack identity, presence intervals, revisions and camera reclassification
use the same owner, including explicit-presence rejection and interval exclusion
constraints. Mutable Annotation updates retain version checks and existing
Mask history capture.

The Mask revision trigger and recorder keep their existing invoker privileges
and behavior, with qualified public references and a fixed search path so they
also run inside owner functions. The upgrade and rollback resources ship beside
the other SQL files. They do not grant the caller direct revision-table access.

The API retains current authorization, source and Mask preflight, audit,
heartbeat, and commit ordering. A command payload is not a permission token.
Its data adapter synchronizes ORM values from the owner result and does not open
another connection or commit. Apply the matching migrations and owner/runtime
grants together; `infra/docker/labeling-annotation-writes-grants.sql` supplies a
dedicated trusted caller with exactly these three functions. The existing task
control credential keeps its two-function profile.

Slice/Mask operations, imports and human video candidate decisions use their
own fixed capabilities and caller transactions. Assets dataset reset remains a
separately held cross-owner workflow.

## Bulk editing, compact video and batch control

The closed contracts `aap-labeling-annotation-bulk/v1`,
`aap-labeling-video-bbox-editing/v1` and `aap-labeling-batch-control/v1` use
`annotation_bulk_command(jsonb)`, `video_bbox_editing_command(jsonb)` and
`batch_control_command(jsonb)` in `aap_labeling_v1`. Portable clients borrow
an execution callback; the API keeps its current connection, authorization and
commit. No transaction lock is transferred over HTTP.

Bulk edits retain whole-track class checks, camera-member updates, attribute
provenance and one version/revision advance. Workspace configuration is read
through its existing public capability. Compact-video policies cover conversion,
aggregation, splitting, merging and joining; existing geometry imports remain
compatible. Actor evidence and statistics stay in the original API workflow.

Batch commands own creation, maintenance, assignment, lifecycle, admin locks,
counters and the existing reset/cleanup writes. Current Identity and Workspace
authority stays in the application workflow. Prediction deactivation and parent
detachment remain separate updates because Mask history records both; cleanup
counts retain camera members. Prediction deletion itself is a separate boundary.

After matching migrations and owner/runtime grants, dedicated trusted callers
use `infra/docker/labeling-editing-grants.sql` for exactly the two editing
functions, or `infra/docker/labeling-batch-control-grants.sql` for exactly the
batch command. Previous task-control and manual-write credentials are unchanged.
The new roles have no direct table access, role membership, CREATE or TEMP.
Saved Native checks are registered with the owned-database verifier; source and
basic checks do not establish native permission, recovery or module qualification.

## Prediction, SceneTrack and cross-frame writes

`prediction_record_command(jsonb)` owns result and metadata insertion, the Task
prediction counter and timestamp, failures, dismissal/restoration, candidate
rejection, Task/Batch cleanup and source-scoped import purge. The purge keeps
FailedPrediction rows; cleanup retains its original metadata-first delete order.
`annotation_prediction_command(jsonb)` owns accepted Annotation creation and
secondary attributes/child source changes. Existing Mask preflight, current write
scope, contributor evidence, source lineage, heartbeat, audit and commit remain in
the caller workflow. Child creation and its source update stay separate so Mask
history and version behavior are retained.

`prediction_partition_command(jsonb)` ensures one fixed UTC month partition.
The existing monthly worker keeps its method, calendar range and caller transaction;
it submits closed month requests instead of executing DDL. Prediction parent and
partition ownership stays with the NOLOGIN Labeling owner, which alone receives
public-schema CREATE for this maintenance. The prediction-client profile allows
exactly the two prediction-write commands plus this maintenance command; callers
still lack raw table access and DDL. User-performance MV ownership remains separate.

Legacy upload Task initialization and completion use the fixed
`create_uploading_task(uuid,uuid,text,text,text)` and
`complete_uploading_task(uuid,uuid,uuid,text,text)` capabilities. The packaged
`upload-tasks.schema.json` defines their argument order and closed replies.
The owner preserves Task defaults and sequence allocation, locks and compares the
captured completion target, and performs no commit. The API retains manager
authorization, the post-storage canonical fence, and the shared transaction with
image admission; initialization still commits before signing the upload URL.

`scene_track_command(jsonb)` owns command lock/replay/apply/inspect/list/revert,
including operation snapshots, intervals, member changes and reversal. Current
Task locks and revisions fence each operation. Scene/frame facts come from the
existing Assets public projection, including the primary-LiDAR fallback. Preview,
detail and other domain reads still use their existing API adapters.

`cross_frame_write_command(jsonb)` owns propagation and interpolation mutations.
The API still checks each target's current visibility/editability and uses public
Assets pose/source capabilities before the final same-transaction write. Geometry
policy uses the standard library and keeps the previous API import paths.

These four functions use closed JSON contracts and borrow the original
transaction. Apply matching migrations and owner/runtime grants together. Trusted
prediction callers use `infra/docker/labeling-prediction-grants.sql` for exactly
the two prediction-write functions and the partition-maintenance function. SceneTrack and cross-frame callers use
`infra/docker/labeling-scene-track-commands-grants.sql` and
`infra/docker/labeling-cross-frame-grants.sql`, each with one function. The LOGIN
roles receive no private tables, role membership, CREATE or TEMP. Test-only
fixture cleanup now uses `aap_labeling_v1.delete_fixture_project_rows(uuid[])`;
the fixed owner command is available only in `_test` and `_e2e` databases, keeps
the caller's transaction and Workspace retirement guard, and does not establish
complete module deployment or recovery qualification. The remaining Labeling
operation contracts and module-level deployment gates are tracked separately.

## Discussion, Mask editing and scene reads

`discussion_command` and `task_event_command` own comment/feedback mutations and
event admission/insertion. Three explicit read projections keep the existing
single-statement discussion, dashboard, export and filtering queries composable.
The discussion LOGIN gets the two commands and four SELECT-only projections,
including the existing unified reconciliation view; caller authorization continues
to consume Identity and Workspace through their existing public capabilities.

`mask_edit_command`, `mask_revision_command` and `ai_mask_accept_command` own the
existing slice/restore and final Mask writes, operation/lineage/acceptance records,
and revision retention. Current content validation and pixel algorithms remain in
the calling workflow. `protect_mask_slice_versions` now takes the task ID before
the report and expiry; all repository callers use that scope.

`scene_read_query` owns the fixed scene/track/interval/member/timeline reads and
cache projections, with exact request validation and reply scope binding. Three
scalar JSON helpers stay private to its owner. Scene facts continue to use the
existing Assets public spatial projection.

Apply `labeling-discussion-grants.sql`, `labeling-mask-edit-grants.sql` or
`labeling-scene-read-grants.sql` from `infra/docker` to the corresponding trusted
LOGIN, after matching migrations and common owner/runtime grants. Mask and scene
LOGINs get exactly three and one functions; no private base table access is granted.
Native, cold and restore qualification remains a complete-module acceptance gate.

## Video control, transfer, core reads and quality

`video_control_command` owns human candidate, keyframe and worker state control.
`annotation_transfer_command` owns the
existing import, conversion, camera editing and Mask format-import ledger.
`task_maintenance_command` owns source-less Task cleanup and derived counters.
All borrow the original caller transaction and preserve its commit boundary.

Thirteen public core-row views retain native column types. Seven fixed row-lock
functions return their explicit published column order and preserve the selected
lock mode. The core-read LOGIN receives only these views and lock functions.

`mask_qc_command`, `mask_repair_command` and `raster_mask_retention_query` own the
Mask quality/repair state and reference reads. `spatial_quality_command` owns the
existing point-cloud and video quality data. Its chapter read uses the five-column
Assets Media `video_chapter_facts` projection; only the Assets owner reads the
underlying chapter table.

Apply the matching `labeling-video-control`, `labeling-annotation-transfer`,
`labeling-core-read`, `labeling-mask-quality`, `labeling-spatial-quality` or
`labeling-task-maintenance` grants profile after migrations and common grants.
Each filename ends in `-grants.sql` under `infra/docker`. These trusted profiles
grant no private-table access, CREATE, TEMP or role membership. Operation source
integration does not establish full module runtime or recovery qualification.

## Drafts and schema cleanup

`annotation_maintenance_command(jsonb)` owns existing draft reads/writes, cleanup
candidate/task reads and the final schema-orphan mutation. It retains ordered
Batch → Task → Annotation locks, refreshes current targets before changes and
keeps Mask history, system attributes and source geometry intact. The API retains
current management/configuration locks, contributor evidence, statistics, audit
and the outer commit. Existing draft service methods remain internal; no draft
product flow is added.

`infra/docker/labeling-annotation-maintenance-grants.sql` gives a trusted caller
exactly this one function, without private-table, CREATE or TEMP access. Module
qualification and local adoption are recorded separately in the deployment ledger.

## Video worker finalization

The private protocol `aap-labeling-video-writer/v1` moves worker success,
cooperative cancellation and failure publication behind Labeling's narrow data
owner. The portable manager validates one stable command, checks the current
execution proof and reconstructs completed candidates from Jobs' sealed canonical
video windows. It does not rerun inference.

The API can use three bindings to the same manager and Native function:

- its default borrowed SQLAlchemy Session, where the API retains commit control;
- a dedicated function-only PostgreSQL DSN, where the provider owns a
  READ COMMITTED transaction; or
- the private HTTP owner, selected explicitly with a URL and control token.

URL and DSN cannot both be configured. An explicitly selected provider failure
does not silently fall back. Retries keep the same operation ID, execution proof
and request digest; after an ambiguous response, callers can query status for that
exact operation before deciding whether to retry. A separate Native
read_publication action and proof-only HTTP status lookup accept only the current
execution proof; a new-generation cancellation first reuses
an existing immutable publication receipt and only reconstructs the sealed prefix
when no publication exists. No retry sends the model request again.

Completion requires every frozen window and required session close. Ordinary
tracking cancellation keeps the sealed result prefix and may accept only a
current-generation capture tail inside the next planned window, and it requires the
database cancellation marker. Correction cancellation clears correction candidates
and bypasses stage authorization. Failure finalization does not replace
an already cancelled candidate payload. The owner continues to enforce current
tracker actor, source, Task and segment checks; the task-creation accepted-rights
rule does not grant a tracker worker new authority.

The optional process entry point is `aap-labeling-video-writer`; it serves
`POST /v1/labeling/video-writer/execute` and
`POST /v1/labeling/video-writer/status` on port 8180 by default. It requires
`LABELING_VIDEO_WRITER_DATABASE_URL(_FILE)` and
`LABELING_VIDEO_WRITER_CONTROL_TOKEN(_FILE)`. Mask candidates additionally
require the Assets Mask Content URL and token; bbox-only results do not require
Mask credentials. The HTTP client uses the separate `LABELING_VIDEO_WRITER_URL`,
`LABELING_VIDEO_WRITER_CONTROL_TOKEN(_FILE)` and
`LABELING_VIDEO_WRITER_TIMEOUT_SECONDS` settings.

Human accept/decide/discard and intermediate start/progress use separate fixed
Native capabilities in their original caller transaction. Consumer, Mask,
crash-cancellation, installed-artifact, restore and bounded-load evidence is
tracked with its exact source and scope in the module deployment ledger. See
[ADR-0159](../../docs/adr/0159-independent-labeling-video-writer.md).

## Operational consumers

`infra/docker/labeling-role-audit-grants.sql` explicitly installs the five fixed
`aap_labeling_audit_v1` read projections for the historical project-role audit.
Its sibling `labeling-role-audit.schema.json` publishes column types and raw
nullable semantics. It supports the pre-preparation schema without advancing
Alembic; rerun it after adding preparation columns. The audit LOGIN has no direct
Task, Batch, Annotation or TaskLock privileges and retains its read-only snapshot.

Manual filtering preparation uses the test-database-only
`prepare_filtering_rendering(jsonb)` command. It changes only the three expected
video fixture candidates, preserves their confidence and supplies the fixed
frame 0/0/10 geometry. Automatic metric-only seeds are unchanged. The caller
retains the existing separate post-seed commit and synchronizes loaded rows from
the owner reply; there is no generic prediction editor or production grant.

For daily local topology, combine the existing class-query, video-writer and Mask
Content Compose files with `docker-compose.labeling-development.yml`. Select
immutable qualified images, scoped credentials and the verified external
infrastructure network. The Mask GC process is manual-only in this overlay;
automatic collection requires a separate explicit operational decision.

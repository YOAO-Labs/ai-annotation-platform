# Labeling

This package owns task and batch control, manual and bulk Annotation writes,
compact-video editing, SceneTrack domain contracts, the Project class-usage
query manager, and video-worker finalization.
The base package depends on Pydantic and the portable preannotation-results,
assets-media and mask-content contracts. It does not depend on the platform API,
SQLAlchemy, Celery, an object-storage SDK or ML implementations. PostgreSQL and
HTTP runtime bindings are optional extras.

For class-usage reads, the API checks current Workspace management authority
before calling the Labeling projection on its own Session. The manager does not
commit or roll back; the adapter preserves both flush points, `no_autoflush`,
and caller rollback, including Workspace's same-transaction class rename.
The ordinary Worktree and LAN roles get only the three class-query functions,
not the Annotation tables. The manager accepts no public credentials or
`is_manager` flag.

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

The optional portable provider remains for contract verification, not product
deployment. `python -m yoao_labeling` serves
`POST /v1/labeling/class-queries/execute`; `/ready` verifies the policy and the
exact `yoao_labeling_class_query_client` credential. Its independent PostgreSQL
adapter uses read-only READ COMMITTED transactions. API deployment has no
class-query URL, DSN, or control bearer selector.

Use `uv run --project apps/labeling --extra runtime --extra test pytest
apps/labeling/tests` for the package suite. Native permissions, coordinated API
replay, installed peers, images and restore remain separate acceptance gates.
See [ADR-0142](../../docs/adr/0142-labeling-class-usage-query-owner.md) for their
current status and limits.

## Task control in the caller transaction

The closed contracts `aap-labeling-task-locks/v1` and
`aap-labeling-task-workflow/v1` call
`yoao_labeling_v1.task_edit_lock_command(jsonb)` and
`yoao_labeling_v1.task_workflow_command(jsonb)`. The current `0466` SQL/schema
pair matches its frozen migration resource; earlier packaged revision artifacts
stay byte-identical to their frozen copies. Portable data clients accept a
callback and do not import the API or SQLAlchemy.

Edit locks use the current `task-edit-lock:<task UUID>` advisory domain while
retaining the account suspension fence, takeover rules, lease renewal,
expired-lock cleanup, and ordered batch final-write check. The historical
`aap:task-edit-lock:` key is read only by the fail-closed cutover guards; it is
never acquired and has no fallback path. Lifecycle commands cover submission,
skip, withdrawal, reopening, review and contributor evidence. They recheck
current actor and Task/Batch identity; the API retains current Workspace
authorization, Audit, counter updates, QC and notification ordering.

Contributor recording first takes a nonblocking transaction advisory lock keyed
by the exact Batch, retaining same-batch writer exclusion until the caller commits
or rolls back. It then takes a NOWAIT SHARE lock on that Batch so original-authority
readers can coexist, followed by the same NOWAIT UPDATE lock on the exact Task.
The writer exclusion prevents competing contributors from both holding SHARE
before later Batch state/counter writes upgrade to UPDATE. Different batches
remain independent; competing writers still receive the existing busy response.
Other workflow operations retain their NOWAIT UPDATE Batch lock. Actor freshness,
Task/Batch identity and expected status/review-round checks remain in the caller
transaction.

This compatibility applies to contributor recording, not to the caller's whole
workflow. Batch status/counter transitions, cleanup and review completion may
still require an exclusive Batch lock before or after recording evidence.

The API borrows its existing connection and synchronizes its ORM cache from the
owner result. Neither data client commits or rolls back. These transaction locks
must not use an HTTP call or a separate connection: they must survive until the
same business transaction ends. A language-neutral PostgreSQL command is the
boundary for these operations.

Apply current migrations and `infra/docker/module-data-grants.sql`, then the
existing LAN/worktree runtime grants. A dedicated trusted caller can use
`infra/docker/labeling-grants.sql` (`-v grant_task_control=1`); it grants exactly these two
functions and rejects direct data access, role membership, CREATE and TEMP.
It does not grant public end users access to the commands. Full module qualification and
local adoption are tracked in the module deployment ledger; this profile alone
is not a deployment claim.

## Manual Annotation writes

The contracts `aap-labeling-annotation-writes/v1`,
`aap-labeling-scene-track-domain/v1` and
`aap-labeling-annotation-statistics/v1` use three fixed JSONB commands in
`yoao_labeling_v1`: `annotation_write_command`, `scene_track_domain_command`
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
grants together; `infra/docker/labeling-grants.sql` (`-v grant_annotation_writes=1`) supplies a
dedicated trusted caller with exactly these three functions. The existing task
control credential keeps its two-function profile.

Slice/Mask operations, imports and human video candidate decisions use their
own fixed capabilities and caller transactions. Assets dataset reset remains a
separately held cross-owner workflow.

## Bulk editing, compact video and batch control

The closed contracts `aap-labeling-annotation-bulk/v1`,
`aap-labeling-video-bbox-editing/v1` and `aap-labeling-batch-control/v1` use
`annotation_bulk_command(jsonb)`, `video_bbox_editing_command(jsonb)` and
`batch_control_command(jsonb)` in `yoao_labeling_v1`. Portable clients borrow
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
use `infra/docker/labeling-grants.sql` (`-v grant_editing=1`) for exactly the two editing
functions, or `infra/docker/labeling-grants.sql` (`-v grant_batch_control=1`) for exactly the
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

Independent Task uploads use `create_uploading_task(uuid,uuid,text,text,text,uuid)`
for the fixed uploading row and `publish_task_original(uuid,uuid,uuid,uuid,uuid,text)`
for the CAS pointer after Assets seals the original. The packaged
`upload-tasks.schema.json` defines their argument order, closed replies and the
`image`/`video`/`other` file types. Labeling allocates the `T-` display ID, preserves
Task defaults, rechecks manager authority and performs no commit. The API allocates
and verifies the independent managed original, commits before signing the staging
URL, and keeps the post-storage canonical fence before publication. The API LOGIN
does not need access to the Task display sequence.

The pre-managed `create_uploading_task(uuid,uuid,text,text,text)` and
`complete_uploading_task(uuid,uuid,uuid,text,text)` pair is retired by `bo0380`
and is no longer granted or distributed.

The ordinary runtime uses fixed capabilities for comment attachments, staged
Mask format uploads, and the integrated raster-object publication ledger.
External storage still uses saved targets and physical-I/O permits; the
Labeling function calls borrow the original business or cleanup transaction.
The separate Mask Content and GC profiles are unchanged. The API `/metrics`
scrape reads only aggregate tracker and accept-decision facts, not the jobs,
staged object keys, or decision receipts.

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
prediction callers use `infra/docker/labeling-grants.sql` (`-v grant_prediction=1`) for exactly
the two prediction-write functions and the partition-maintenance function. SceneTrack and cross-frame callers use
`infra/docker/labeling-grants.sql` (`-v grant_scene_track_commands=1`) and
`infra/docker/labeling-grants.sql` (`-v grant_cross_frame=1`), each with one function. The LOGIN
roles receive no private tables, role membership, CREATE or TEMP. Test-only
fixture cleanup now uses `yoao_labeling_v1.delete_fixture_project_rows(uuid[])`;
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

Acceptance history extends the existing `mask_edit_command`; it does not grant a
fourth function or private table access. The API journals keyed ordinary acceptance
and native Mask create/refine in the accepting transaction. Refinement captures
the prior geometry and prediction lineage before mutation. Restore locks the
task and complete annotation version set, preserves annotation IDs, journals
each idempotent transition, and retains image/video Mask revisions under the
same 30-day policy as slice restore. Current task, actor, segment and source
qualification remain caller and owner fences; the receipt is not authorization.
The restore holds the existing task editor scope or video-segment lock through
the mutation and rechecks the segment lease. For Scene Track members it uses
the track owner to advance revision and rejects active camera dependencies.
The Assets-owned history source helper locks the exact direct or primary-linked
DatasetItem and returns its existing source snapshot, generation and identity;
only the Labeling owner can execute it, without private Assets table grants.

`scene_read_query` owns the fixed scene/track/interval/member/timeline reads and
cache projections, with exact request validation and reply scope binding. Three
scalar JSON helpers stay private to its owner. Scene facts continue to use the
existing Assets public spatial projection.

Apply `labeling-grants.sql` (`-v grant_discussion=1`, `-v grant_mask_edit=1`, or
`-v grant_scene_read=1`) from `infra/docker` to the corresponding trusted
LOGIN, after matching migrations and common owner/runtime grants. Mask and scene
LOGINs get exactly three and one functions; no private base table access is granted.
Native, cold and restore qualification remains a complete-module acceptance gate.

## Video control, transfer, core reads and quality

`video_control_command` owns human candidate, keyframe and worker state control.
`annotation_transfer_command` owns the
existing import, conversion, camera editing and Mask format-import ledger.
`task_maintenance_command` owns source-less Task cleanup and derived counters.
All borrow the original caller transaction and preserve its commit boundary.
Mask format-import creation records the already-verified staged storage reference
in the same transaction as its plan and receipt. Reads and resumed executions
use that saved reference and content digest, not the current import binding.

Public core-row views retain native column types. Row-returning lock functions
use the explicit published column order and preserve their selected lock mode.
`core_read_contract.CORE_READ_LOCK_FUNCTIONS` is the current lock inventory;
the core-read LOGIN receives only the published views and lock functions.

Frozen exports additionally lock bounded prediction rows by exact project/task
scope, SceneTrack rows and their intervals, and exact TaskDatasetItemLink IDs.
These capabilities retain global identity ordering, the caller's exact SQL
limit, UPDATE/SHARE NOWAIT and caller-owned transaction lifetime. Composite
Prediction identities remain distinct. The packaged export-lock SQL accompanies
the forward migration; no private-table grants or separate transaction are needed.

Video exports read the published `video_track_quality_run_rows` projection and
freeze segment/quality identities through
`lock_video_segment_ids_for_export_share(uuid[], integer)` and
`lock_video_quality_run_ids_for_export_share(uuid[], integer)`. These return only
ordered IDs under SHARE NOWAIT; the bounded payload reads remain separate so
oversized quality JSON is rejected before materialization. The same transaction
holds the locks through snapshot collection. Runtime callers do not receive
SELECT or UPDATE privileges on the private segment or quality tables.

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

`infra/docker/labeling-grants.sql` (`-v grant_annotation_maintenance=1`) gives a trusted caller
exactly this one function, without private-table, CREATE or TEMP access. Module
qualification and local adoption are recorded separately in the deployment ledger.

## Video worker finalization

The private protocol `aap-labeling-video-writer/v1` moves worker success,
cooperative cancellation and failure publication behind Labeling's narrow data
owner. The portable manager validates one stable command, checks the current
execution proof and reconstructs completed candidates from Jobs' sealed canonical
video windows. It does not rerun inference.

The tracking worker is the product host. It commits preceding progress and
borrows the same worker Session and ordinary LOGIN for the final transaction;
the manager owns no commit. There is no dedicated writer DSN, private bearer,
or HTTP selector in the application. Retries keep the operation ID, execution
proof and request digest. After an ambiguous commit, the worker reads status in
a fresh transaction using those same facts. A new-generation cancellation only
reconstructs the sealed prefix after the current-proof `read_publication`
lookup misses. No retry sends the model request again.

Completion requires every frozen window and required session close. Ordinary
tracking cancellation keeps the sealed result prefix and may accept only a
current-generation capture tail inside the next planned window, and it requires the
database cancellation marker. Correction cancellation clears correction candidates
and bypasses stage authorization. Failure finalization does not replace
an already cancelled candidate payload. The owner continues to enforce current
tracker actor, source, Task and segment checks; the task-creation accepted-rights
rule does not grant a tracker worker new authority.

The worker keeps the existing Mask byte custody: verify saved owned objects
or use the explicitly selected Assets Mask Content host. A remote read failure
never falls back locally. No cleanup credential is passed to the writer;
bbox-only results need no Mask Content credential. The portable
`yoao-labeling-video-writer` entrypoint, one-function test LOGIN and private
HTTP wire contract remain for independent contract verification.

Human accept/decide/discard and intermediate start/progress use separate fixed
Native capabilities in their original caller transaction. Consumer, Mask,
crash-cancellation, installed-artifact, restore and bounded-load evidence is
tracked with its exact source and scope in the module deployment ledger. See
[ADR-0159](../../docs/adr/0159-independent-labeling-video-writer.md).

## Operational consumers

`infra/docker/labeling-grants.sql` (`-v grant_role_audit=1`) explicitly installs the five fixed
`yoao_labeling_audit_v1` read projections for the historical project-role audit.
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

The only separate Labeling operator overlay is `docker-compose.labeling-mask-gc.yml`
(profile `mask-gc`), over `docker-compose.assets-mask-content.yml`. It runs manual
Mask GC with a six-function LOGIN and a distinct Assets cleanup bearer, without
enabling polling or replacing Beat's saved-object cleanup. See the
[Mask GC package](../labeling-mask-gc/README.md) for activation and recovery.

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
It does not grant public end users access to the commands. Unmigrated Batch and
Annotation operations still need their legacy permissions; this capability does
not close the entire Labeling module.

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

Slice/Mask operations, whole-scene-track commands, imports, cross-frame writes
and human video candidate decisions remain separate caller migrations. This
write group does not close the complete Labeling module.

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

This slice does not move human accept/decide/discard or intermediate start and
progress writes out of the API. Local owned-resource tests cover crash-cancellation recovery, all three actual
consumer bindings, Mask content and an HTTP process restart. Installed artifacts,
backups, images and capacity remain separate qualification gates. See [ADR-0159](../../docs/adr/0159-independent-labeling-video-writer.md).

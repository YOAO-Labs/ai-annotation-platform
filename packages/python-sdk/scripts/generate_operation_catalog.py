#!/usr/bin/env python3
"""Generate the installed SDK's compact, offline API operation catalog."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "packages" / "python-sdk"
SOURCE = PACKAGE / "src" / "ai_annotation"
SNAPSHOT = ROOT / "apps" / "api" / "openapi.snapshot.json"
MANIFEST = PACKAGE / "api-coverage.toml"
OUTPUT = SOURCE / "_operation_catalog.generated.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_MISSING = object()
METHOD_NOTES: dict[str, dict[str, Any]] = {
    "files.upload_init": {
        "constraints": [
            "Requires project manager authority before task creation/storage signing. Creates an uploading task and a temporary PUT URL (900 seconds), not a dataset item. Save task_id; no idempotency key or automatic POST retry."
        ],
    },
    "files.upload_content": {
        "constraints": [
            "Streams the local file to the absolute init upload_url without platform credentials. content_type must match initialization. No redirect/retry or implicit completion; preserve task_id and explicitly call upload_complete only after a successful 2xx PUT."
        ],
    },
    "files.upload_complete": {
        "constraints": [
            "Requires project manager authority before storage verification or queue dispatch. The task must still be uploading; success moves it to pending and may enqueue an image thumbnail. No automatic retry or implicit wait."
        ],
    },
    "files.original_identity": {
        "constraints": [
            "Requires tasks:read for API keys and rechecks current task visibility, including project, batch and assignment. Returns the verified content descriptor (content_id, sha256, size_bytes, media_type); original availability remains unchecked."
        ],
    },
    "files.download_original": {
        "constraints": [
            "Reads the verified identity first, then streams the full original over platform credentials with content_id and sha256 bound to that descriptor. Enforces the descriptor SHA-256 and size, accepts only HTTP 200, and replaces the destination atomically; the legacy signed file-url route returns 409 original_requires_authenticated_download."
        ],
    },
    "files.backfill_thumbnails": {
        "constraints": [
            "Requires project manager authority. Returns a durable image-thumbnail job receipt (operation_id, job_id, status, accepted_before), not a Celery task ID or per-item progress. Reuse the Idempotency-Key after a lost response; no automatic POST retry or implicit wait."
        ],
    },
    "scenes.list": {
        "constraints": [
            "Returns a raw unpaginated array for dataset_id. Scenes follow the global dataset-catalog read policy, not project membership or creator privacy. API keys require datasets:read."
        ],
    },
    "scenes.get": {
        "constraints": [
            "Requires authenticated identity and datasets:read for API keys; unknown scene returns 404. Current global dataset policy remains authoritative."
        ],
    },
    "scenes.trajectory": {
        "constraints": [
            "Returns scene_id and frame-index-ordered ego poses; no pose data means an empty array. API keys require datasets:read. This is source trajectory metadata, not annotation/tracker geometry."
        ],
    },
    "scenes.update": {
        "constraints": [
            "Requires platform project_admin/super_admin like dataset metadata writes. Only explicit fields are sent; null is no-op and source_metadata={} clears it. No CAS revision; name conflicts remain 409."
        ],
    },
    "video_review.upload_mask_content": {
        "constraints": [
            "Uploads COCO RLE JSON for a visible editable task and returns an immutable content-addressed reference; does not edit an annotation. Requires annotations:write and server quota/geometry capability checks. storage_encoding is separate from COCO RLE encoding."
        ],
    },
    "video_review.get_annotation_mask_content": {
        "constraints": [
            "Requires annotations:read and current parent-task visibility. Returns static COCO RLE JSON; no conditional cache header is sent. For a track use the frame-specific method."
        ],
    },
    "video_review.get_annotation_frame_mask_content": {
        "constraints": [
            "Resolves a visible annotation's mask at the absolute source frame; an outside or missing mask returns 404. Response is JSON, not image bytes."
        ],
    },
    "video_review.get_prediction_mask_content": {
        "constraints": [
            "Task/prediction identity and current visibility are jointly checked. shape_index selects the prediction shape; frame_index is the absolute source frame. Returns resolved COCO RLE JSON."
        ],
    },
    "video_review.save_mask_keyframe": {
        "constraints": [
            'Pass the uploaded CocoRleMaskRef, exact task/annotation/frame and current annotation expected_version. Sends If-Match W/"version"; the server checks parent identity, editability and segment lease. 409 never refreshes or retries the write.'
        ],
    },
    "video_review.operate_mask_keyframe": {
        "constraints": [
            "Delete, mark outside or restore against exact annotation expected_version/If-Match. The server checks lease and keyframe validity; deleting the last keyframe and invalid outside/restoration transitions are rejected."
        ],
    },
    "video_review.list_track_quality_runs": {
        "domain_effects": [
            "This read can refresh stale status against current inputs."
        ],
        "constraints": [
            "Unpaginated task-scoped quality runs; their statuses and digests are distinct from tracker candidate review."
        ],
    },
    "video_review.run_track_quality": {
        "constraints": [
            "Requires adjacent completed collaboration segments. HTTP 200 creates or reuses a domain run and dispatches an AsyncJob; it is not completed quality analysis. Same current input digest may reuse an eligible run."
        ],
    },
    "video_review.get_track_quality_run": {
        "constraints": [
            "The run must belong to the task. Reading can mark stale and includes current chapter-derived metrics; preserve input_digest for any acceptance."
        ],
    },
    "video_review.accept_track_quality_run": {
        "constraints": [
            "Requires task reviewer or project management authority, exact input_digest and complete pair decisions. The server rejects stale input and incompatible pairs/components; 409 is not automatically refreshed or retried."
        ],
    },
    "video_review.compose_tracks": {
        "constraints": [
            "Requires editable task annotations and valid segment write range. delete_sources defaults to true and can deactivate input annotations. Inspect operation-specific geometry/count/frame rules before confirming; no If-Match or idempotency key is available."
        ],
    },
    "video_review.convert_track_to_bboxes": {
        "constraints": [
            "copy preserves the source, while split may deactivate or remove part of its track. scope=frame needs frame_index; frame_mode selects keyframes or all_frames for track scope. No If-Match/idempotency key; read current source and deliberately confirm split."
        ],
    },
    "video_tracker.list_jobs": {
        "constraints": [
            "Requires project_admin or super_admin; project administrators are restricted to projects they own. Opaque cursor pages have limit 1–100. counts follow project/model filters but ignore status/cursor."
        ],
    },
    "video_tracker.get_job": {
        "constraints": [
            "Task visibility is rechecked. Tracker completed only describes compute; pending_review/partially_reviewed await candidate decisions, accepted/discarded describe review."
        ],
    },
    "video_tracker.list_active": {
        "constraints": [
            "Recover queued/running jobs after uncertain creation before another POST. Ordinary task actors list their own jobs; privileged actors may see all task jobs."
        ],
    },
    "video_tracker.list_reviewable": {
        "constraints": [
            "Returns task jobs with staged reviewable results, including eligible cancelled jobs; ordinary task actors see their own jobs. This is an unpaginated array, not an AsyncJob page."
        ],
    },
    "video_tracker.track": {
        "constraints": [
            "Preferred task-level creation route. Source annotations must belong to the task; no-source discovery needs configured target class/tool unit. Model keys/capabilities are dynamic. Do not combine singular and plural source selectors; the server otherwise prioritizes a nonempty plural list.",
            "The server checks task editability, absolute frame bounds and a valid segment lease covering the window. HTTP 202 returns tracker job identity, not a generic AsyncJob ID. No idempotency key/automatic POST retry; recover with list_active/list_reviewable after an uncertain response.",
        ],
    },
    "video_tracker.propagate": {
        "constraints": [
            "Legacy single-annotation creation path; task-level track is preferred. The annotation/task relationship and segment lease are checked, and creation has no idempotency key or automatic retry."
        ],
    },
    "video_tracker.create_correction": {
        "constraints": [
            "First upload and save the manual mask keyframe with the current annotation version. Preserve correction frame, source_annotation_version, corrected_mask_digest, exact model/backend and segment. Bbox fallback is explicit. This 202 job requires local decisions rather than whole-job accept/discard."
        ],
    },
    "video_tracker.get_preview": {
        "constraints": [
            "Read authoritative job_revision, expected_source_versions, candidate selectors/digests and manual protection before decisions. The response contains all current candidates without result pagination."
        ],
    },
    "video_tracker.decide_candidates": {
        "constraints": [
            "Select instance_ids+from_frame+to_frame or qc_issue_id+candidate_digest, exclusively. Preserve preview job_revision and required source versions. The server checks lease/source evidence; 409 requires deliberate reread, never automatic refresh/replay. override_manual requires privilege."
        ],
    },
    "video_tracker.accept_job": {
        "constraints": [
            "Accepts ordinary tracking candidates and writes annotations after permission, lease and source-version checks. Correction jobs require local decisions; no implicit override or POST retry."
        ],
    },
    "video_tracker.discard_job": {
        "constraints": [
            "Discards ordinary tracking candidates without annotation writes. Correction jobs require local decisions. The server owns replay and current lease/permission checks."
        ],
    },
    "video_tracker.cancel_job": {
        "constraints": [
            "Returns the updated tracker job. Ordinary tracking pending review requires discard instead; correction cancellation may clear staged candidates. Cancellation is not proof the worker has stopped."
        ],
    },
    "video_tracker.get_mask_content": {
        "constraints": [
            "Returns COCO RLE JSON, not binary bytes. Requires annotations:read, visible parent task and a SHA belonging to this job; invalid objects conflict and storage unavailability is retryable 503."
        ],
    },
    "project_templates.list": {
        "constraints": [
            "Returns a raw array of visible templates with no server pagination. scope and repeated type_key filters narrow it; search matches names. Private/organization/public visibility is checked by the server; super_admin sees all.",
        ],
    },
    "project_templates.create": {
        "constraints": [
            "Requires project_admin or super_admin; only super_admin may create public templates. organization requires organization_id; the current create service does not additionally require organization membership.",
            "source_project_id uses a visible project to fill omitted identity/configuration fields; explicit overrides win. Without a source, name/type_label/type_key are required. Explicit null identity fields are invalid. Creation has no idempotency key or automatic retry.",
        ],
    },
    "project_templates.get": {
        "constraints": [
            "Invisible templates return 404. API-key validity and current user visibility remain authoritative; the template routes do not declare separate API-key scopes."
        ],
    },
    "project_templates.update": {
        "constraints": [
            "Requires creator or super_admin; invisible templates return 404. Only explicit fields are sent, including null; no revision/If-Match protection is available. Configuration remains server-owned."
        ],
    },
    "project_templates.delete": {
        "constraints": [
            "Requires creator or super_admin and returns empty 204. Existing projects retain their copied configuration; usage_count does not prevent deletion."
        ],
    },
    "project_templates.duplicate": {
        "constraints": [
            "Requires administrator role and visible source. Sends no request body, creates an owned private copy with reset usage_count; no idempotency key or automatic retry."
        ],
    },
    "projects.create": {
        "constraints": [
            "template_id and source_project_id are mutually exclusive. Omit type fields and type_label to inherit server defaults from the visible source/template; explicit overrides win. No SDK-side configuration merge is performed.",
            "copy_annotation_guide is a source-project option and cannot accompany template_id. Template guide text is inherited but guide asset objects are not copied. Creation has no idempotency key and is not retried automatically.",
        ],
    },
    "storage.health": {
        "constraints": [
            "Authenticated probe of the annotation bucket; this is not a full check of every configured storage role."
        ],
    },
    "storage.buckets": {
        "domain_effects": [
            "May list many storage objects to compute bucket summaries."
        ],
        "constraints": [
            "Inspect each item's status/error; a successful HTTP envelope does not mean every bucket is healthy."
        ],
    },
    "storage.video_asset_failures": {
        "constraints": [
            "Requires project_admin or super_admin. Project administrators see dataset-only assets and assets linked to projects they own. Limit/offset pages can change while generation runs."
        ],
    },
    "storage.retry_video_asset": {
        "constraints": [
            "Administrator operation; the server rechecks project ownership of the dataset item. chunk requires chunk_id; frame requires frame_index, width and format. Metadata asset kinds queue the metadata generator.",
            "HTTP 202/status=queued acknowledges scheduling, not repair. No idempotency key or implicit wait/POST retry; reread failure and descriptor state before another deliberate attempt.",
        ],
    },
    "data_manager.schema": {
        "constraints": [
            "Fields, operators, options, columns and supported entity scopes are specific to the current project. Fetch this before building an unfamiliar filter; the response has no schema revision."
        ],
    },
    "data_manager.summary": {
        "domain_effects": [
            "This POST computes a read-only task-centric summary over the full matched visible scope, independently of page offsets."
        ],
        "constraints": [
            "The current handler accepts dimensions for compatibility but returns the standard aggregate set; it does not provide selective dimension projection."
        ],
    },
    "data_manager.matches": {
        "domain_effects": [
            "This POST is a read-only explanation query, not a task mutation."
        ],
        "constraints": [
            "The project/task pair and current task visibility are jointly checked. Annotation, prediction-shape and tracker matches share one offset/limit window; this response is not raw geometry."
        ],
    },
    "data_manager.query_objects": {
        "domain_effects": [
            "This POST reads visible active object rows and facets; it does not mutate annotations."
        ],
        "constraints": [
            "Use the object-grain project schema. Preserve filter groups and opaque cursors; reset cursor when filters or sort change. Current entity queries use only the first sort item and validate columns without pruning returned row fields."
        ],
    },
    "data_manager.query_tracks": {
        "domain_effects": ["This POST is a read-only logical-track query."],
        "constraints": [
            "Track scope must be available for the project. Use the server track_ref locator, not track_id. Current entity queries use the first sort item and do not prune returned row fields by columns_json; reset opaque cursors when query context changes."
        ],
    },
    "data_manager.object_detail": {
        "constraints": [
            "The annotation must belong to a currently visible project task and be an eligible active object; invisible, foreign or unavailable objects return 404."
        ],
    },
    "data_manager.object_location": {
        "constraints": [
            "Returns the direct location object, not a nested location envelope or media bytes. Project/task/annotation visibility is rechecked."
        ],
    },
    "data_manager.track_detail": {
        "constraints": [
            "Pass the opaque server track_ref unchanged; the SDK encodes it as a path parameter. The response includes all visible members and has no member-pagination limit."
        ],
    },
    "data_manager.assignment_preview": {
        "domain_effects": ["This POST previews assignment without applying it."],
        "constraints": [
            "Select 1–200 task UUIDs; IDs are sorted and deduplicated. At least one owner field must be explicit: omitted keeps the override, null clears it and inherits the batch owner. Preserve the returned preview_version and review per-item eligibility/effective owners."
        ],
    },
    "data_manager.assignment_apply": {
        "constraints": [
            "Submit the same canonical selection and explicit assignment fields with preview_version. The server locks/recomputes current state; 409 requires a fresh preview and a deliberate decision, never an automatic retry.",
            "Project management authority and target membership roles are rechecked. skipped_count/failed_count and succeeded IDs must be inspected; HTTP success is not success for every selected task.",
        ],
    },
    "data_manager.export_tasks": {
        "constraints": [
            "Exports exactly 1–200 selected visible task UUIDs and never falls back to a project-wide selection. Requires export.annotations capability. Video scope and VOC/coco-multicamera/nuscenes/kitti/pointmask targets are unavailable for this selected-task route.",
            "Optional Idempotency-Key is caller supplied (server trims and requires 1–128 characters). Preserve the original key/body; same scoped key with different canonical content conflicts. The SDK does not retry POST automatically.",
            "HTTP 202 returns job_id/status, including an existing terminal status on replay; it is not the artifact. Observe with jobs.get/wait and download via exports.download without persisting signed URLs.",
        ],
    },
    "task_views.list": {
        "constraints": [
            "Returns an unpaginated items envelope containing built-in and visible saved views. Built-ins have null id and a key, and cannot be passed to saved-view UUID routes. Null counts plus invalid_fields preserve invalid configurations instead of treating them as empty."
        ],
    },
    "task_views.get": {
        "constraints": [
            "The project_id/view_id pair is checked together. Private views are owner-visible; project views are visible to authorized project members. Do not infer access to another user's private view from a global role."
        ],
    },
    "task_views.create": {
        "constraints": [
            "View fields use the current project/entity schema. Project-shared visibility requires project management authority; private is the default. Invalid names or duplicate names remain server validation/conflict errors."
        ],
    },
    "task_views.update": {
        "constraints": [
            "entity_scope cannot be changed. Omitted or null fields remain unchanged; use {} or [] to clear filter/sort/column settings. Shared writes require project management authority and private writes require ownership."
        ],
    },
    "task_views.delete": {
        "constraints": [
            "Deletes only the saved view configuration and returns empty 204; tasks, annotations and tracks are not deleted."
        ],
    },
    "task_views.copy": {
        "constraints": [
            "The JSON body is required even when empty. Copy inherits source visibility by default; an ordinary member copying a shared view must explicitly select private visibility. The copy is owned by the caller and duplicate names can conflict."
        ],
    },
    "task_views.query": {
        "domain_effects": [
            "This POST is a read-only project task query, with no task mutation or job creation."
        ],
        "constraints": [
            "Uses limit/offset (1–200 per page), not cursor/revision/ETag. Get dynamic fields/operators from data_manager.schema. Explicit columns reduce expensive task projections; empty columns retain broader compatibility behavior. Interpret metrics with the requested columns: unrequested expensive fields may retain compatibility defaults. Do not flatten filter groups or compile filters locally."
        ],
    },
    "task_views.query_view": {
        "constraints": [
            "Only saved views with entity_scope=tasks can use this route. Pass the view UUID and owning project, not a built-in key. Pagination is limit/offset without a cursor."
        ],
    },
    "notifications.list": {
        "constraints": [
            "Returns the authenticated account's notifications; offset pages are not a stable snapshot while notifications change. Limit is 1–100.",
            "A notification target is a locator, not an authorization grant; reread the owning resource before acting.",
        ],
    },
    "notifications.mark_read": {
        "constraints": [
            "Marks one notification belonging to the authenticated account; a missing or already-read notification returns 404. This does not complete the target business task."
        ],
    },
    "notifications.mark_all_read": {
        "constraints": [
            "Changes all unread notifications for the authenticated account; it does not prove their business tasks were completed."
        ],
    },
    "notifications.clear_read": {
        "constraints": [
            "Deletes all read notifications for the authenticated account, without deleting their source feedback or tasks."
        ],
    },
    "notifications.delete": {
        "constraints": [
            "Deletes one notification belonging to the authenticated account; it does not delete the target resource."
        ],
    },
    "notifications.preferences": {
        "constraints": [
            "Returns effective preferences for the authenticated account and the server's current known notification types."
        ],
    },
    "notifications.update_preference": {
        "constraints": [
            "Only explicit in_app/toast channels are patched; omitted channels are preserved, explicit null and empty patches are rejected. The server owns the valid notification-type list."
        ],
    },
    "feedbacks.thread": {
        "constraints": [
            "The cursor is opaque and bound to root_feedback_id; reuse it only for that root thread in descending creation-time/ID order.",
            "root is separate from items; total counts active descendants, including descendants reached through a deleted intermediate reply.",
        ],
    },
    "feedbacks.reply": {
        "constraints": [
            "Sends only body and attachments; the server inherits scope from the parent and rechecks visibility and anchor freshness.",
            "This API has no idempotency key. Do not automatically retry after an ambiguous result; reread the thread before deciding whether another reply is needed.",
        ],
    },
    "feedbacks.patch": {
        "constraints": [
            "Omitted fields and explicit null are preserved. The current server treats null as no change, while mentions=[] clears mentions; content fields including null still affect permission checks.",
            "Author/manager content-edit authority differs from reviewer Issue-status authority; readable feedback or actions hints do not replace server authorization.",
        ],
    },
    "feedbacks.delete": {
        "constraints": [
            "Soft-deletes one feedback and returns empty HTTP 204. Deleting a root makes its thread unavailable; deleting a reply does not recursively delete its descendants."
        ],
    },
    "pipelines.list": {
        "constraints": [
            "The API returns an unpaginated visible array; scope/project/organization filters are available, but limit/offset are not server parameters."
        ],
    },
    "pipelines.create": {
        "constraints": [
            "Private scope requires project_id only, organization scope requires organization_id only, and public scope has neither; only private pipelines may be project defaults.",
            "Saving validates structure; target backend enablement and runtime capabilities are checked later by apply/dispatch. There is no standalone server validate/dry-run endpoint.",
        ],
    },
    "pipelines.update": {
        "constraints": [
            "Preserve omitted fields and explicit ownership-clearing nulls. The server validates the merged scope, current authority and default replacement."
        ],
    },
    "pipelines.apply": {
        "constraints": [
            "Clones a visible saved pipeline into the target project and optionally switches its default; it does not run inference or return an async job.",
            "The server checks all referenced backends are enabled in the target project; a visible source pipeline does not prove target-project applicability.",
        ],
    },
    "pipelines.preannotate": {
        "constraints": [
            "Bulk and workbench scopes have distinct task/project/batch/state/lock rules; workbench targets one task without batch_id. Explicit task lists contain at most 200 items.",
            "Idempotency-Key is optional and caller supplied. Preserve the exact key and request; a reused key with different content conflicts. The SDK neither generates keys nor retries POST automatically.",
            "Root stage identity is parent_stage is None, not stage number zero. The server owns graph, backend and model-capability qualification; there is no dry-run endpoint.",
            "Returns the actual job acknowledgement (currently HTTP 200), not completion. total_tasks may be unknown; observe job_id with jobs.get/wait, and do not dispatch another request just because polling times out.",
        ],
    },
    "training_studies.create": {
        "constraints": [
            "Creation requires preview_digest even though the shared preview model permits omission; submit the digest for the unchanged preview request.",
            "Keep base.request_id and the exact request stable when reconciling an uncertain submission.",
        ],
    },
    "exports.create": {
        "constraints": [
            "scope and lidar belong in body=ExportRequestBody(...), not query kwargs.",
            "This method returns an async job ID; use exports.download_voc for synchronous VOC ZIP bytes.",
        ],
    },
    "batches.export": {
        "constraints": [
            "scope and lidar belong in body=ExportRequestBody(...), not query kwargs.",
            "This method returns an async job ID; use batches.download_voc for synchronous VOC ZIP bytes.",
        ],
    },
    "exports.download_voc": {
        "constraints": [
            "Streams synchronous VOC ZIP bytes using authenticated POST without mutation retries; no async job ID is returned."
        ],
    },
    "batches.download_voc": {
        "constraints": [
            "Streams synchronous VOC ZIP bytes using authenticated POST without mutation retries; no async job ID is returned."
        ],
    },
    "training_runs.preflight": {
        "constraints": [
            "Use the returned digest for the same request body when creating the run; changing inputs invalidates the preflight result."
        ],
        "examples": [
            {
                "kind": "workflow",
                "text": "Run `aap learning runs preflight --project <project-id> --body run.json --json`, copy its digest into the same request body, then call `aap learning runs create`.",
            }
        ],
    },
    "training_runs.create": {
        "constraints": [
            "The SDK rejects a missing preflight_digest locally; submit the fresh digest returned by preflight for these request values.",
            "Keep request_id stable across reconciliation of an ambiguous submission. The SDK does not generate or replace it.",
        ],
    },
    "training_runs.logs": {
        "constraints": [
            "Use `after` to page forward or `before` to page backward; the SDK rejects requests that provide both.",
            "Resume from the returned next_cursor and preserve stream_id; inspect gaps before treating the read as complete.",
        ],
    },
    "training_runs.telemetry": {
        "constraints": [
            "Resume forward from the returned next_cursor for the same stream_id; inspect gaps before treating the read as complete."
        ],
    },
    "training_runs.events": {
        "constraints": [
            "`after` is the last event sequence already consumed; continue with the returned sequence values."
        ],
    },
    "training_runs.list": {
        "domain_effects": [
            "This GET may reconcile stale or lost training execution state on the server. Treat it as a state-aware read."
        ],
    },
    "training.runs": {
        "domain_effects": [
            "This GET may reconcile stale or lost training execution state on the server. Treat it as a state-aware read."
        ],
    },
    "training_recipes.archive": {
        "constraints": [
            "expected_head_revision is a compare-and-set precondition; reread the recipe after a conflict before retrying."
        ],
    },
    "learning_policies.disable": {
        "constraints": [
            "expected_revision is a compare-and-set precondition; reread the policy after a conflict before retrying."
        ],
    },
}


for _namespace in ("videos", "task_videos"):
    for _method in ("manifest", "segments"):
        METHOD_NOTES[f"{_namespace}.{_method}"] = {
            "domain_effects": [
                "This GET may create/persist video segments; segment reads can reconcile expired leases."
            ],
            "constraints": [
                "Use the namespace's dataset_item_id or task_id, not an interchangeable UUID. Signed media URLs are temporary; refresh the descriptor after expiry."
            ],
        }
    for _method in ("chunks", "chunk", "frame"):
        METHOD_NOTES[f"{_namespace}.{_method}"] = {
            "domain_effects": [
                "This GET can create cache rows, enqueue generation or prewarm adjacent chunks. Bound frame ranges; an unbounded chunks query may process the whole video."
            ],
            "constraints": [
                "Frame/chunk HTTP 202 is a JSON descriptor with status/retry_after, not binary media. Download only a ready descriptor with URL; polling is explicit. Frame width is 1–4096, formats webp/jpeg."
            ],
        }
    METHOD_NOTES[f"{_namespace}.frame_timetable"] = {
        "constraints": [
            "Python from_frame/to_frame map to HTTP from/to. Source is ffprobe or estimated; unavailable metadata returns 503. No implicit conditional-cache or polling behavior."
        ],
    }
    METHOD_NOTES[f"{_namespace}.prefetch_frames"] = {
        "constraints": [
            "Queues missing/failed frames for 1–500 requested indexes; the server deduplicates/sorts and validates actual video bounds. Acknowledgement is not generation completion."
        ],
    }
    METHOD_NOTES[f"{_namespace}.retry_frames"] = {
        "constraints": [
            "Up to 500 indexes; empty retries matching failed frames, force clears selected cache objects and regenerates. Task-scoped retry requires annotations:write; dataset-item retry currently checks visible-task access only. No automatic POST retry."
        ],
    }
    for _method in ("download_frame", "download_chunk"):
        METHOD_NOTES[f"{_namespace}.{_method}"] = {
            "constraints": [
                "Requires a typed ready descriptor with an absolute signed URL. No descriptor refresh or polling. Uses no platform credentials, accepts full HTTP 200 only, validates available byte size and atomically replaces destination; failures preserve the old file."
            ],
        }

METHOD_NOTES.update(
    {
        "task_videos.manifest_v2": {
            "domain_effects": ["May create segment records."],
            "constraints": [
                "Task-scoped current manifest with signed URLs; the legacy manifest method remains a separate public route/schema."
            ],
        },
        "videos.chunk_samples": {
            "constraints": [
                "Returns codec configuration and packet timestamps/offsets/sizes as JSON, not binary media or a local decoder. Missing diagnostics return 404."
            ],
        },
        "videos.chapters": {
            "constraints": [
                "Unpaginated chapters sorted by frame range; server-derived frame_step/source fields are authoritative."
            ],
        },
        "videos.create_chapter": {
            "constraints": [
                "Requires project manager authority. Ordered nonnegative frame bounds and nonblank title are validated; duplicate intervals conflict. Chapter metadata is not a tracker candidate or media-generation request."
            ],
        },
        "videos.update_chapter": {
            "constraints": [
                "The chapter must belong to this dataset item and the actor must manage its project. Only explicit fields are sent; the server validates the merged interval and duplicate ranges. No revision/CAS token exists."
            ],
        },
        "videos.delete_chapter": {
            "constraints": [
                "Checks dataset-item/chapter relationship and project management authority; deletes metadata, returns empty 204 and does not delete source media."
            ],
        },
        "task_videos.claim_segment": {
            "constraints": [
                "Requires annotations:write and eligible task actor/segment assignment. Inspect locked_by/lock_expires_at; the lease is needed for ordinary actor tracking and annotation writes."
            ],
        },
        "task_videos.heartbeat_segment": {
            "constraints": [
                "Explicitly extends the current owned lease using server TTL. SDK does not schedule background heartbeats or automatically reclaim after conflict."
            ],
        },
        "task_videos.release_segment": {
            "constraints": [
                "Releases the caller's permitted task segment lease; subsequent writes still require current lease checks."
            ],
        },
        "task_videos.submit_segment": {
            "constraints": [
                "Ordinary actor needs assignment and valid lease. Final segment submission can move task to review, freeze evidence and dispatch adjacent track-quality runs. Repeated completed submission returns current state."
            ],
        },
        "task_videos.reopen_segment": {
            "constraints": [
                "Requires review authority; clears review evidence, returns task to in-progress and marks affected quality runs stale. Deliberate confirmation is required by CLI."
            ],
        },
        "task_videos.unassign_segment": {
            "constraints": [
                "Requires review authority; completed segments must first be reopened. Changes assignment/lease ownership and requires CLI confirmation."
            ],
        },
    }
)


for _namespace, _methods, _note in [
    (
        "health",
        "get db redis minio celery",
        "Public origin-root /health routes, outside /api/v1. HTTP 503 remains a typed report without retries; CLI prints it and exits 1 on degradation. Public errors omit internal diagnostics.",
    ),
    (
        "auth",
        "login register register_open refresh logout_all",
        "JWT results do not update Client credentials or saved configuration. Preserve returned credentials explicitly. CLI uses hidden/file/stdin input; passwords, CAPTCHA and tokens are hidden from model repr, not from intentional JSON.",
    ),
    (
        "auth",
        "logout refresh",
        "Requires a JWT session, not an API key. No automatic refresh or saved-credential replacement. Logout revokes only this session; refresh can reject invalidated sessions.",
    ),
    (
        "auth",
        "forgot_password send_verification_email verify_email reset_password",
        "Explicit account recovery/verification flow with server rate limits and one-time credentials. Email operations contact SMTP; accepted forgot-password does not prove delivery. Reset invalidates prior JWT sessions.",
    ),
    (
        "auth",
        "resolve_invitation accept_invitation register",
        "Invitation token is an opaque bearer credential. Resolve/register are public; existing-account acceptance checks authenticated email, current inviter authority and project relationship. No implicit login or credential persistence.",
    ),
    (
        "account",
        "get_preferences update_preferences",
        "Preferences are extensible JSON. Send only the intended patch; server owns deep merge and atomic replacement. Named presets require current namedPresetsRevision; after 409 reread and reconcile, never substitute the newest token into stale full contents.",
    ),
    (
        "account",
        "upload_avatar set_avatar_ref clear_avatar",
        "Explicit avatar replacement may delete the previous uploaded object. Upload uses multipart file, at most 2 MiB; server verifies and normalizes WebP. Preset selection cannot forge upload tokens.",
    ),
    (
        "account",
        "request_deactivation cancel_deactivation",
        "Self-deactivation has a seven-day cooling period and notification effects. Cancellation applies only while eligible; no real account lifecycle action is implied by capability discovery.",
    ),
    (
        "account",
        "change_password",
        "Explicit hidden/stdin password input; successful 204 does not imply JWT/API-key revocation. New password needs uppercase, lowercase and a digit.",
    ),
    (
        "account",
        "heartbeat",
        "Updates account online state only when explicitly called. SDK does not schedule telemetry or background heartbeats.",
    ),
    (
        "account",
        "submit_task_events",
        "Submit real closed intervals with stable caller-provided client_id. Inspect discarded and queued_async; CLI emits the full receipt then exits 1 on discarded rows. Queued is not committed processing proof.",
    ),
    (
        "avatars",
        "download",
        "Public WebP token route with 32 lowercase hex characters. No conditional header is sent; full HTTP 200 is atomically saved and failures preserve the destination.",
    ),
    (
        "api_keys",
        "create rotate",
        "One-time plaintext is a bearer credential. CLI requires --json or --show-secret before mutation plus confirmation; model repr hides plaintext, intentional JSON retains it. Rotation immediately invalidates the old key.",
    ),
    (
        "api_keys",
        "patch",
        "Explicit typed patch preserves omission versus null: expires_in_days=null clears expiry. name/scopes cannot be null. Legacy update(..., expires_in_days=None) keeps its omission behavior.",
    ),
    (
        "users",
        "list",
        "Compatibility unpaginated array. Project-admin active-employee assignment candidates ignore project_id; use query for project-scoped results. Visibility does not establish lifecycle authority.",
    ),
    (
        "users",
        "query stats export",
        "Scoped management results; use bounded query pages and inspect is_managed/is_lifecycle_managed. Export records audit and streams a wrapped attachment once.",
    ),
    (
        "users",
        "invite bulk_invite",
        "Creates bearer invitations without sending email; fresh invites invalidate earlier pending links. Bulk apply can partially succeed: inspect each item, retry only reconciled failures; CLI prints the receipt then exits 1 if failed>0.",
    ),
    (
        "users",
        "preview_bulk_invites preview_bulk_group_assignment",
        "Read-only preview validates current scope but does not reserve state or perform the apply. Preview failures remain a normal receipt/exit 0.",
    ),
    (
        "users",
        "bulk_group_assignment",
        "group_id is required: UUID assigns, explicit null clears every selected user's group. Apply may partially succeed; CLI prints all results then exits 1 if failed>0. No implicit omission-to-clear.",
    ),
    (
        "users",
        "assign_group",
        "Explicit null clears the association. Server checks the same managed-target scope as bulk assignment before mutation.",
    ),
    (
        "users",
        "preview_role_change change_role",
        "Superadmin-only platform role, distinct from project membership role. Preview is informative; role patch has no CAS token and rechecks compatibility without silently rewriting project duties.",
    ),
    (
        "users",
        "offboarding_preview offboard",
        "Offboarding commit requires the original preview_version and explicit transfer choices. Server checks all related projects; stale 409 needs a fresh preview and new decision, not automatic resubmission.",
    ),
    (
        "users",
        "deactivate reactivate delete",
        "Lifecycle authority is stricter than visibility. Suspension revokes keys; reactivation does not restore them. Delete/transfer and emergency suspension have different work-preservation effects; inspect current obligations before confirming.",
    ),
    (
        "users",
        "admin_reset_password",
        "Returns one-time temporary credentials. CLI requires deliberate secret output and confirmation; model repr hides password/url. No email delivery is implied.",
    ),
    (
        "groups",
        "list query create update delete",
        "Manager-gated global group directory; prefer paged query for agents. Delete clears user associations and requires explicit CLI confirmation. No CAS token.",
    ),
    (
        "invitations",
        "list query stats export",
        "Manager-scoped invitations; scope=all is superadmin-only. Prefer bounded query. Export records audit and streams CSV/JSON once; platform and project roles remain separate.",
    ),
    (
        "invitations",
        "resend",
        "Rotates/revives the bearer token and invalidates the old link without sending email. CLI requires --json or --show-secret before mutation and confirmation.",
    ),
    (
        "invitations",
        "send_email",
        "Sends the current valid invitation token via real SMTP without rotating it. SMTP failure leaves the invitation valid; text output omits its link, deliberate JSON retains API fields.",
    ),
    (
        "invitations",
        "revoke",
        "Revokes the invitation; no account deletion or automatic notification. Requires management authority and CLI confirmation.",
    ),
    (
        "storage_connections",
        "list get create update delete",
        "Visibility is global or creator-scoped; writes require current authority. Secrets are write-only. Config rejects unknown keys and URL userinfo; SFTP has exactly one matching password/key credential. Updates can reuse stored secrets.",
    ),
    (
        "storage_connections",
        "test",
        "Performs a real outbound storage connection after the host guard and writes audit. HTTP 200 can have ok=false; inspect the result, not only status.",
    ),
    (
        "storage_connections",
        "get_allowlist update_allowlist reset_allowlist deployment_sftp_preset",
        "Superadmin-only. Allowlist updates require explicit entries; [] denies all targets. Reset removes the override and restores deployment defaults. Preset supplies no credentials or guard bypass; no CAS token.",
    ),
    (
        "system_settings",
        "get update reset",
        "Superadmin-only effective settings, metadata and opaque version. Password is represented by password_set. PATCH omission/null preserves values, smtp_password='' clears; reset restores deployment defaults. Supply expected_version and reconcile 409 deliberately.",
    ),
    (
        "system_settings",
        "test_smtp",
        "Sends real SMTP email to the current superadmin; limit three tests per minute. CLI requires explicit confirmation before the request.",
    ),
    (
        "audit_logs",
        "list",
        "Project admins see only currently managed-project events, filtered before counts/cursors; superadmins see global events. detail_key/detail_value must be paired; total remains the full filtered count, not remaining rows.",
    ),
    (
        "audit_logs",
        "monthly_summary archives archive export",
        "Superadmin-only. Months use UTC; summary excludes HTTP metadata by default, list/export include it. Export logs itself, streams once, caps at 50,000 rows (413 otherwise); JSON uses items. Archive pagination currently loads the whole month.",
    ),
    (
        "admin_operations",
        "preannotate_queue preannotate_summary alias_frequency",
        "Project-admin results are restricted by current project ownership on the server; superadmins retain global access. Queue includes completed preannotation batches; summary requires enabled backends; alias frequency caps at 200 labels.",
    ),
    (
        "admin_operations",
        "bulk_clear_preannotate",
        "Destructive predictions_only/reset_to_draft modes have different scope. Inspect succeeded/skipped/failed; repeated batch IDs can repeat processing. No idempotency key or automatic write retry.",
    ),
    (
        "admin_operations",
        "system_health analytics",
        "Superadmin diagnostics can include internal details. Inspect response status; analytics only accepts fixed panel names and days 1..365, and may return 503 before DuckDB initialization.",
    ),
    (
        "ml_capabilities",
        "protocol instances",
        "Protocol is static vocabulary; authenticated instances may probe uncached backend setup. SDK omits conditional cache headers to obtain JSON rather than an empty 304.",
    ),
    (
        "ml_diagnostics",
        "overview gpu_resources runtime_hints observe",
        "Operational management reads can inspect live buckets/backends; observe calls health/setup. Current server roles remain authoritative; results may describe unavailable components rather than raise an HTTP error.",
    ),
    (
        "ml_diagnostics",
        "probe smoke_test",
        "Explicit privileged outbound diagnostics allow private/internal HTTP(S) base URLs. No URL userinfo/query/fragment. Probe forwards the supplied bearer token only when enabled; inspect ok under HTTP 200. Smoke test may reload/unload; auto_unloaded does not prove VRAM release.",
    ),
    (
        "project_ml",
        "create_backend update_backend disable_backend",
        "Global backend configuration and project association have distinct authority. Disable removes only this project association; create/update tokens are write-only credentials. No automatic mutation retry.",
    ),
    (
        "project_ml",
        "setup capabilities refresh_capabilities warmup",
        "Checks current project authority and that the registry backend is enabled in the project. Refresh is an explicit live probe; warmup may evict shared weights. Preserve busy/conflict failures rather than forcing a fallback.",
    ),
    (
        "project_ml",
        "interactive interactive_frame predict_frame predict_test",
        "Server checks backend/project and task relationships. Frame paths use multipart image plus JSON context/config. context.mask_prompt_source also requires annotations:read. predict-test is transient; predict-frame saves Prediction; no automatic retry.",
    ),
    (
        "project_ml",
        "reload unload",
        "Superadmin-only global shared-model lifecycle; project_id is audit context, not a private copy. Quiescence/busy checks remain authoritative; no force or fallback after 409/503.",
    ),
    (
        "projects",
        "query",
        "Paged current-user-visible projects, page_size 1..100. Repeated type_key/data_type filters and current project roles are independent of account roles; date validation and authority remain server-owned.",
    ),
    (
        "projects",
        "readiness class_usage",
        "Requires project owner/superadmin. Readiness is current project setup evidence, not proof that a backend or worker is currently ready to execute.",
    ),
    (
        "projects",
        "rename_class",
        "Requires project management and explicit CLI confirmation. tool_unit_id limits configuration updates, while matching annotations are renamed across the project. No CAS token or automatic POST retry.",
    ),
    (
        "projects",
        "cleanup_annotation_orphans",
        "Defaults to dry_run=true. Apply deactivates orphan-class annotations and prunes orphan user attributes under task/annotation locks; no preview digest is bound to the later request. CLI confirms actual apply.",
    ),
    (
        "projects",
        "list_datasets mention_candidates",
        "Project-visible unpaginated arrays. Dataset relation IDs and mention user IDs are not interchangeable with task or membership-row IDs.",
    ),
    (
        "projects",
        "preview_orphan_tasks cleanup_orphan_tasks",
        "Owner/superadmin only. Preview returns counts without a CAS token; cleanup recomputes source-less tasks and deletes related work. No automatic retry after ambiguity; inspect current state before resubmitting.",
    ),
    (
        "projects",
        "transfer",
        "Superadmin-only ownership transfer to an active project_admin account. Server locks account/project and rechecks the target; project ownership, not membership role, changes. CLI confirms before the request.",
    ),
    (
        "projects",
        "lidar_export_preflight",
        "Requires export.annotations capability. Returns a current readiness report capped at 200 issues, not a binding digest or authorization to skip export-time revalidation.",
    ),
    (
        "batches",
        "unclassified_count",
        "Project-visible count of tasks without a batch. Count is live evidence, not a reservation for a later split.",
    ),
    (
        "batches",
        "audit_logs",
        "limit 1..200. Live batch/project pairing is checked; deleted direct history requires reliable project detail or exact project/batch path. Returns raw array with detail, not the global audit page shape.",
    ),
    (
        "batches",
        "distribution_preview distribution_apply assignment_preview assignment_apply",
        "Project owner/superadmin only. Apply preserves the exact reviewed selection, recipient fields and preview_version; 409 requires a fresh preview and deliberate decision. Assignment fields are required nullable targets: null explicitly clears a role. Recipients must currently belong to the project with the matching role.",
    ),
    (
        "batches",
        "split",
        "Splits only unclassified/default-batch tasks. id_range.item_ids are dataset-item UUIDs, not task UUIDs; scene projects require by_scene. Random n_batches cannot exceed the current splittable task count. Creates batches without an idempotency key; CLI confirms.",
    ),
    (
        "batches",
        "bulk_archive bulk_delete",
        "Owner/superadmin, 1..200 batch IDs; inspect succeeded/skipped/failed. CLI prints the full receipt then exits 1 on failures. force deletion can reset work and remove predictions; no automatic resubmission of the full partial batch.",
    ),
    (
        "batches",
        "admin_lock admin_unlock",
        "Owner/superadmin and exact parent relationship required. Changes batch editing access and sends in-app notifications; lock reason is audited. CLI confirms before effects.",
    ),
    (
        "batches",
        "submit_review",
        "Assigned batch annotator or manager only, from annotating/reviewing. Commits bounded chunks and can dispatch Mask QC; submitted/skipped/remaining is a partial progress receipt, not an atomic all-or-nothing result.",
    ),
    (
        "batches",
        "lidar_preflight export download_voc",
        "Requires export.annotations capability, matching project/batch and current server export guards. LiDAR preflight is advisory; export rechecks readiness. Annotator/viewer membership alone cannot authorize an export job or synchronous VOC bytes.",
    ),
    (
        "datasets",
        "upload_init",
        "Active account and datasets:write. Reserves a staged upload without creating an Item or Task. Reuse a saved UUID for uncertain pending initialization; a completed intent cannot issue another PUT URL. No automatic retry.",
    ),
    (
        "datasets",
        "upload_complete",
        "Active account and datasets:write; modern uploads require the original actor and matching parent. Copies the frozen staging generation and commits Item, task append and receipt together. Save this phase UUID for uncertain legacy completion; missing/duplicate returns400/409 without deleting old sources. Successful media dispatch is asynchronous. No automatic retry.",
    ),
    (
        "datasets",
        "import_from_connection",
        "Platform manager operation on the global dataset catalogue. Rechecks connector visibility, host guard and source path, freezes import budgets, then returns HTTP 202 job_id. Save the optional caller-supplied UUID idempotency_key and exact request to replay an uncertain acceptance. Observe that AsyncJob explicitly; no automatic retry.",
    ),
    (
        "datasets",
        "upload_zip",
        "Active account with datasets:write. Synchronously imports one ZIP and returns the added/deduped/skipped/error receipt. Pass a caller-saved UUID idempotency_key to reconcile an uncertain response; SDK and CLI do not generate or retry automatically.",
    ),
    (
        "datasets",
        "scan_items",
        "Platform manager. Synchronously scans/imports items; new_items and linked_tasks describe the scan, not completed media work. Pass a caller-saved UUID idempotency_key to reconcile an uncertain response; SDK and CLI do not generate or retry automatically.",
    ),
    (
        "datasets",
        "backfill_dimensions",
        "Platform manager. Returns a durable dimensions-job receipt, not processed counts or per-item progress. The legacy batch query is accepted but ignored; the owner scans in bounded pages. Reuse the Idempotency-Key after a lost response; no automatic POST retry or implicit wait.",
    ),
    (
        "datasets",
        "backfill_media",
        "Platform manager. Returns a durable image-thumbnail job receipt while retaining the dataset video-metadata backfill path; the receipt is not per-item progress. Reuse the Idempotency-Key after a lost response; no automatic POST retry or implicit wait.",
    ),
    (
        "datasets",
        "backfill_scenes",
        "Platform manager, mode auto/single/per_subdirectory. Default dry_run=false writes inferred scene/frame metadata; existing scene inference may return notes instead of changing it. CLI confirms actual writes.",
    ),
    (
        "datasets",
        "sniff_axis_convention",
        "Platform manager. Read-only axis analysis despite POST; returned candidates/confidence do not update dataset configuration.",
    ),
    (
        "guide_assets",
        "upload_init",
        "Project owner/superadmin. Allows PNG/JPEG/WebP/GIF/SVG up to 5 MiB and returns key plus a 900-second signed PUT URL. Creates no dataset item; do not retry initialization automatically.",
    ),
    (
        "guide_assets",
        "upload_bytes",
        "Explicit bare signed PUT with the initialization Content-Type and no platform credentials. Accepts only 2xx, follows no redirects, and never calls API completion. Preserve the same key/URL and reconcile ambiguous uploads.",
    ),
    (
        "guide_assets",
        "upload_complete",
        "Project owner/superadmin; checks key/project and uploaded size before registering metadata. Repeating completion can register the same key again; inspect current project assets before resubmitting. No idempotency key.",
    ),
    (
        "guide_assets",
        "delete sign_url",
        "Checks key/project and current authorization. Signed GET URL lasts 3600 seconds. Delete removes metadata even if the storage deletion fails, so success is not proof the object was physically erased.",
    ),
    (
        "project_performance",
        "members member events export_members",
        "Project owner/superadmin only; member_id is the user UUID, with current/historical roster eligibility rechecked. Date scope defaults to Asia/Shanghai and at most 90 local days. Cursors are offset-based and must reset when filters change. Preserve null/partial/unknown metric coverage rather than inventing zero.",
    ),
    (
        "project_performance",
        "export_members",
        "Exports the entire filtered roster as CSV with scope headers, not a cursor page; SDK omits cursor/limit. API constructs the full payload in memory. Client atomically streams it to the requested destination; no AsyncJob or export audit event is implied.",
    ),
    (
        "search",
        "query",
        "Each entity class has its own limit 1..20. Projects/members use current visibility; tasks also enforce assignment/batch role before LIMIT and preserve collaborative-video visibility. Datasets follow the global authenticated catalogue policy.",
    ),
    (
        "dashboard",
        "person_detail people people_export",
        "Project admins must supply their owned project; detail target must be a member. Counts and timeline use the selected project, filtering before the latest-50 limit; unscoped events are omitted. Superadmin can explicitly request global scope.",
    ),
    (
        "dashboard",
        "people_export",
        "Full in-memory UTF-8 BOM CSV from the unpaginated people list, atomically streamed by SDK. No API audit write or AsyncJob is produced; explicit CLI destination overwrite confirmation still applies.",
    ),
    (
        "dashboard",
        "annotator_onboarding annotator_batches reviewer_today_mini recent_reviews",
        "Current-actor views, not global team metrics. Annotator batches are bounded to 100; reviewer mini and recent reviews preserve current membership/actor authority. Server remains authoritative after role changes.",
    ),
    (
        "dashboard",
        "prediction_cost_stats",
        "Superadmin-only global cost panel; range is 7d or 30d. Internal statistics failures can fall back to zero values, so zero is not proof no inference ran.",
    ),
    (
        "failed_predictions",
        "list",
        "Platform manager plus predictions:read for API keys; project admins see only owned-project rows. Page/page_size are bounded; include_dismissed changes soft-hidden visibility.",
    ),
    (
        "failed_predictions",
        "retry dismiss restore",
        "Platform manager and target-project ownership; no additional API-key write scope is declared here. Retry returns HTTP 202 with failed_id, not an AsyncJob ID, and has a three-attempt ceiling. Dismiss/restore are idempotent soft-state changes; CLI confirms all mutations.",
    ),
    (
        "predictions",
        "purge",
        "Owner/superadmin role gate without an additional API-key scope. Default source_scope=external_import; all can include ML and unknown predictions. Inspect dry-run counts first, then explicitly confirm deletion. Exact task selection stays server-validated; no implicit conversion to annotation deletion.",
    ),
    (
        "annotations",
        "import_file",
        "Owner/superadmin plus annotations:write for API keys. One multipart file, AAP JSON only, at most 64 MiB. Returns a synchronous partial receipt, not a job; overwrite replaces only imported annotations. CLI emits full JSON then exits 1 on errors. No idempotency key or automatic POST retry.",
    ),
]:
    for _method in _methods.split():
        METHOD_NOTES.setdefault(f"{_namespace}.{_method}", {}).setdefault(
            "constraints", []
        ).append(_note)


for _namespace, _methods, _note in [
    (
        "comments",
        "list page task_page annotation_counts",
        "Current actual task visibility is rechecked. list is a legacy unpaginated array; page/task_page use parent-bound keyset cursors with limit 1-200. Counts include only visible active annotation comments.",
    ),
    (
        "comments",
        "discussion_page",
        "Mixed comments/feedback feed with all/annotation scope and server-owned action flags. Cursor is bound to parent and filters; preserve item source and actions rather than inferring permissions from author IDs.",
    ),
    (
        "comments",
        "create update delete",
        "Requires current task access and author/project management authority for edits/deletes. Empty body needs attachments or canvas drawing. Delete is soft; comments can mirror feedback, audit and mention notifications. No automatic retry.",
    ),
    (
        "comments",
        "init_attachment_upload upload_attachment",
        "Initialization returns a 900-second signed PUT target. upload_attachment streams bytes without platform credentials, does not follow redirects, and returns registration metadata; caller must explicitly create/update the comment.",
    ),
    (
        "comments",
        "attachment_download_url download_attachment",
        "The API verifies registered key and current task visibility. JSON URL and explicit HTTP 302 forms expire after 300 seconds. Download follows only an absolute HTTP(S) Location through a credential-free client and requires target HTTP 200; temporary URLs are hidden from repr, retained in intentional JSON.",
    ),
    (
        "annotation_history",
        "annotation task",
        "Unpaginated latest-first history for an actually visible task, including soft-deleted records. Annotation project metadata does not override its actual task authority; the server remains authoritative after reassignment.",
    ),
    (
        "bug_reports",
        "list cluster",
        "Global manager policy: both super_admin and platform project_admin can manage the report queue, independent of report project metadata. list uses limit/offset (limit <=200). cluster computes suggestions for up to 50 new reports without changing their state.",
    ),
    (
        "bug_reports",
        "list_mine get",
        "mine is current reporter only; detail permits that reporter or global managers. Report project/task context is diagnostic metadata and does not grant project authority. get returns typed JSON; list(format=markdown) returns text/markdown.",
    ),
    (
        "bug_reports",
        "create update delete add_comment",
        "Reporters may update title/description/severity; global managers own status, assignment, duplicate and resolution fields. Only nullable administrative fields can be cleared. Delete is permanent. Reporter comments on closed reports may reopen them to triaged with a five-per-day limit; comments have a 60/hour limit. Mutations are not retried.",
    ),
    (
        "bug_reports",
        "init_screenshot_upload upload_screenshot download_attachment",
        "Signed screenshot uploads accept PNG/JPEG/WebP and return metadata for explicit report registration. The 10 MiB size field validates caller metadata, not actual stored size. Downloads validate explicit 302 and use a credential-free HTTP 200 stream; no redirect/retry or credential-bearing URL errors.",
    ),
    (
        "mask_formats",
        "list export_preflight",
        "Descriptors reflect project media type and server adapters; SDK does not convert masks. Export preflight requires export.annotations and returns an advisory plan/digest; actual project export does not consume this digest.",
    ),
    (
        "mask_formats",
        "upload_init upload_bytes import_preflight",
        "Import requires explicit signed upload followed by preflight of exact source SHA, mapping and options. PUT uses no platform credentials and accepts 2xx only. Upload URL expires after 900 seconds; preflight receipt after 15 minutes. Credentials/receipts are hidden from repr, retained in intentional JSON.",
    ),
    (
        "mask_formats",
        "execute_import get_import resume_import",
        "Execute exact actor-owned receipt and plan_digest; lossy plans need explicit confirm_lossy. Domain import batch and AsyncJob statuses differ. Resume is server-gated for eligible failed imports. Never refresh proofs or retry POST automatically.",
    ),
    (
        "mask_qc",
        "create_run list_issues task_summary get_issue patch_issue",
        "Requires review authority and actual affected task visibility; project-wide scans require project management. Runs can reuse the same current input and HTTP 202 is not completion. Issues use keyset pages <=200; stale/busy updates remain conflicts.",
    ),
    (
        "mask_qc",
        "compare compare_content issue_region_content",
        "Keep exact annotation version, content digest and source frame. Content is COCO RLE JSON, not image bytes; no conditional cache headers are sent. tracker_candidate content uses video_tracker.get_mask_content with returned candidate_job_id and digest.",
    ),
    (
        "mask_qc",
        "dry_run_repairs execute_repairs get_repair_batch resume_repairs rollback_repairs",
        "Dry-run persists a plan without editing annotations. Execute exact 15-minute actor-owned receipt/digest. Preserve skipped/partial item results and domain status separately from AsyncJob. Resume only eligible failed/partial batches; rollback completed/partial batches within retention using expected_result_digest. CLI prints failed/partial receipts before exit 1; rollback is explicit, never automatic.",
    ),
    (
        "point_cloud_quality",
        "create_run get_run list_issues get_issue patch_issue",
        "Requires review.write and actual task visibility; project/scene scans require management. Run creation may reuse current source/config/scope. Dispatch failure returns reason, run_id and async_job_id for persisted failed records; query before resubmission. Issues retain scene/frame/annotation anchors; stale/busy changes return conflict.",
    ),
    (
        "point_cloud_quality",
        "list_evaluations create_evaluation get_evaluation promote_evaluation",
        "Evaluation freezes reviewed non-stale samples synchronously. Candidate config is strict, readback permits additive fields. Eligibility is a gate, not promotion. Promotion requires project management and exact baseline config revision/digest CAS; no automatic re-evaluation or conflict refresh.",
    ),
]:
    for _method in _methods.split():
        METHOD_NOTES.setdefault(f"{_namespace}.{_method}", {}).setdefault(
            "constraints", []
        ).append(_note)

for _method in ("list_issues", "get_issue"):
    METHOD_NOTES.setdefault(f"point_cloud_quality.{_method}", {})["domain_effects"] = [
        "This read can persist stale issue status against current inputs."
    ]
METHOD_NOTES.setdefault("mask_qc.dry_run_repairs", {})["domain_effects"] = [
    "Persists a dry-run plan and receipt without mutating annotations."
]


for _namespace, _methods, _note in [
    (
        "task_context",
        "annotations_page neighbors neighbor_annotations mask_capabilities predictions",
        "Current actual task visibility remains authoritative. Annotation pages preserve opaque cursor/limit1-1000 and require video_segment_id for collaborative video. Neighbor UUIDs are navigation metadata, including unavailable placeholders; they do not grant read/write authority. Prediction shapes preserve original shape_index after filtering and require annotations:read for API keys.",
    ),
    (
        "image_pyramids",
        "manifest asset_urls",
        "Manifest GET can invalidate stale generations and enqueue generation when enabled; shared GET retries still apply. Sequential retries observe committed invalidation, but concurrent stale readers can race into duplicate Celery dispatch. Signed URLs expire and asset requests bind current generation with at most128 assets. No implicit URL refresh or generation wait.",
    ),
    (
        "image_pyramids",
        "retry",
        "Explicit rate/cooldown-gated generation retry, HTTP202 with a Celery identifier, not necessarily an AsyncJob. Do not pass that identifier to jobs.wait or treat acceptance as ready media.",
    ),
    (
        "cross_frame_jobs",
        "list create retry_failed",
        "Current-actor list/retry; creation requires editable source and visible/editable targets, at most100 frames and500 unique box_3d sources. Active identical work is server-singleflight, not caller idempotency. Observe actual AsyncJob responses via jobs; retry only eligible terminal failed frames.",
    ),
    (
        "task_locks",
        "acquire heartbeat release",
        "Explicit current-actor task lease with300-second TTL. Acquisition can take over eligible old locks; heartbeat can return404. unique_id is not a CAS token. Release affects the current actor lease. CLI confirms effects; no background renewal thread or automatic acquire/retry.",
    ),
    (
        "point_cloud",
        "manifest camera_members calibration_history",
        "Requires actual task visibility; manifest requires lidar. Camera member track must match actual task scene, not only project. Readback retains current versus stored calibration relations and virtual legacy calibration revision1. Manifest URLs are expiring and hidden in repr/default text, retained in intentional JSON.",
    ),
    (
        "point_cloud",
        "create_camera_member update_camera_member delete_camera_member restore_camera_member update_calibration",
        "Exact task/source/member/track/camera parent relationships and annotations:write. Create uses track revision and calibration revision/digest; update/restore also use member version. Delete soft-deactivates and changes track revision. Calibration writes require project management; data items shared across projects require superadmin. Preserve finite fixed matrix shapes and all supplied CAS fields; no refresh/retry.",
    ),
    (
        "scene_tracks",
        "scene_timeline get_track diagnostics list_command_operations list_track_operation_candidates",
        "Timeline is limited to200 inclusive frames and omits selected-track presence for unavailable frames. Track/diagnostic reads can require whole-scene task visibility. Candidate GET requires write authority and limit<=20; command history and diagnostics have separate bounds. Metadata is not later authorization.",
    ),
    (
        "scene_tracks",
        "preview_command execute_command revert_command preview_track_operation execute_track_operation",
        "Preferred commands bind exact preview snapshot_token and caller idempotency_key; revert uses original operation_id. Multi-task writes require all affected task visibility/editability. Legacy execution generates the key server-side, so an uncertain response has no safe caller replay. No automatic conflict refresh or fallback to legacy execution.",
    ),
    (
        "annotation_operations",
        "conversion_dry_run conversion_execute interpolate_range propagate_batch propagate_to_task secondary_inference commit_mask_mutations commit_polygon_slice restore_slice accept_prediction reject_prediction accept_ai_mask_candidate",
        "Exact source/target task and annotation relationships, current task editability and segment/write authority. Conversion proofs expire after10min and bind actor/task with explicit replace/lossy decisions. Mask commits preserve whole-scope versions/fingerprint; AI acceptance preserves candidate receipt and required If-Match. Original prediction shape_index is not filtered list position. Dynamic secondary inference params are backend-specific. Mutations are single-attempt; no invented idempotency or proof refresh.",
    ),
]:
    for _method in _methods.split():
        METHOD_NOTES.setdefault(f"{_namespace}.{_method}", {}).setdefault(
            "constraints", []
        ).append(_note)
METHOD_NOTES.setdefault("image_pyramids.manifest", {})["domain_effects"] = [
    "May invalidate stale generations and enqueue image-pyramid generation."
]


def _attribute_path(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return list(reversed(parts))


def _placeholder(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        value = node.slice
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise ValueError(f"unsupported path placeholder: {ast.dump(node)}")


def _path_template(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        raise ValueError(
            f"HTTP path must be a literal or simple f-string: {ast.dump(node)}"
        )
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append("{" + _placeholder(value.value) + "}")
        else:
            raise ValueError(f"unsupported HTTP path expression: {ast.dump(node)}")
    return "".join(parts)


def _literal(node: ast.expr) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise ValueError(f"HTTP method must be a string literal: {ast.dump(node)}")
    return node.value.upper()


def _source_default(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return ast.unparse(node)


def _signature(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [_MISSING] * (len(positional) - len(args.defaults)) + list(args.defaults)
    parameters: list[dict[str, Any]] = []
    for arg, default in zip(positional, defaults, strict=True):
        if arg.arg == "self":
            continue
        parameters.append(
            {
                "name": arg.arg,
                "kind": "positional_only"
                if arg in args.posonlyargs
                else "positional_or_keyword",
                "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
                "required": default is _MISSING,
                "default": _source_default(default)
                if default is not _MISSING
                else None,
            }
        )
    if args.vararg:
        parameters.append(
            {
                "name": args.vararg.arg,
                "kind": "var_positional",
                "annotation": ast.unparse(args.vararg.annotation)
                if args.vararg.annotation
                else None,
                "required": False,
                "default": None,
                "opaque": True,
            }
        )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(
            {
                "name": arg.arg,
                "kind": "keyword_only",
                "annotation": ast.unparse(arg.annotation) if arg.annotation else None,
                "required": default is None,
                "default": _source_default(default) if default is not None else None,
            }
        )
    if args.kwarg:
        parameters.append(
            {
                "name": args.kwarg.arg,
                "kind": "var_keyword",
                "annotation": ast.unparse(args.kwarg.annotation)
                if args.kwarg.annotation
                else None,
                "required": False,
                "default": None,
                "opaque": True,
            }
        )
    return {
        "parameters": parameters,
        "return_type": ast.unparse(function.returns) if function.returns else None,
        "python_only": any(
            param.get("opaque")
            or (param.get("annotation") and "Callable" in param["annotation"])
            for param in parameters
        ),
    }


def _client_namespaces() -> dict[str, str]:
    tree = ast.parse((SOURCE / "client.py").read_text(encoding="utf-8"))
    client = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Client"
    )
    init = next(
        node
        for node in client.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    mapping: dict[str, str] = {}
    for node in ast.walk(init):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                mapping[value.func.id] = target.attr
    return mapping


def _is_http_call(node: ast.Call, method: str) -> bool:
    path = _attribute_path(node.func)
    return path == ["self", "_http", method]


def _is_external_url(node: ast.expr) -> bool:
    path = _attribute_path(node)
    return bool(path and (path[-1] == "url" or path[-1].endswith("_url")))


def _method_target(
    chain: list[str] | None,
    namespace: str,
    namespaces: set[str],
    injected_namespaces: dict[str, str],
) -> str | None:
    if not chain or chain[0] != "self" or len(chain) < 2 or chain[1] == "_http":
        return None
    if chain[1] in namespaces and len(chain) >= 3:
        return ".".join(chain[1:])
    if chain[1] in injected_namespaces and len(chain) >= 3:
        return ".".join((injected_namespaces[chain[1]], *chain[2:]))
    if namespace:
        return f"{namespace}.{chain[1]}"
    return chain[1]


def _sdk_declarations() -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    namespace_by_class = _client_namespaces()
    namespaces = set(namespace_by_class.values())
    injected_by_class: dict[str, dict[str, str]] = {}
    methods: dict[str, dict[str, Any]] = {}
    function_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    calls: dict[str, set[str]] = defaultdict(set)
    operation_methods: dict[str, set[str]] = defaultdict(set)

    for source_path in sorted(SOURCE.glob("*.py")):
        if source_path.name.startswith("_") and source_path.name != "__init__.py":
            continue
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        module = f"ai_annotation.{source_path.stem}"
        for class_node in (
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ):
            if (
                class_node.name != "Client"
                and class_node.name not in namespace_by_class
            ):
                continue
            namespace = namespace_by_class.get(class_node.name, "")
            init = next(
                (
                    node
                    for node in class_node.body
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__"
                ),
                None,
            )
            if init is not None:
                argument_names = {
                    argument.arg: namespace_by_class.get(
                        argument.annotation.id
                        if isinstance(argument.annotation, ast.Name)
                        else ""
                    )
                    for argument in init.args.args
                    if argument.annotation is not None
                }
                for assignment in ast.walk(init):
                    if not isinstance(assignment, ast.Assign) or not isinstance(
                        assignment.value, ast.Name
                    ):
                        continue
                    injected = argument_names.get(assignment.value.id)
                    if not injected:
                        continue
                    for target in assignment.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            injected_by_class.setdefault(class_node.name, {})[
                                target.attr
                            ] = injected
            for function in class_node.body:
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if function.name.startswith("_"):
                    continue
                if class_node.name == "Client" and function.name != "me":
                    continue
                name = f"{namespace}.{function.name}" if namespace else function.name
                methods[name] = {
                    "name": name,
                    "module": module,
                    "class": class_node.name,
                    "method": function.name,
                    **_signature(function),
                    "constructed_models": sorted(
                        {
                            call.func.id
                            for call in ast.walk(function)
                            if isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id[:1].isupper()
                        }
                    ),
                    "direct_api_operations": [],
                    "api_operations": [],
                    "calls": [],
                    "external_http": False,
                }
                function_nodes[name] = function

    for name, function in function_nodes.items():
        namespace = name.rsplit(".", 1)[0] if "." in name else ""
        class_name = methods[name]["class"]
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if class_name == "Streams" and _attribute_path(call.func) == [
                "self",
                "_connect",
            ]:
                channel = call.args[0]
                if not isinstance(channel, ast.Constant) or not isinstance(
                    channel.value, str
                ):
                    raise ValueError(f"{name}: stream channel must be a literal")
                methods[name]["stream_channel"] = channel.value
                methods[name]["websocket_path"] = _path_template(call.args[1])
                methods[name]["protocol"] = "websocket"
                methods[name]["python_only"] = True
                methods[name]["invocation_note"] = (
                    "Use async with and async iteration. The CLI provides bounded NDJSON listeners; a stream context is not a JSON result."
                )
                continue
            method = None
            root_path = False
            path_node: ast.expr | None = None
            if _is_http_call(call, "request"):
                if len(call.args) < 2:
                    raise ValueError(f"{name}: request needs method and path")
                method = _literal(call.args[0])
                path_node = call.args[1]
                root_option = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "root_path"
                    ),
                    None,
                )
                if root_option is not None:
                    if (
                        not isinstance(root_option, ast.Constant)
                        or type(root_option.value) is not bool
                    ):
                        raise ValueError(f"{name}: root_path must be a literal boolean")
                    root_path = root_option.value
            elif _is_http_call(call, "put_presigned"):
                methods[name]["external_http"] = True
                continue
            elif _is_http_call(call, "stream_download"):
                path_node = (
                    call.args[0]
                    if call.args
                    else next(
                        (
                            keyword.value
                            for keyword in call.keywords
                            if keyword.arg == "url"
                        ),
                        None,
                    )
                )
                if path_node is None:
                    continue
                if _is_external_url(path_node):
                    methods[name]["external_http"] = True
                    continue
                method_node = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "method"
                    ),
                    None,
                )
                method = _literal(method_node) if method_node is not None else "GET"
            if method is not None and path_node is not None:
                path = _path_template(path_node)
                if path.startswith(("http://", "https://")):
                    methods[name]["external_http"] = True
                    continue
                if not path.startswith("/"):
                    raise ValueError(f"{name}: HTTP path must start with /")
                if not root_path and not path.startswith("/api/v1/"):
                    path = "/api/v1" + path
                operation = f"{method} {path}"
                methods[name]["direct_api_operations"].append(operation)
                operation_methods[operation].add(name)
                continue
            target = _method_target(
                _attribute_path(call.func),
                namespace,
                namespaces,
                injected_by_class.get(class_name, {}),
            )
            if target and target != name:
                calls[name].add(target)

    for name, method in methods.items():
        method["direct_api_operations"] = sorted(set(method["direct_api_operations"]))
        method["calls"] = sorted(target for target in calls[name] if target in methods)
        method.update(METHOD_NOTES.get(name, {}))

    def resolve(name: str, visiting: set[str]) -> set[str]:
        if name in visiting:
            return set()
        method = methods[name]
        operations = set(method["direct_api_operations"])
        for callee in method["calls"]:
            operations.update(resolve(callee, {*visiting, name}))
        return operations

    for name, method in methods.items():
        method["api_operations"] = sorted(resolve(name, set()))
    methods = {
        name: method
        for name, method in methods.items()
        if method["api_operations"]
        or method["external_http"]
        or method.get("stream_channel")
    }
    operation_sdk = {
        operation: sorted(names) for operation, names in operation_methods.items()
    }
    return operation_sdk, methods


def _click_commands() -> list[dict[str, Any]]:
    sys.path.insert(0, str(PACKAGE / "src"))
    from ai_annotation.cli.main import app

    commands: list[dict[str, Any]] = []

    def add_command(info: Any, path: tuple[str, ...]) -> None:
        callback = getattr(info, "callback", None)
        if callback is None:
            return
        source_file = inspect.getsourcefile(callback)
        callback_name = getattr(callback, "__name__", None)
        callback_tree = None
        function_nodes = {}
        if source_file and callback_name:
            module_tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
            function_nodes = {
                node.name: node
                for node in module_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            callback_tree = function_nodes.get(callback_name)
        if callback_tree is None:
            return
        sdk_calls: set[str] = set()
        confirmation = False

        def collect_calls(
            function: ast.FunctionDef | ast.AsyncFunctionDef, visited: set[str]
        ) -> None:
            nonlocal confirmation
            if function.name in visited:
                return
            visited.add(function.name)
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                chain = _attribute_path(node.func)
                if chain and chain[0] == "client" and len(chain) >= 2:
                    sdk_calls.add(".".join(chain[1:]))
                elif isinstance(node.func, ast.Name) and node.func.id in function_nodes:
                    collect_calls(function_nodes[node.func.id], visited)
                if chain and chain[-1] == "confirm_destructive":
                    confirmation = True

        collect_calls(callback_tree, set())
        params = []
        for parameter in inspect.signature(callback).parameters.values():
            declaration = parameter.default
            is_typer_declaration = hasattr(declaration, "param_decls")
            options = list(getattr(declaration, "param_decls", ()) or ())
            options = [
                option
                for option in options
                if isinstance(option, str) and option.startswith("-")
            ]
            if not is_typer_declaration:
                options = ["--" + parameter.name.replace("_", "-")]
            default = getattr(declaration, "default", declaration)
            if default is ...:
                default = None
            if not isinstance(default, (str, int, float, bool, type(None), list, dict)):
                default = str(default)
            params.append(
                {
                    "name": parameter.name,
                    "kind": "option" if options else "argument",
                    "options": options,
                    "required": getattr(declaration, "default", declaration) is ...,
                    "default": default,
                    "type": str(parameter.annotation)
                    if parameter.annotation is not inspect.Parameter.empty
                    else None,
                    "description": getattr(declaration, "help", None),
                }
            )
        command_name = info.name or callback_name.removesuffix("_").replace("_", "-")
        commands.append(
            {
                "path": " ".join(("aap", *path, command_name)),
                "alias": ".".join((*path, command_name)),
                "parameters": params,
                "sdk_methods": sorted(sdk_calls),
                "confirmation": confirmation,
            }
        )

    def visit(app_info: Any, path: tuple[str, ...]) -> None:
        for info in getattr(app_info, "registered_commands", ()):
            add_command(info, path)
        for group in getattr(app_info, "registered_groups", ()):
            if group.name:
                visit(group.typer_instance, (*path, group.name))

    visit(app, ())
    return commands


def _schema_descriptor(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not schema:
        return None
    allowed = (
        "$ref",
        "title",
        "type",
        "format",
        "enum",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "description",
    )
    result = {key: schema[key] for key in allowed if key in schema}
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            result[keyword] = [_schema_descriptor(item) for item in schema[keyword]]
    if isinstance(schema.get("items"), dict):
        result["items"] = _schema_descriptor(schema["items"])
    if isinstance(schema.get("additionalProperties"), dict):
        result["additionalProperties"] = _schema_descriptor(
            schema["additionalProperties"]
        )
    elif "additionalProperties" in schema:
        result["additionalProperties"] = schema["additionalProperties"]
    return result


def generate_catalog() -> dict[str, Any]:
    spec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    coverage = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    operations = coverage["operations"]
    sdk_by_operation, sdk_methods = _sdk_declarations()
    cli_commands = _click_commands()
    cli_by_operation: dict[str, set[str]] = defaultdict(set)
    for command in cli_commands:
        for sdk_method in command["sdk_methods"]:
            if sdk_method in sdk_methods:
                sdk_methods[sdk_method].setdefault("cli_aliases", []).append(
                    command["alias"]
                )
                for operation in sdk_methods[sdk_method]["api_operations"]:
                    cli_by_operation[operation].add(command["alias"])
    for method in sdk_methods.values():
        if method.get("external_http"):
            method["indirect_note"] = (
                "This method also streams from a returned or presigned URL; that URL has no fixed route in the catalog."
            )
        if "cli_aliases" in method:
            method["cli_aliases"] = sorted(set(method["cli_aliases"]))

    openapi_keys = {
        f"{method.upper()} {path}"
        for path, path_item in spec["paths"].items()
        for method in path_item
        if method.lower() in HTTP_METHODS
    }
    if set(operations) != openapi_keys:
        raise ValueError(
            "coverage manifest differs from OpenAPI snapshot: "
            f"missing={sorted(openapi_keys - set(operations))}; "
            f"extra={sorted(set(operations) - openapi_keys)}"
        )

    result: list[dict[str, Any]] = []
    for path, path_item in spec["paths"].items():
        for method, operation_data in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            key = f"{method.upper()} {path}"
            entry = operations[key]
            request_body = operation_data.get("requestBody")
            request_content = request_body.get("content", {}) if request_body else {}
            responses = {}
            for code, response in sorted(operation_data.get("responses", {}).items()):
                content = {}
                for media_type, media in sorted(response.get("content", {}).items()):
                    descriptor = _schema_descriptor(media.get("schema"))
                    if descriptor:
                        content[media_type] = {"schema": descriptor}
                responses[code] = {
                    "description": response.get("description"),
                    **({"content": content} if content else {}),
                }
            sdk_names = sdk_by_operation.get(key, [])
            cli_aliases = sorted(cli_by_operation.get(key, set()))
            row: dict[str, Any] = {
                "name": key,
                "method": method.upper(),
                "path": path,
                "operation_id": operation_data.get("operationId"),
                "summary": operation_data.get("summary"),
                "description": operation_data.get("description"),
                "tags": sorted(operation_data.get("tags", [])),
                "status": entry["status"],
                "sdk_methods": sdk_names,
                "cli_aliases": cli_aliases,
                "parameters": [
                    {
                        "name": parameter["name"],
                        "in": parameter["in"],
                        "required": parameter.get("required", False),
                        **(
                            {"description": parameter["description"]}
                            if parameter.get("description")
                            else {}
                        ),
                        **(
                            {"schema": _schema_descriptor(parameter.get("schema"))}
                            if parameter.get("schema")
                            else {}
                        ),
                    }
                    for parameter in operation_data.get("parameters", [])
                ],
                "responses": responses,
            }
            if entry.get("reason"):
                row["reason"] = entry["reason"]
            if entry.get("release"):
                row["release"] = entry["release"]
            if request_body:
                row["request_body"] = {
                    "required": request_body.get("required", False),
                    "content": {
                        media_type: {"schema": _schema_descriptor(media.get("schema"))}
                        for media_type, media in sorted(request_content.items())
                        if _schema_descriptor(media.get("schema"))
                    },
                }
            result.append(row)
    result.sort(key=lambda item: item["name"])
    cli_commands = sorted(cli_commands, key=lambda item: item["alias"])
    for command in cli_commands:
        command["sdk_methods"] = sorted(
            name for name in command["sdk_methods"] if name in sdk_methods
        )
    from ai_annotation.streams import describe_stream_channel, list_stream_channels

    channels = [describe_stream_channel(row["name"]) for row in list_stream_channels()]
    channel_paths = {row["name"]: row["path"] for row in channels}
    actual_streams = {
        method["stream_channel"]: method["websocket_path"]
        for method in sdk_methods.values()
        if method.get("stream_channel")
    }
    if actual_streams != channel_paths:
        raise ValueError("public stream declarations differ from channel metadata")
    return {
        "schema_version": 1,
        "stream_channels": channels,
        "openapi_version": spec["info"].get("version"),
        "guide_resource": "ai_annotation/AGENT_GUIDE.md",
        "authority": "Descriptions do not grant permission or prove runtime availability; the server remains authoritative.",
        "invocation_note": "This catalog describes concrete SDK and CLI interfaces; it does not provide a generic JSON request executor.",
        "operation_count": len(result),
        "operations": result,
        "sdk_methods": [sdk_methods[name] for name in sorted(sdk_methods)],
        "cli_commands": cli_commands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if generated catalog is stale"
    )
    args = parser.parse_args()
    generated = (
        json.dumps(generate_catalog(), ensure_ascii=False, indent=2) + "\n"
    ).encode()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != generated:
            print(
                f"{OUTPUT.relative_to(ROOT)} is stale; run {Path(__file__).relative_to(ROOT)}"
            )
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is current")
        return 0
    OUTPUT.write_bytes(generated)
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

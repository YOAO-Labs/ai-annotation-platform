# Operating AAP through the Python SDK and CLI

Use the installed SDK's operation description before constructing an unfamiliar request. Metadata describes the installed package and its API baseline; it does not grant authorization or establish current server/executor availability.

## Discover only what you need

```sh
aap capabilities --status covered --limit 20 --offset 0 --json
aap capabilities --tag training-runs --json
aap describe training_runs.create --json
aap describe learning.runs.create --json
```

The SDK method name is canonical; an available CLI dotted path is an alias. Browse bounded pages and describe one operation at a time. `covered` means there is an SDK implementation, `excluded` records an intentional scope decision, and `uncovered` means the API exists but the SDK does not expose it. Check operation limitations and request constraints as well as the HTTP route.

Core-only Python consumers can use `list_capabilities()` and `describe_operation()` without CLI/TUI extras, credentials, or a running API. Python callbacks and opaque kwargs are not automatically JSON-invocable tools.

## Read identities and access before writing

- Use UUIDs for resources. A display ID, task-dataset stream ID, version ordinal, storage dataset ID, and frozen training-version ID are different identities.
- Training request `dataset_id` identifies a frozen training version. Keep its manifest/config/provenance and digests unchanged.
- Use `client.projects.access(project_id)` to inspect project access. The server still checks current roles, API-key scopes, resource relationships, qualification, quotas, and runtime availability.
- Some queue/list reads reconcile lost-execution status. Do not infer the absence of server-side effects solely from a GET method.
- Keep reads bounded. Use list limits/offsets or the operation's cursor; use `include_manifest=False` when the full frozen manifest is unnecessary.

## Preflight, preserve, then submit

1. Read the operation's request schema, defaults, and additional constraints.
2. Select explicit task/model/data identities and any native-compute or review budgets.
3. Call the resource's preflight/preview operation. Inspect readiness, exclusions, and qualification reasons.
4. Add the returned digest to the create request. Preserve the exact request and a caller-generated stable `request_id` before submission.
5. Submit once. If the result is uncertain, replay the same request only where the operation supports idempotency; do not substitute a new request ID.
6. Keep compare-and-swap values from the relevant read: policy revision, recipe head revision, or model release generation. A conflict requires a fresh read and a deliberate decision.

Run creation requires `preflight_digest`; Study creation requires `preview_digest`, with its request ID in `base.request_id`. A shared preflight model may allow a field that the creation operation requires. `configuration` and explicit legacy `parameters` must not be submitted together.

Complex CLI requests can use a UTF-8 JSON object file or stdin:

```sh
aap --error-format json learning runs preflight --project "$PROJECT_ID" --body run-draft.json --json
aap --error-format json learning runs create --project "$PROJECT_ID" --body - --json < run-request.json
```

`--error-format` is a root option and precedes the command. `--json` selects successful machine output on the leaf command. Protected commands require explicit `--yes` in noninteractive JSON mode. That flag confirms the requested action; it never overrides server authorization or a stale preflight/revision.

## Observe the owning resource

`202` means accepted, not completed. Async Job states are not interchangeable with training run/attempt, preview, evaluation, Study, or active-learning states.

- Read the resource ID returned by submission and its current attempt.
- Poll with an explicit time limit. On timeout keep the ID and current status for later observation; do not implicitly cancel, resume, or recreate it.
- `cancelling` or `lost` does not prove execution has stopped or the GPU lease is free. Read recovery eligibility and reasons before a deliberate resume.
- Keep logs/telemetry scoped to an exact attempt and bounded cursor page. Reset attempt-local cursors when the current attempt changes. Logs accept one cursor direction at a time.
- Model registration, deployment, sample evaluation, human candidate decisions, and project publication are separate operations. None should be inferred from success of the preceding operation.

## Export the requested scope and verify output

Video export range and lidar options belong in typed `ExportRequestBody`, not query kwargs. Async project/batch export returns a job ID; wait/read that Job explicitly and then download its artifact.

VOC is synchronous binary output. Use `client.exports.download_voc(...)`, `client.batches.download_voc(...)`, `aap export project ... --target voc --out ...`, or `aap batches download-voc ... --out ...`. It has no async job ID.

Downloads retain platform authorization for relative API routes and isolate it from absolute presigned storage URLs. Available digest/size metadata is checked before atomic destination replacement; errors preserve the previous destination. Do not work around a hash mismatch by disabling verification or substituting live media for frozen evidence.

## Notifications, feedback, and inference

Use notifications as locators for pending work, then read the owning resource with current authorization. Marking a notification read does not complete its target task. Preference updates change only explicit `in_app`/`toast` channels; omitted channels remain unchanged.

Read feedback threads with their root-bound opaque cursors. The root is separate from the page's active descendants. Replies have no idempotency key; after an uncertain result, reread the thread before deciding whether to send another reply. Preserve omitted patch fields and explicit nulls: current feedback handlers treat null as no change, while `mentions=[]` clears mentions. Server actions and current role/project/task/anchor checks remain authoritative.

Pipeline apply clones a saved template into a project, optionally replacing the default. Preannotate dispatches inference; these are separate operations. Saving is not a dry run. Preserve bulk/workbench task scopes, current locks, and exact backend identities. The root is the stage without `parent_stage`, regardless of its numeric stage ID.

For preannotation, persist the complete request and any caller-supplied `Idempotency-Key` before submission. Never generate a replacement key or retry POST automatically after an ambiguous result. A successful job acknowledgement is not completion; read the existing job with `jobs.get` or a bounded `jobs.wait`. CLI users can call `aap jobs get <job-id> --json`.

## Data Explorer and saved views

Fetch `data_manager.schema` for the current project and entity grain before building unfamiliar filters. Preserve dynamic attribute keys and boolean groups; the SDK does not execute filters or compile SQL. Task queries use limit/offset; object/track queries use opaque cursors and currently only the first sort item. Entity columns are validated but do not prune row fields. Reset cursors after query changes. Track references are opaque locators, distinct from track IDs; track detail has no member pagination.

Saved-view lists include built-ins with null IDs and keys; only saved UUIDs work with view routes. Invalid fields and null counts preserve an unusable configuration rather than proving it matches no data. Shared copies inherit visibility unless explicitly changed to private, and current project permissions still apply. View patch nulls mean unchanged; empty objects/arrays clear configuration.

Assignment preview is a read. Preserve its canonical 1–200 task selection, explicit owner fields and preview_version for apply. Omitted owners remain unchanged; null clears the task override and inherits the batch owner. Inspect skipped/failed items and effective owners; a 409 requires a new preview and deliberate decision, not automatic resubmission.

Selected-task export retains the exact task set, cannot include video scope, and supports fewer targets than whole-project export. Preserve any caller Idempotency-Key and original request; observe/download the returned Job using existing helpers. Do not widen selection after an error. CLI JSON input rejects non-finite numbers before HTTP.

## Video, templates and operations

The public REST roadmap includes administrative and operational interfaces. Only the internal training-executor protocol is excluded; uncovered public routes remain explicit work rather than hidden exclusions.

Use task IDs with task_videos/video_tracker/video_review and dataset-item IDs with videos. Media GETs can create segments, cache rows or queued generation. Keep ranges bounded. Frame/chunk HTTP 202 bodies are JSON descriptors; inspect status/retry_after and fetch bytes only from a ready descriptor. Signed URLs expire and are not durable resource identities. Downloads preserve credential isolation and existing destination files on failure.

Claim the required segment, explicitly heartbeat according to the returned lease, then release or submit deliberately. The last submission can advance task review; reopen/unassign can invalidate review evidence or ownership. Tracking creation has no idempotency key: recover uncertain submissions with task active/reviewable lists before another attempt. Tracker compute completion and candidate acceptance are different states. Preserve preview job_revision, expected_source_versions and selector/digest for decisions; never silently refresh a conflict. Correction jobs require local decisions, source annotation versions and the uploaded mask digest. Mask keyframe writes use If-Match. Track quality acceptance uses its own input_digest and pair decisions. Composition/split may deactivate source annotations.

Template/source project defaults are resolved on the server. Keep omitted fields omitted, pass explicit overrides deliberately, and do not copy a second inheritance algorithm into an agent. template_id and source_project_id are mutually exclusive; copy_annotation_guide is source-only. Template duplication creates an owned private copy and creation/duplication has no idempotency key.

Storage bucket HTTP success can include per-bucket errors. Video asset retry requires administrator authority and rechecked ownership; queued acknowledges scheduling only. Inspect current asset state before another retry. General health and summaries are not proof of successful inference, storage repair or review.

## Files and dataset scenes

Task files use three explicit stages: files.upload_init, files.upload_content, then files.upload_complete. Preserve the original task_id. The content stage streams to the temporary absolute PUT URL without platform credentials, uses the initialization Content-Type, accepts only 2xx and never implicitly completes or retries. An uncertain result requires reconciliation, not another initialization. files.original_identity returns the verified content descriptor and files.download_original streams the full SHA-256 checked original over platform credentials; the legacy signed file_url route is retired (409 original_requires_authenticated_download). Thumbnail backfill returns a Celery task ID rather than an AsyncJob ID. Initialization, completion and backfill require legitimate project manager authority.

Scenes belong to the global dataset catalogue. list/get/trajectory require datasets:read for API keys, and list is an unpaginated array. Trajectory contains ordered ego poses, not annotations. Metadata updates require platform manager role; send only explicit fields, null is currently no-op and an empty source_metadata map clears it. No CAS revision is available. Do not infer project privacy or resend an entire GET response as a patch.

datasets.upload_zip and datasets.scan_items are synchronous receipt operations. ZIP upload requires an active account with datasets:write; scan additionally requires a platform manager. Pass a caller-generated UUID idempotency_key and preserve it after an uncertain response. The SDK and CLI do not generate keys or retry either POST automatically.

## Account, administration and operations

Use auth/account/avatars for explicit identity, preference and avatar flows; Client.me remains available. Auth JWT results never replace Client credentials or saved configuration. Refresh/logout require JWT, not API keys. Keep returned credentials deliberately; do not log successful secret-bearing JSON. Repr hides passwords/tokens, while intentional serialization retains the actual API fields. CLI one-time secret results require --json or --show-secret before mutation. Invitation creation/resend does not email; resend rotates the token, send-email sends the current one.

Preference patches preserve opaque values and server merge rules. Send only intended keys. Named preset writes replace the full preset map and require current namedPresetsRevision; reconcile stale data rather than substituting a new token into old contents. Avatar uploads are multipart, limited to 2 MiB, and may delete old uploads. Heartbeat/task events are never automatic. Events use stable caller-provided client_id; discarded rows cause CLI exit 1 after the full receipt, queued_async is only scheduling evidence.

Prefer paginated users/groups/invitations query methods. Legacy project-admin active-employee candidate lists ignore project_id. Read is_managed and is_lifecycle_managed separately. Offboarding commits preserve preview_version and explicit transfers; role preview is informative without a CAS token. Suspension revokes keys, reactivation does not restore them. Bulk apply prints all results then exits 1 for failed rows; previews remain exit 0. Required group_id explicitly selects assignment or null clearing. API-key patch uses explicit expires_in_days=null to clear expiry; legacy update(None) omits it.

Public health requests use origin-root /health\*, not /api/v1. The health CLI needs only a configured URL, without an API key. Accepted degraded 503 remains a report without retries; CLI prints it and exits 1. Public details are sanitized; authenticated superadmin system health provides internal diagnostics. Capability instances may probe uncached backends; protocol vocabulary is static. No conditional header is sent for JSON reads.

System settings are superadmin-only. Keep expected_version, preserve omitted/null patches, use smtp_password="" to clear and reset to restore deployment defaults. SMTP tests really send mail to the current administrator and need confirmation. Audit lists enforce managed-project scope on the server before counts/cursors. Summary/archive/export are superadmin-only; months use UTC. Pair detail_key/detail_value, including explicit empty-string values. Export logs itself and streams once, caps at 50,000 rows; JSON uses items, CSV has metadata headers. Archive reads currently load the full month before pagination.

Storage credentials are write-only, never in endpoint URL userinfo. Unknown config keys fail; SFTP password/key must match auth_type. An explicit empty host allowlist denies all targets. Storage test and ML probe/observe perform real outbound requests; HTTP 200 with ok=false is failure evidence. Privileged ML probes support private/internal HTTP(S) base URLs; tokens go to the named backend only when authentication is enabled. Smoke tests can reload/unload, and auto_unloaded is not proof of VRAM release. Warmup requires project enablement and may evict shared weights. Global reload/unload remain superadmin operations; project_id is audit context. Predict-frame persists Prediction; predict-test is transient. Multipart context.mask_prompt_source additionally needs annotations:read.

Preannotation queue/summary and alias frequency enforce current project ownership. Bulk clear predictions_only versus reset_to_draft has different impact; inspect succeeded/skipped/failed, avoid repeated IDs and do not assume idempotency. Fixed analytics panels may be unavailable before DuckDB initialization. Confirmation never bypasses server authorization, CAS, quiescence or resource relationships.

## Project and data workflows

Use projects.query for bounded project pages and search.query for per-entity limits. Search task authority includes current assignments, batch states and collaborative-video visibility before the limit. Readiness is setup evidence, not live executor readiness. Maintenance requires current project management; transfer is superadmin-only to an active project_admin. Class rename can update matching annotations across the project even when configuration changes target one tool unit.

Keep batch preview/apply selection, recipients, only_unassigned and preview_version unchanged. Explicit assignment null clears a role. After 409, inspect a fresh preview and make a new decision. Splits only consume unclassified/default-batch tasks; id_range.item_ids are dataset-item IDs, and scene projects require by_scene. Random split rejects more batches than current tasks. Bulk apply receipts require succeeded/skipped/failed reconciliation; failure exits do not imply successful rows rolled back. submit-review commits chunks and may dispatch Mask QC. Batch LiDAR preflight and export require export.annotations; preflight has no binding digest and export rechecks current evidence. Deleted batch audit needs reliable project context.

Dataset operations require platform management under the global catalogue policy. Connector import freezes budgets and returns an AsyncJob job_id. Scan is synchronous but can queue media. Media backfill returns queued without a job ID. Scene inference defaults to writing; axis sniff only analyzes. Dimensions backfill has no cursor/order: failed width-null rows remain eligible, remaining_hint only describes a full selected batch, and processed=0 with failures is not completion. Never loop automatically; media backfill helps only items missing thumbnails. CLI prints complete receipts then exits 1 for failed dimension rows or remaining batch submit-review tasks.

Guide assets use init, bare signed PUT, then explicit complete. Allowed image MIME types and 5 MiB bound are in the model. Keep the original key and Content-Type; completion can duplicate registration if blindly retried. Signed URLs are credentials, hidden from repr/default text but preserved by intentional JSON. Delete may remove metadata despite an object-storage deletion error.

Project performance is owner/superadmin data; member_id is the user UUID, including verified historical roster eligibility. Respect date timezone, 90-local-day bounds and cursor filters. Keep null/partial/unknown metrics. Performance and dashboard CSV exports build full matching results on the server and stream atomically on the client; page limits do not bound those exports. Scoped dashboard counts and timeline remain within the selected project; global cost zero fallback is not proof no inference ran.

Failed prediction listing requires predictions:read for API keys; mutations still rely on management and target-project authority. Retry returns failed_id and queued, not an AsyncJob identity, with a three-attempt ceiling. Dismiss/restore are soft and idempotent. Prediction purge defaults to external_import; preview counts before confirming all-source deletion. Annotation import is AAP JSON multipart up to 64 MiB, requires annotations:write for API keys and project management, returns a synchronous partial receipt and exits 1 on CLI errors after full JSON. Overwrite replaces imported annotations only. No automatic write retry or invented idempotency key.

## Handle errors without guessing

With `--error-format json`, read one structured error from stderr and a nonzero exit status. Successful stdout retains the command's normal `--json` contract. Keep status/code/detail and sanitized field locations; absence of a server code is not evidence for an invented one.

Job failure and wait-timeout errors retain `job_id`; failures also retain `job_status`. Use that identity to observe the existing job instead of submitting another export. Raw job payloads and failure messages are omitted.

Authentication/permission errors, missing resources, stale preflight/CAS conflicts, request validation, throttling, and unavailable execution need different responses. Preserve the original request identity on ambiguous network failures. Retry-after context is advisory; it does not make a write automatically safe to repeat.

Never print API keys or full request input when reporting a failure. Core-only installation intentionally gives a CLI-extras installation hint if `aap` is invoked without its optional dependencies.

## Discussion and quality workflows

Read comments with parent-bound cursor pages; the mixed discussion feed retains item kind and server actions. History is latest-first and unpaginated, including soft-deleted records authorized through the actual task. Uploading an attachment only returns metadata; explicitly register it in the comment/report. Signed downloads validate 302, isolate platform credentials, require complete HTTP 200 and preserve an existing destination on failure.

Bug reports use a deliberate global project_admin/super_admin queue. Reporters may edit only title/description/severity; none accepts explicit null. Only nullable administrative fields can be cleared. Reporter comments can reopen closed reports. Screenshot helpers reject files over 10 MiB before requests; server size metadata is not actual object-length verification.

Mask-format export preflight is advisory; actual export does not consume its digest. Import and Mask QC repair require the exact reviewed actor-owned receipt and plan digest before expiry. Dry-run repair persists a plan without modifying annotations. Preserve skipped/partial results and the domain batch status separately from AsyncJob. Resume/rollback are explicit server-gated mutations; never refresh proofs or retry conflicting writes automatically. Tracker-candidate comparison content belongs to video_tracker.get_mask_content, not a generic content-path executor.

Point-cloud issue reads can persist stale status. Evaluation freezes reviewed samples; eligibility does not promote configuration. Promotion checks baseline revision/digest. A dispatch-failed 503 can contain persisted run_id/async_job_id; query those identities before resubmission. Structured CLI errors expose safe reason/run_id/job_id, while Python APIStatusError.detail retains the original body. Candidate config is strict; Project config readback retains additive fields.

## Task context, leases and geometry

All 558 adopted stable public REST operations, including operations/admin workflows, have concrete SDK consumers; ten private executor operations remain excluded. Task context keeps opaque annotation cursors, exact collaboration segment IDs and original prediction shape_index. Neighbor IDs are navigation metadata, not authorization. Source and target task editability are both checked for cross-frame writes; completed tasks require explicit reopen. Image-pyramid manifest GET can invalidate/enqueue generation and uses normal GET retry policy; concurrent stale reads can race into duplicate Celery dispatch. Retry's Celery ID is not necessarily an AsyncJob ID.

Task locks are explicit current-actor 300-second leases, with no background renewal or unique_id CAS. Camera/member writes preserve member version, track revision and calibration revision/digest; reads bind the selected track to the actual task scene. Calibration matrices have fixed finite dimensions and shared data items require superadmin. Preferred scene-track commands preserve preview snapshot_token, caller idempotency_key and original operation_id for revert; legacy execution has no safe caller replay. Conversions and Mask commits preserve actor/task proofs, whole-scope versions, fingerprints and required If-Match. SDK does not recompute geometry or invent idempotency keys.

## Public streams

The six root /ws channels are inventoried separately from REST counts. Use list_stream_channels(), describe_stream_channel() or aap streams list/describe offline without optional dependencies. Connections need the streams extra (TUI already includes it). Client stream methods return async context managers and async iterators, not JSON results; operation descriptions include the event schema and authority/recovery contract. CLI's six named listeners emit bounded NDJSON with max-events/timeout and no implicit reconnect, refresh or submission.

Notifications, global prediction jobs and tracker jobs require JWT. Project preannotation/batches and administrator ML stats accept JWT or API key. Query tokens are encoded and hidden from socket errors/logs. JSON ping is ignored; preannotation has no JSON heartbeat. Context exit/cancellation closes the connection. Permission changes may stop delivery without a specific close code. There is no acknowledgement/replay cursor or guaranteed delivery; refresh owning REST state after disconnect. Tracker events keep absent source annotation IDs as null, including normalization of the legacy string None. Frame results are transient candidate hints, and ML stats subscriptions activate the server's existing polling. CLI retains emitted events on error; timeout/count exit0 only ends the observation window, not the domain work.

# Task workflows and public stream SDK completion

> Status: implemented and locally verified; adopted public REST and stream completion achieved.
> Baseline: 568 OpenAPI operations; 514 covered, 44 uncovered, ten executor-only exclusions.
> Previous receipt: [Annotation discussion and quality](1792179022_sdk-annotation-quality.md).

## Outcome and boundaries

Implement the remaining 44 public REST operations and six documented public WebSocket channels. Administrator and operations workflows remain in scope. REST completion is 558 covered plus ten executor-only exclusions; it does not include or prove the separate stream contracts. No application/SDK release, originating commit, push or deployment is implied.

Reuse existing Task, Annotation, Job, COCO RLE and immutable mask-reference models. Extend the owning resource where its scope fits; point-cloud quality is not a substitute for camera/calibration/scene-track APIs. Literal REST paths must retain the snapshot placeholders. Do not add a generic arbitrary-path executor or a second mask/geometry engine. Read actual router/service invariants and frontend callers before selecting request defaults.

## REST implementation partitions

Four isolated writers can handle independent resources: task context/media/jobs/leases (14), point-cloud cameras/calibration (eight), scene tracks/commands (ten), and annotation transformations/prediction/mask mutations (12). The parent owns shared transport, Client/public/root CLI integration, existing shared model owners, coverage/catalog, API fixes/artifacts, documentation and integration acceptance. Writers branch from the current originating local HEAD, receive only necessary pending parent prerequisites, restore temporary copies before committing, and return real commits. Preserve all existing pending changes.

- Task context: annotation page limit 1-1000 and opaque cursor; video collaboration requires exact video_segment_id. Neighbor annotation visibility is checked per task. Preserve original prediction shape_index when later accepting/rejecting; filtering must not renumber it.
- Pyramids: manifests/assets expose expiring signed URLs and generation identity. Asset URLs require current generation and at most 128 requested assets. Retry is rate/cooldown-gated HTTP 202 with a Celery identifier, not necessarily an AsyncJob; do not send it to jobs.wait. Separate signed reads from generation mutation and isolate credentials on binary fetches.
- Cross-frame jobs: visible editable source/targets, at most 100 frames and 500 unique box_3d sources; verify exact source/task/scene relationships. Only the current user's jobs are listable/retryable. Active identical creation has server singleflight but no caller idempotency key. Reuse Jobs for actual AsyncJob responses; retry only eligible terminal failed frames.
- Leases: acquisition, heartbeat and release are explicit current-actor operations with a 300-second TTL. Acquisition can take over an old lock; heartbeat can return 404. unique_id is not a CAS token. Do not add a background renewal thread or promise an automatic scoped lease unless the server contract supports it. CLI states the effect and follows existing confirmation policy.
- Cameras/calibration: request versions include annotation version, track revision and calibration revision/digest as applicable. Verify parent task/member/source/track relationships. Delete soft-deactivates; restore rechecks current versions. Calibration writes require project management; shared dataset items across projects require superadmin. Preserve fixed matrix shapes (extrinsic 16, intrinsic nine, optional rect 16) and current digest/revision, including signed manifest URLs.
- Scene tracks: timeline window at most 200 frames, candidate results at most 20. Some reads require the complete scene's task visibility, and candidates GET requires write authority. Multi-task writes need all affected tasks visible/editable. Preferred command execution binds exact preview snapshot_token and caller idempotency key; revert uses the original operation. Legacy track-operations execution generates the key server-side and cannot be safely retried by the caller after an uncertain result.
- Geometry/prediction/masks: preserve actual source/target task ownership, current locks, segment leases, expected versions and fingerprints. Conversion execute consumes an actor/task-bound ten-minute plan token and explicit replace/lossy confirmation. Multi-mask commits require whole-scope versions/fingerprint. AI candidate acceptance needs the original receipt; refine also needs If-Match. Prediction decisions have no idempotency key. Secondary inference has dynamic backend params/model_variants and may write attributes or create child annotations. Raw masks are not directly uploaded by these 44 routes; reuse existing immutable COCO RLE content/reference workflows.

### REST keys completed by this cohort

#### Task annotation context (5)

- `GET /api/v1/tasks/{task_id}/annotations/page`
- `GET /api/v1/tasks/{task_id}/mask-capabilities`
- `GET /api/v1/tasks/{task_id}/neighbor-annotations`
- `GET /api/v1/tasks/{task_id}/neighbors`
- `GET /api/v1/tasks/{task_id}/predictions`

#### Image pyramids (3)

- `GET /api/v1/tasks/{task_id}/image-pyramid`
- `POST /api/v1/tasks/{task_id}/image-pyramid/asset-urls`
- `POST /api/v1/tasks/{task_id}/image-pyramid/retry`

#### Cross-frame jobs (3)

- `GET /api/v1/tasks/{task_id}/cross-frame-jobs`
- `POST /api/v1/tasks/{task_id}/cross-frame-jobs`
- `POST /api/v1/tasks/{task_id}/cross-frame-jobs/{job_id}/retry-failed`

#### Task leases (3)

- `DELETE /api/v1/tasks/{task_id}/lock`
- `POST /api/v1/tasks/{task_id}/lock`
- `POST /api/v1/tasks/{task_id}/lock/heartbeat`

#### Point-cloud cameras and calibration (8)

- `DELETE /api/v1/tasks/{task_id}/point-cloud/camera-members/{member_id}`
- `GET /api/v1/tasks/{task_id}/point-cloud/camera-members`
- `GET /api/v1/tasks/{task_id}/point-cloud/cameras/{camera_role}/calibration`
- `GET /api/v1/tasks/{task_id}/point-cloud/manifest`
- `PATCH /api/v1/tasks/{task_id}/point-cloud/camera-members/{member_id}`
- `PATCH /api/v1/tasks/{task_id}/point-cloud/cameras/{camera_role}/calibration`
- `POST /api/v1/tasks/{task_id}/point-cloud/camera-members`
- `POST /api/v1/tasks/{task_id}/point-cloud/camera-members/{member_id}/restore`

#### Scene tracks and commands (10)

- `GET /api/v1/tasks/{task_id}/scene-timeline`
- `GET /api/v1/tasks/{task_id}/scene-track-commands`
- `GET /api/v1/tasks/{task_id}/scene-track-diagnostics`
- `GET /api/v1/tasks/{task_id}/scene-tracks/{track_id}`
- `GET /api/v1/tasks/{task_id}/track-operations/candidates`
- `POST /api/v1/tasks/{task_id}/scene-track-commands/execute`
- `POST /api/v1/tasks/{task_id}/scene-track-commands/preview`
- `POST /api/v1/tasks/{task_id}/scene-track-commands/{operation_id}/revert`
- `POST /api/v1/tasks/{task_id}/track-operations`
- `POST /api/v1/tasks/{task_id}/track-operations/preview`

#### Annotation transformations, predictions and masks (12)

- `POST /api/v1/tasks/{task_id}/ai-mask-candidates/accept`
- `POST /api/v1/tasks/{task_id}/annotation-conversions:dry-run`
- `POST /api/v1/tasks/{task_id}/annotation-conversions:execute`
- `POST /api/v1/tasks/{task_id}/annotations/interpolate-range`
- `POST /api/v1/tasks/{task_id}/annotations/mask-mutations:commit`
- `POST /api/v1/tasks/{task_id}/annotations/polygon-slices:commit`
- `POST /api/v1/tasks/{task_id}/annotations/propagate-batch`
- `POST /api/v1/tasks/{task_id}/annotations/slices/{operation_id}:restore`
- `POST /api/v1/tasks/{task_id}/annotations/{annotation_id}/propagate-to-task`
- `POST /api/v1/tasks/{task_id}/annotations/{annotation_id}/secondary-inference`
- `POST /api/v1/tasks/{task_id}/predictions/{prediction_id}/accept`
- `POST /api/v1/tasks/{task_id}/predictions/{prediction_id}/reject`

## Public WebSocket consumer and discovery

Keep channels separate from HTTP operation counts. The application mounts them at root /ws, outside /api/v1. Implement six named Client stream methods, with an explicitly optional WebSocket extra using the existing websockets>=12 floor. Core imports and offline discovery must work without that dependency. Prefer a small async context manager/iterator per channel with typed additive events and guaranteed connection cleanup; share only actual transport/heartbeat parsing. No arbitrary channel/path executor, acknowledgement, subscription message, reauthentication or replay protocol exists.

| Channel               | Root path                             | Authentication and authority              | Reconcile with                              |
| --------------------- | ------------------------------------- | ----------------------------------------- | ------------------------------------------- |
| Notifications         | /ws/notifications                     | Active JWT user                           | Notification REST state and owning resource |
| Project preannotation | /ws/projects/{project_id}/preannotate | JWT or API key; current project access    | Current project/job/task state              |
| Project batch events  | /ws/batches/project/{project_id}      | JWT or API key; current project access    | Project batches                             |
| Prediction jobs       | /ws/prediction-jobs                   | JWT; super_admin/project_admin            | Persisted prediction/job state              |
| Video tracker job     | /ws/video-tracker-jobs/{job_id}       | JWT; actual job task visibility           | Tracker job and candidate preview           |
| ML backend stats      | /ws/ml-backend-stats                  | JWT or API key; super_admin/project_admin | Current backend registry/health/stats       |

Tokens are query parameters and must be percent-encoded and hidden from repr, exceptions, CLI logs and diagnostic URLs. Pre-accept authorization failures close with 1008; midstream permission changes can stop relaying without that close code. Notification access filtering can drop messages while the connection remains open. Do not infer continued authority or guaranteed delivery from an open socket.

Five channels send JSON type=ping every 30 seconds; preannotation disables that heartbeat. Distinguish heartbeat from events. Most payloads are unchanged Pub/Sub hints; only notifications have a durable event envelope. No channel has a replay cursor or delivery acknowledgement. A disconnect requires an explicit REST refresh before relying on observed state. Tracker frame-result IDs may be local to a processing window. ML stats reuse MLBackendStatsSnapshot; no extra GPU polling or inference is initiated by the SDK beyond the channel's existing subscriber behavior.

Represent the six channel names, auth, event schemas and REST recovery in installed offline discovery without faking HTTP call sites. Python Agent consumers must be able to discover typed stream parameters without credentials or importing optional WebSocket dependencies. CLI lists/describes channels offline and may expose six named bounded NDJSON listeners (explicit duration/event limit), preserving prior emitted events on disconnect. Avoid an unbounded generic follow command; the TUI's existing reconnect policy can remain in its owning UI layer. Verify dependency-floor behavior before sharing transport with TUI.

Resolve known documentation drift while implementing: progress.py publishes batch type/batch_id/from/to/at, not the older example's project_id/actor_id; ML stats accepts API keys as well as JWT; video tracker and TUI docstrings must use root /ws rather than /api/v1/ws.

## Explicit internal/test inventory

The ten /api/v1/internal/training operations use the executor token and remain excluded. Hidden /api/v1/internal/metrics-targets and /metrics are internal service discovery/scrape endpoints with proxy/network boundaries. Hidden \_test_seed endpoints are enabled only for non-production E2E seeding. Outbound ML backend health polling is an internal client action. These are not additional adopted public SDK operations. Evidence owners: api/v1/training_runs.py, api/v1/internal.py, main.py, api/v1/router.py, workers/ml_health.py and the internal-endpoints reference.

## Acceptance and finish

For REST, verify every exact call site, typed input/response, task/parent/assignment access before limits, bounded paging, current leases/versions/digests, signed URL isolation, proof/idempotency preservation, confirmation before effects and partial receipts. Add behavioral regressions for concrete API gaps found during tracing. DB checks target only a freshly doctor-verified disposable worktree test mode; mock storage/queue/ML/mail effects and do not seed the everyday database.

For streams, use local WebSocket handshake/event fixtures to verify exact root paths, encoded JWT/API-key auth, no credential leakage, heartbeat handling, additive variants, disconnect/timeout cleanup and REST reconciliation guidance. Validate missing optional dependency behavior, installed core/CLI/TUI and WebSocket lower floor/current environments. No live production subscription or GPU/backend side-effect test is implied.

Regenerate/check OpenAPI, frontend API artifacts, HTTP catalog and separate channel metadata. Run focused and full SDK checks, affected API tests, pinned Ruff/format, docs/snippet/navigation checks, wheel/sdist builds and installed loopback tests. Review the complete diff and check tracked/staged/untracked whitespace. Clean only owned environments/resources/worktrees and retain worker commits.

Mark the overall goal complete only after all 558 public REST operations and six public channels have concrete consumer implementations, their contracts and agent discovery are verified, and the outside-OpenAPI inventory is reconciled. Report local tests separately from live deployment/browser/GPU and remote CI; counts or plans alone are not completion.

## Completion receipt

Integrated worker commits b69b357b33ebec862e465e8007710e870c98ee05 (14 task context/media/job/lease routes), c7e9dd61f (eight point-cloud camera/calibration routes), d5ff26481df9be0ec590ca0be3aa95cc573c8e92 (ten scene-track routes), 2f15f8cd9f799726cbce58dbccd2178d85c44390 (12 annotation operations), 62cb4924b (camera scene binding and API regressions) and a89056a8fc686eb06edfe1c966848dea2fb1fd7d (cross-frame task editability regressions). The parent added the six public stream consumers, offline protocol discovery, bounded CLI listeners, aggregate/public integration, shared schema reuse, API fixes/artifacts, docs and package acceptance. Worker prerequisite copies were restored/removed before their scoped commits.

The final inventory is 568 OpenAPI operations = **558 covered + ten executor-only exclusions**, zero uncovered/planned. All 44 final REST methods have operation constraints. Six public root /ws channels are inventoried separately with actual source-call-path drift checks and event schemas. There are 579 SDK method descriptions and 848 public exports; all expand in installed core-only, CLI, streams-only, TUI and lower-floor environments. Core discovery and import need no WebSocket/CLI/TUI dependencies. Streams are explicitly asynchronous Python contexts, not JSON-invocable return values; CLI provides bounded NDJSON. Public prediction and COCO RLE types reuse existing owners.

Source tracing corrected actual API contract gaps: prediction acceptance verifies task/prediction pairing before mutation or heartbeat; task prediction reads require annotations:read for API keys; direct propagation/interpolation checks source task editability and retains reviewer review-adjustment rules; selected track existence is omitted for unavailable timeline frames; camera-member queries bind SceneTrack to the actual task scene. Tracker events now serialize absent source annotation_id as JSON null, with SDK compatibility for the legacy string None. No uniform per-annotation UI-lock policy was invented from the task-lease requirements.

Acceptance:

- Final full SDK suite: **1079 passed**. Includes exact REST call sites/classifications, separate public channel/router matching, typed/additive schemas, proof/CAS preservation, confirmation refusal, partial receipts, signed isolation, optional imports, async stream cleanup/cancellation/heartbeat and bounded NDJSON errors. No implementation change followed this final suite.
- Final cohort API checks: 18 new scope/editability/timeline cases plus 66 existing propagation/cross-frame/camera/scene-track/WebSocket revalidation cases, all passed on doctor-verified disposable worktree test mode. Four pure tracker event-identity cases also passed without DB/queue/GPU/storage effects. The previous cohort's 105 distinct API cases passed separately, giving 193 relevant passing cases across the two cohorts; this is not the entire API suite.
- Five isolated Python 3.11.14 install environments: core, CLI, streams, TUI, and lower floor. Lower floor pins Pydantic 2.5.3, HTTPX 0.27.2, Typer 0.12.5, Click 8.1.8 and websockets 12.0; current stream installs use websockets 17.2. All 848 exports and 579 descriptions expanded with warnings treated as errors. Installed HTTP loopback counts are 8/10/8/10/10; WebSocket connections are 0/0/7/9/9. Every public channel completed an actual local handshake/event delivery, and close 1008/partial NDJSON, encoded token/no Authorization forwarding, missing optional dependencies, exact preview/execute proofs and refusal-before-HTTP were checked. These are local fixture servers, not deployed services.
- Final wheel/sdist built. All 183 runtime source/guide/catalog files byte-match both archives and all five installations; all 104 SDK test/support Python files match sdist source. MANIFEST includes test support files. SDK uv.lock adds only streams extra metadata, retaining existing pins; offline lock check passes.
- Final wheel SHA-256: 52e9d1e1e76d0141b39ac2c127ffc10dd01ccd7ae2fa596eb318a9c81363b478. Final sdist SHA-256: 8740414272a3998c231fae781127a2e1298b3ab5d9752a3d201ec489f6844c94.
- OpenAPI export/check, frontend API codegen, catalog generation/check, pinned Ruff/format, docs build, 183 frontmatter documents, 186-page navigation and 33 SDK Python snippets passed. Final tracked whitespace checks and independent checks for all 219 untracked files passed after the receipt update.

The final outside-OpenAPI scan found only the six adopted streams and explicitly internal metrics/test-seed endpoints; the ten executor routes remain classified private. Framework documentation delivery is not a domain operation. Signed downloads already use concrete consumer helpers; no additional public HTTP protocol was silently omitted. Image-pyramid manifest GET cache/queue effects and the bounded concurrent-dispatch race are documented rather than presented as a side-effect-free read.

The disposable aap_wt_a13d73521739f081_test database, dedicated Redis and nine owned buckets were doctor-verified before checks and destroyed with exact confirmation afterward. Four clean integrated Orca worktrees were removed; their unmerged worker branches/commits remain. Temporary installation/JUnit/build-log resources are cleaned after recording this receipt. Originating HEAD remains 7ed68a5a667c43058917bdec5a9ee809cde052b1; cached origin/main comparison is 0 behind / 2 ahead. The originating index is restored empty and all authorized changes remain in the working tree. No new originating commit, push, release, deployment, live external GPU/backend qualification or remote CI success is claimed. No running worker in this SDK worktree required refresh.

The adopted all-stable-public-interface implementation and local acceptance are complete. Further release/deployment/remote CI work remains separate from this goal.

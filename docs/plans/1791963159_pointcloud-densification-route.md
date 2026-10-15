# Point-cloud densification route and qualification gates

> Status: experimental integration implemented; broader geometry, hardware rendering, managed lifecycle and annotation-benefit qualification remain open.
> Release milestone: Not yet determined.

## Current decision

The objective is useful point density for annotation. Preserve the complete current
LiDAR scan and its attributes as the source for picking, point masks, measurements,
boxes and persistence. Model geometry must remain an identifiable, optional display
layer that can be disabled independently.

Continue evaluating current-frame VGGT-Omega with LiDAR calibration and measurement
protection. Compare independent single-image inference with current six-camera
inference; the present evidence does not establish a universal winner. Do not use
cross-time model accumulation as the default annotation background. Full-scene and
overlapping-window outputs remain reproducible comparisons.

The existing traditional neighbor overlay and surface display style are separate
implemented capabilities. Their remaining renderer promotion gates stay open.
This consolidated plan records the implemented experimental integration and its
remaining qualification gates. The deployment gate remains off by default;
the archived experiments retain their original protocols and outcomes.

## Evidence and archived sessions

| Session                                                                                 | Preserved result                                                                                                              | Record                                                                |
| --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| [Surface enhancement](archive/1789979548_pointcloud-surface-enhancement.md)             | Implemented display style and bounded real neighbors; warm navigation and tri-view promotion gates remain unmet               | [User guide](../../docs-site/user-guide/workbench/pointcloud-view.md) |
| [Route selection and cleanup](archive/1790239103_pretrained-model-rendering-context.md) | VGGT retained; other model trial executables removed. Its three-frame default is historical                                   | [Pilot report](../research/68-vggt-omega-lidar-densification.md)      |
| [Three/five-frame pilot](archive/1791530790_vggt-omega-lidar-densification.md)          | Twelve 416-checkpoint comparisons and fixed-geometry RGB meshes; sparse coverage gains coexist with worse common-support MAE  | [Report 68](../research/68-vggt-omega-lidar-densification.md)         |
| [234-image joint reconstruction](archive/1791537431_vggt-omega-full-sequence.md)        | Entire keyframe sequence reconstructed with the 512 checkpoint; whole-scan holdouts and failed larger-input attempts retained | [Report 69](../research/69-vggt-omega-full-sequence.md)               |
| [1,318-image overlapping windows](archive/1791540231_vggt-omega-all-exposures.md)       | Static correct support declines after fusion; actor compensation uses oracle identities and has narrow evidence               | [Report 70](../research/70-vggt-omega-all-exposures.md)               |
| [Current-frame comparison](archive/1791888763_vggt-omega-current-frame.md)              | All 39 times and 234 images completed; selected-cloud moving support favors mono, while preselection depths do not            | [Report 71](../research/71-vggt-omega-current-frame.md)               |

Current-frame within-scan completion scores and older acquisition-heldout scores
have different denominators and must not be ranked together. Keep failure records,
negative results, frozen protocols and amendments, source snapshots, provenance,
audits and browser evidence. The final cleanup retired the offline executables and
previews and removed bulky derived arrays and source copies. Original datasets and
the platform importer remain available; the current mono publisher input subset
is retained. Historical manifests describe their pre-cleanup snapshots.

## Experimental integration specification

### Product contract

Keep the complete current LiDAR scan as the annotation source. Offer one
**point-cloud augmentation** selector, with exactly one supplementary source at a
time. The existing point/surface setting becomes an appearance setting independent
of that selector.

| User-facing choice              | Actual visible geometry                                             | Intended use and boundary                                                                       |
| ------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Off / current measured points   | Current LiDAR only                                                  | Default for new users; authoritative comparison                                                 |
| Measured neighboring frames     | Current LiDAR plus bounded, pose-aligned real neighbors             | Additional context for background; motion can still produce trails                              |
| AI current frame (experimental) | Current LiDAR plus one selected camera's current-frame Omega output | Optional shape context, including moving objects; generated surfaces can be wrong or incomplete |

The product labels are `关闭（仅当前实测）`, `邻帧实测补强`, and
`AI 当前帧补强（实验）`. Explain the model name in the option description and help,
without requiring annotators to choose checkpoints or inference batch shapes.
Do not recommend an automatic static-neighbor/dynamic-AI mixture: current dynamic
neighbor handling depends on existing boxes/tracks and cannot reliably partition
unannotated moving objects. There is no combined-source mode in the initial pilot.

Place the selector in the existing 3D viewport control area. The settings dialog
exposes the same preference under point-cloud display, with conditional details:
neighbor radius and existing motion treatment for neighbors; a camera-direction
choice for AI. Preserve the existing neighbor radius and motion preferences when
switching away. Remove the duplicate visible neighbor-enable checkbox from the
settings presentation, while retaining its compatibility storage field.

```text
Point-cloud augmentation: [AI current frame (experimental) v] [Measured comparison]
AI direction: [Front v]       Appearance: [Points / Surface]
Current measured: 34,688 | AI additions: 43,000 | Front | Experimental
Orthographic refinement: current measured points
```

This is a layout specification, not a new standalone review page. Reuse local UI
adapters, semantic AI/status tokens, and the compact Workbench layout. The two
source counts remain separate; do not present their sum as measured point density.

`实测对照` temporarily hides supplementary points without changing the saved source
or point/surface appearance. It is a visible, keyboard-accessible toggle, resets
on task/frame or account changes, and creates no annotation history entry. Do not
assign a new global hotkey in this slice. A source-color view is available inside
the augmentation menu to distinguish generated points from the measured base.
Normal AI display uses its source-image RGB with subdued opacity; real points,
boxes, masks and edit overlays retain visual priority.

AI is opt-in at deployment, project and user levels. Project managers enable the
experiment and see ready/missing/failed coverage in the existing project settings
surface. Enabling a project does not change any annotator's selected source.
This capability is independent of preannotation, interactive AI tools and the
browser-local WebGPU renderer experiment. Viewing prepared geometry never creates
predictions, accepts annotations or starts GPU inference.

### Compatibility with existing augmentation

The pre-integration implementation coupled appearance and geometry:
`resolveNeighborPointCloudPlan` in `useNeighborPointClouds.ts` requests one past
and one future frame for `displayStyle=surface`, even when
`neighborPointOverlay=false`. Explicit neighbor overlays instead use the stored
radius and diagnostic colors. Simply adding an AI checkbox would leave hidden
traditional geometry, additional downloads and ambiguous motion trails.

Add the nullable preference
`workbench.pointcloud.augmentationSource: null | none | neighbors | vggt_omega`.
Null means legacy compatibility, not an extra user-facing option:

| Existing preferences when the new field is null  | Effective source shown by the selector                                   |
| ------------------------------------------------ | ------------------------------------------------------------------------ |
| Explicit neighbor overlay enabled                | Neighbors, with the existing radius and diagnostic appearance            |
| Overlay disabled and surface appearance selected | Neighbors, radius one, retaining the existing subdued surface appearance |
| Overlay disabled and point appearance selected   | Off                                                                      |

Resolve this mapping without writing preferences during hydration. Users therefore
keep their previous visible geometry on first opening; the previously implicit
source is now named. Add a short inline explanation for this legacy surface case.
Once the user explicitly selects a source, surface appearance alone never enables
neighbors again. Selecting AI or off clears supplementary neighbors immediately;
the neighbor-box reference overlay remains a distinct annotation aid.

Use one resolver for requested source, effective source, neighbor plan, and
availability. New selector writes submit the source and compatibility
`neighborPointOverlay` together in one patch; the latter is true only for an
explicit neighbor selection. `buildFieldPatch` must support this compound field
in the settings dialog, and the viewport must call the same helper. Keep radius,
motion handling and appearance untouched. An explicitly supplied new source wins
over the compatibility boolean; a legacy writer that supplies the old enable
field without the new source restores null/legacy interpretation. Normalize this
before deep merge in `/me/preferences`, with partial- and whole-tree cases covered.
Do not add a general preferences migration, rewrite other subtrees or claim
cross-tab compare-and-swap semantics that the current writer does not provide.

Persist only user intent through `useWorkbenchConfig`. Availability, loading,
camera direction and comparison state belong to the current Workbench owner;
they must not be copied into personal settings or `rendering_config` overrides.
Older frontends retain their original neighbor/surface behavior and cannot show
AI; complete the API-first rollout with a frontend refresh before pilot use.
Rollback keeps these extra preferences and leaves annotation data unchanged.

Correct neighbor help text to say that culling/alignment uses available annotated
boxes and matching tracks. Do not describe either as guaranteed trail-free or as
automatic motion segmentation. Keep the current `keep` default and existing
users' choices; show missing motion support explicitly rather than claiming it
was applied. Existing neighbor-box quality locators must not enable point
augmentation or replace the selected AI source.

### Availability and navigation

| Condition                                  | Supplementary display                      | User feedback                                                                             |
| ------------------------------------------ | ------------------------------------------ | ----------------------------------------------------------------------------------------- |
| Source off                                 | None                                       | Current measured points                                                                   |
| Valid AI generation loading                | None until the exact frame/camera is ready | Loading AI; current measured points remain usable                                         |
| Ready, identity checks pass                | Selected camera's AI points                | Experimental, camera name and additional point count                                      |
| Missing, disabled, unsupported or failed   | None                                       | Selected AI unavailable, reason, and an explicit switch-to-neighbors action when eligible |
| Source image, calibration or LiDAR changed | None                                       | Result expired; manager can prepare a new generation                                      |
| Fast frame/task/camera switch              | Clear old supplementary points immediately | Only the current owner may publish a new layer                                            |

Never silently substitute neighbors for missing AI, reuse a prior frame's AI,
hold playback for AI, or automatically choose a different camera when its asset
is missing. Neighbors likewise show their actually loaded radius and partial
availability; missing ego poses leave current measured geometry usable.

The first AI profile displays **one camera direction at a time**, matching report 71. Initialize to the front role, otherwise the first supported role in stable
order; retain the explicitly selected role while navigating a scene. Expose the
direction selector only in AI mode. If that role becomes unavailable, display
the reason instead of switching direction. Enlarging a camera, selecting a box,
or changing orbit angle does not silently change the model layer. Do not call
these outputs a full-surround fused cloud. Six-camera merging, selection-driven
camera changes and temporal model fusion require separate qualification.

### Asset and API contract for the first pilot

The first deliverable serves prepared, verified artifacts through the platform's
normal task authorization and object storage. It includes an administrator
publisher CLI; annotators do not install tools, supply HF tokens or run scripts.
No inference backend, new GPU worker or browser inference is needed to use this
first slice. Freeze the initial profile to current-frame mono from the tested
six-slot batch and checkpoint; retain joint inference as a research comparison.
This is a pilot choice, not evidence that mono is universally more accurate.

Implemented contracts, all additive:

- Deployment gate `POINT_CLOUD_AI_ENHANCEMENT_ENABLED=false`; project JSONB
  `point_cloud_enhancement_config` defaults to
  `{schema_version: 1, enabled: false, profile: omega-current-mono-512}`.
  Only `project.manage` may change it. Return availability independently from
  image rendering overrides. New projects and clones/templates start disabled.
- Asset owner: primary LiDAR `DatasetItem` plus profile and an input-binding
  digest of ordered camera roles/item IDs. Multiple authorized tasks using the
  same input binding share assets. Legacy direct-file tasks are unsupported in
  the first pilot, with an explicit reason.
- Add dedicated `point_cloud_auxiliary_assets` and
  `point_cloud_auxiliary_generations` tables. Use immutable generation prefixes,
  an active-generation pointer, and one in-flight attempt per asset. Reuse the
  immutable publication pattern of ADR-0062, not its image-specific models.
  The generation stores source identities/fingerprints, per-camera calibration
  revisions/digests, effective transforms, model/checkpoint/preprocessing/filter
  digests, point counts, sizes, hashes, producer identity and publication time.
- Publisher input is a local verified bundle and explicit dataset/task mapping,
  never a browser-supplied storage prefix. Check source image/LiDAR hashes,
  calibration and coordinate provenance against platform items. Scene names and
  frame numbers alone cannot bind an artifact. Preserve the original experiment
  outputs; publish platform copies only after verification and an atomic final
  source check. Failed or interrupted attempts do not replace an active result.
- Save per-camera auxiliary XYZ/RGB as bounded binary PCD, with provenance and
  source-pixel/confidence records in an audit sidecar. Existing research binaries
  need an explicit conversion; do not treat them as platform PCD files. Output
  is in the primary point-cloud coordinate frame with its declared axis
  convention; frontend normalization runs exactly once. Never apply neighbor
  ego alignment to already aligned AI outputs.
- `GET /tasks/{task_id}/point-cloud/augmentation` returns gate/eligibility state,
  asset ID, active generation, input digest, profile, per-camera logical assets,
  coordinate declaration, counts/sizes/digests, and stable reason codes. It
  performs no generation. Keep the existing primary manifest response unchanged.
- `POST /tasks/{task_id}/point-cloud/augmentation/assets` accepts only the listed
  asset ID, generation and camera role, and returns a short-lived signed URL
  (at most 300 seconds). Both routes verify task visibility and
  `annotations:read`, current task bindings, project/deployment gates and source
  fences. Clients cannot submit bucket names, keys, arbitrary URLs or paths.
- Poll capability/generation at most once per 30 seconds while AI is selected,
  and revalidate on focus/context change. Signed-URL refresh is separate from
  content identity; renew once on expiry, without an infinite retry loop.
  Cache keys contain account/task access context, asset/generation, camera,
  coordinate digest and display budget. Clear account-scoped state on logout.
- Reconcile expired publisher leases, partial prefixes and unreachable owners;
  keep ready assets while their dataset inputs exist. Dataset deletion and
  project unlink/revocation must remove visibility immediately at the API;
  object cleanup must respect assets still shared by other tasks/projects.
  Retain failed publication metadata and bounded diagnostics for diagnosis.

**Coordinate gate:** the old nuScenes importer writes camera calibration files
from the first frame and attaches them across a scene. The offline experiment
instead reconstructs exposure-specific transforms from source poses. Existing
source metadata already contains acquisition tokens, source hashes, sensor and
ego poses, and the original platform calibration digest. The publisher must
check the effective transform for every frame, not just compare a stored digest.
Materialize correct per-frame calibration on new qualified imports. For existing
pilot data, use a manager-run repair path through append-only calibration
revisions with expected revision/digest checks, and only when the stored value
still matches the import baseline. Preserve manual calibration edits; reject
conflicts for explicit review. Publishing occurs after this check/repair and
binds the resulting authoritative revision. Do not alter calibration in a GET,
fit a hidden registration, or draw incompatible data as if validation passed.

The existing held-out experiment assets use a calibration subset; record that
profile and its anchor lineage when importing them. A later producer using all
current LiDAR points is a distinct generation/profile with new quality evidence.
Neither production inference nor candidate filtering may require GT boxes,
instance tracks or evaluation target selection. Unsupported input layouts and
insufficient scale anchors leave AI unavailable rather than guessing scale.

### Container boundary for managed preparation

Deploy the model producer as one independent `vggt-omega-backend` GPU HTTP
container under `apps/vggt-omega-backend/`, following the existing first-party
backend layout. Add an opt-in `gpu-vggt-omega` profile to
`docker-compose.ml.yml`. Keep the first prepared-asset slice usable without this
service; the container supplies new results in the managed-preparation slice.

```text
Manager starts preparation
          |
Platform API -> AsyncJob / Celery orchestration
                         |
                         +-> GPU admission -> vggt-omega-backend
                         |                    RGB -> depth/confidence artifacts
                         |
                         +-> CPU calibration/filtering using current real LiDAR
                         |
                         +-> verified immutable publication -> object storage
                                                                    |
Workbench auxiliary API <---------------------------------------------+
```

| Owner                             | Responsibility                                                                                                                               |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Platform API                      | Project/task authorization, experiment/profile eligibility, creation of a durable preparation job and read access to published assets        |
| Celery preparation owner          | Frame selection, idempotency, bounded dispatch, progress, retry/cancellation, input fencing, CPU postprocessing and publication              |
| Model container                   | Pinned Omega source/weights and preprocessing, model loading/inference, per-camera depth/confidence export and truthful GPU lifecycle/health |
| Object storage and asset registry | Staged binary artifacts, publication checks, immutable final PCD/provenance objects, active generation and cleanup                           |
| Workbench                         | Loading the selected published auxiliary layer and displaying its actual source/state                                                        |

Reuse the registry connection/authentication, health/setup, pool concurrency,
GPU dispatch and shared runtime/lifecycle helpers. `MLBackendClient.predict`
currently parses label-style `PredictionResult` values; its return type and
capability vocabularies do not describe reconstruction assets. Add a typed,
optional `/setup.point_cloud_densification` capability with protocol version,
`/reconstruct` endpoint and the allowed profile/input/output limits. Extend
capability parsing, lidar compatibility and the project experiment's backend
selector to consume it. Use a dedicated reconstruction client/response contract;
reuse GPU `predict` admission scope without passing artifacts through prediction
creation or advertising box/mask outputs. Keep existing prediction clients and
backend capability declarations compatible.

`POST /reconstruct` handles one bounded current-frame input set. Its request
contains an idempotency key, frozen profile/input digest, ordered camera roles,
short-lived input image URLs and job-scoped staging upload targets. Its response
contains binary depth/confidence descriptors, shapes, preprocessing-to-source
pixel transforms, digests and timing, not large JSON arrays. The platform verifies
these artifacts and performs metric scaling, bounded local correction, measured
ray protection and candidate sampling on the CPU before publishing final
auxiliary PCD. This keeps calibration/filtering policy under the same platform
owner for imported bundles and new model results. The model process receives no
platform database credentials; use scoped transfers rather than permanent
administrative object-storage credentials.

Pin model source commit, weight revision/hash and the validated execution shape
inside each producer profile. The initial mono profile retains the tested six
independent-image slots; changing batch shape or using all current LiDAR for
calibration requires its own recorded profile/qualification. Mount previously
downloaded, verified weights as a read-only model volume; obtain missing gated
weights during authorized provisioning. Credentials belong to provisioning,
not the image build layers or an annotator's browser. Do not depend on a user's
interactive HF login inside a running service.

Use one HTTP process, a model pool capacity of one and one active inference per
container initially. Bind exactly one configured physical GPU; report its actual
identity and effective device. Derive the registry VRAM budget from the target
image/profile's measured peak plus operational margin. The offline observation
of approximately 6.9 GiB is not a deployment reservation. Register health/setup
and warmup/unload behavior using the existing contracts; publish managed GPU
capability only after the real container passes lifecycle qualification.

The container owns neither a second durable job queue nor active-generation
publication. Cancellation belongs to the platform job: stop future frame units,
fence late results and keep an active inference accounted for until its GPU work
has actually ended. Model restart, OOM, timeout or a lost response leaves the
affected unit retryable under the same input key, without duplicate publication
or changing the original annotation task. Subsequent jobs reuse the loaded model
when available; ordinary Workbench navigation reads assets without contacting it.

### Renderer and annotation boundaries

```text
Prepared bundle -> verified publisher -> immutable auxiliary assets
                                          |
Task ACL + source/calibration fences -> auxiliary API -> owner-scoped loader
                                          |                    |
Current primary manifest -> existing loader/cache ------------> PointCloudScene
Neighbor tasks/poses -> existing neighbor loader -------------> single renderer
                                                               |
                                                main view + measured-only tri-view
Annotation tools, masks, measurement and QC <--- primary geometry only
```

Add a dedicated auxiliary layer to `PointCloudScene`; reuse its renderer,
invalidation scheduler, display-style helpers and Legacy/WebGPU primitives.
Keep neighbor-specific transforms/colors in the existing neighbor path. Do not
append model points to primary geometry, change source row indices, mutate raw
attributes, insert model points into raycast targets, or change point-count QC,
mask/export payloads, frustum selection, box fitting or measurement calculations.
Users may visually adjust a box while looking at AI, as with other references;
the interface must not imply that such a box has been validated by real returns.

Both neighbor and AI supplementary points stay out of orthographic refinement in
this pilot, consistent with the existing neighbor geometry boundary. Keep the
tri-view's selected appearance but label its measured-only content. The main view
always retains measured points and edit-overlay priority. Validate depth,
transparency and render ordering in both rendering paths, not only screenshot
appearance. Device loss/fallback must rebuild or clear the auxiliary layer through
the same scene owner, with no second canvas/context.

Reuse the persistent compute session for bounded auxiliary PCD decode. Its
current decoder discards PCD RGB, so add an auxiliary-specific XYZ/RGB result
without changing primary height-color behavior. Reuse the cache owner with a
separate auxiliary namespace; do not evict active primary frames for model data.
Start with one visible camera, no speculative AI prefetch, at most
`min(80_000, round(pcdDecimate / 8))` displayed auxiliary points, three retained
auxiliary entries and a 32 MiB combined auxiliary buffer/decoded-cache cap.
These are proposed conservative pilot limits, not measured optimal values.
Publish retained/total model counts when decimation occurs. Schedule auxiliary
work after the primary frame becomes usable; late completions cannot attach to
a different task/frame/camera/generation. Tests must cover worker transfer,
cache eviction and renderer disposal without detaching primary buffers.

### Delivery slices and current status

1. **Experimental Workbench with prepared assets — implemented.** Implement the selector,
   legacy mapping, gate/config, verified publisher, immutable registry and read
   APIs, coordinate qualification and single-camera auxiliary rendering together.
   Pilot on the existing qualified scene with explicit users; it must be usable
   inside the real annotation Workbench, including read-only/reviewer access,
   with no dependency on the standalone review server. Include the acceptance
   evidence and current-system documentation in the same change.
2. **Managed preparation — implemented; enforce qualification pending.** Add manager-triggered
   selected-task/scene preparation, never generation on task-open or on a display
   toggle. Use an `AsyncJob` kind `point_cloud_densification`, bounded per-frame
   units, idempotent input keys, progress, cancellation, retry and partial results.
   Reuse platform GPU admission and lifecycle ownership through a qualified
   backend adapter; a Celery concurrency limit alone does not prove GPU admission.
   The current arbitration package has rollout gates: do not assume every
   configured backend can use enforce dispatch. If the producer is unavailable,
   retain the prepared-asset pilot instead of starting unmanaged GPU subprocesses.
   No deployment GPU number or resource budget is inferred from this workstation.
3. **Broader qualification — pending.** Expand scenes and annotator trials, assess thin
   targets and false surfaces, and isolate mono/joint filtering effects. Consider
   full-surround camera fusion or a later automatic hybrid only with new evidence;
   neither is necessary for the first two usable slices.

The first slice affects more than eight files across API, asset/data lifecycle,
Workbench preferences, renderer and documentation. Expected implementation owners:

| Area                                | Files to extend or add                                                                                                                                                                                                                                                                                                      |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gates and project capability        | `apps/api/app/config.py`, `.env.example`, `DEV.md`, `apps/api/app/db/models/project.py`, `apps/api/app/schemas/project.py`, `apps/api/app/api/v1/projects.py`, project clone/template serializers, `apps/web/src/api/projects.ts`, existing project rendering-settings section                                              |
| Asset registry and publication      | New `apps/api/app/db/models/point_cloud_auxiliary.py`, `schemas/point_cloud_auxiliary.py`, `services/point_cloud_auxiliary/`, task router module and registration, model imports, one additive Alembic migration from the implementation checkout's current head, and a publisher/reconcile CLI under `apps/api/scripts/`   |
| Coordinate qualification            | `apps/api/scripts/import_nuscenes_scene.py`, shared source/transform helpers, `apps/api/app/services/sensor_calibration.py` callers, and a manager-run compatibility repair command with CAS checks                                                                                                                         |
| Preferences and settings            | `apps/api/app/schemas/user.py`, `apps/api/app/api/v1/me.py`, `apps/web/src/api/auth.ts`, `state/useWorkbenchConfig.ts`, `state/workbenchSettingsFields.ts`, their focused tests, and a shared augmentation resolver/write helper                                                                                            |
| Workbench delivery                  | `apps/web/src/api/tasks.ts`, new auxiliary query/control modules, `stages/three-d/ThreeDWorkbench.tsx`, `PointCloudScene.ts`, `pointCloudAssetCache.ts`, persistent compute/decode modules, `useNeighborPointClouds.ts`, and focused lifecycle/renderer tests                                                               |
| Managed preparation, slice two only | `apps/vggt-omega-backend/`, `docker-compose.ml.yml`, shared protocol/runtime packages, API capability parsing and reconstruction client, `AsyncJob` kind, worker dispatch/terminal reconciliation, cancel API, job presentation/notifications and preparation controls; audit these owners again before this separate slice |
| Contracts and user documentation    | Regenerated OpenAPI/TypeScript artifacts, `README.md`, `apps/api/scripts/README.md`, point-cloud user guide and data-model reference, project experiment instructions, env-var reference generation, an ADR for the accepted auxiliary asset/truth boundary, and an Unreleased entry only when the feature is implemented   |

### Acceptance and rollback

- CPU/API: disposable-database migration and registry constraints, authorization
  across projects/tasks, publication deduplication and source-change races,
  calibration conflicts, invalid/corrupt bundles, URL expiry and logical-asset
  validation, interrupted publication recovery, shared-owner deletion and cleanup.
  Extend relevant `test_pointcloud_manifest.py`, preference and calibration/import
  tests; add dedicated auxiliary asset tests without needing GPU weights.
- Preferences/UI: all three legacy mappings, compound/partial patches, reload and
  account switching, requested versus actual source, missing directions, failures,
  quick frame/task/source/camera changes and late responses. Assert no hidden
  neighbor PCD fetch in explicit AI/off mode, while independent neighbor boxes
  continue working. Check light/dark themes and compact-width keyboard access.
- Annotation: edit/save/undo/redo, picking and original indices, point masks,
  source-only counts/measurement/box fitting, scene navigation/playback, QC issue
  locators and read-only guards. Tri-view stays measured-only. Demonstrate payload
  and source identity equivalence with augmentation off/on for the same actions.
- Actual browser/device: qualify Legacy WebGL2 and the selected WebGPU or fallback
  backend, one renderer/canvas, stable idle rendering and bounded resources over
  repeated switches. Report the actual adapter. The prior SwiftShader preview is
  functional evidence only. Preserve the current benchmark thresholds: 250 ms
  cold geometry, 120 ms warm geometry, 50 ms tri-view first-open, and first-open
  within 10 ms of reopen. Existing failures stay documented. For the limited
  experimental pilot, require no more than 10% additional primary-frame p95 latency
  against an interleaved same-device baseline; broader renderer promotion still
  requires the unchanged absolute gates. Record auxiliary-ready latency separately.
- Benefit: compare current measured, traditional neighbors and AI on the same
  representative annotation tasks. Record completion time, corrections, missed
  moving/thin targets and reference box error; do not promote on point count or
  a coverage metric alone. The experiment remains opt-in until benefit is shown.
- Scale: evaluate ten times the pilot frame/task count with bounded memory and
  paginated administrative coverage; do not load a full scene's model points into
  the browser or repeatedly generate the same DatasetItem binding for each task.
- Rollback: turn off the project or deployment gate; refresh capability state and
  clear auxiliary geometry, without rewriting user intent or annotation data.
  Preserve valid immutable artifacts for diagnosis/re-enabling. Roll back the
  frontend first if reverting the API schema; do not destructively downgrade
  asset tables while a publisher or consumer still uses them.

The weakest assumption is that improvements seen in a single-camera offline
comparison transfer to useful, correctly aligned Workbench geometry. Per-frame
coordinate verification and prepared-asset delivery bound the integration risk;
default-off display, measured-only editing and explicit source comparison keep
the pilot useful even if AI quality or generation availability is inconsistent.

## Remaining qualification gates

1. **Useful geometry:** isolate filtering and sampling effects, inspect thin/small
   moving targets and false surfaces, and validate additional scenes without
   selecting examples after seeing results. More points do not establish accuracy.
2. **Deployable inputs:** current-frame model selection does not require GT actor
   boxes; GT still defines evaluation groups and preview locators. Any future
   temporal mode must replace oracle masks/trajectories and pass its own checks.
3. **Asset lifecycle:** immutable source-bound generations, asynchronous dispatch,
   cooperative cancellation, retry and prepared-asset delivery are implemented.
   Expand recovery/fault qualification before enabling managed GPU enforce mode.
4. **Workbench behavior:** the existing renderer/cache owner, separate measured
   and auxiliary identities, frame switches and comparison lifecycle are covered
   by focused tests. Expand production-device and long-session trials.
5. **Hardware and benefit:** qualify actual rendering on the target GPU, memory and
   active-frame budgets, and annotator accuracy/time on representative scenes.
   Current-frame WebGL2 evidence used SwiftShader; older Canvas2D evidence also
   does not qualify production GPU rendering.
6. **Existing surface performance:** profile warm navigation-to-paint and Dockview
   tri-view activation, then compare both display styles against the unchanged
   promotion thresholds. The detailed benchmark history remains in the archived
   surface plan; cleanup does not turn its exceptions into passes.

## Implementation and maintained documentation

- Model inference: `apps/vggt-omega-backend/`.
- Current-frame geometry and jobs: `apps/api/app/services/point_cloud_densification/`
  and `apps/api/app/workers/point_cloud_densification.py`.
- Source-bound assets and import: `apps/api/app/services/point_cloud_auxiliary/`
  and `apps/api/scripts/publish_point_cloud_auxiliary.py`.
- Workbench display: `apps/web/src/pages/Workbench/stages/three-d/`.
- Offline trial scripts, tests and four preview templates are retired; their
  pre-cleanup source is archived locally under `retired-runtime-source/` in the
  historical experiment directory. Full old artifact audits require regeneration.
- [Developer route](../../docs-site/dev/reference/point-cloud-data-model.md#vggt-ω-点云增密主路线).
- [Script inventory](../../apps/api/scripts/README.md#vggt-ω-平台链路与试验归档)
  and [research index](../research/README.md#当前点云增密路线).

## Outcome

- Consolidated six session plans here and archived their original filenames;
  execution history remains available without competing current defaults.
- Persistent route and comparison boundaries are recorded in
  `docs-site/dev/reference/point-cloud-data-model.md` and `docs/research/README.md`.
- Implemented one augmentation selector, compatibility for implicit surface
  neighbors, prepared-asset delivery and manager-triggered generation. Managed
  generation uses an independent model container, platform-owned CPU calibration
  and publication, and an optional reconstruction capability namespace.
- Added migration 0175 and applied it to the disposable acceptance databases and
  the local development database. Refreshed only affected development workers;
  production and ordinary project experiment settings remain unchanged.
- Built the pinned producer image and ran real six-camera inference, cache
  retransmission, unload and reload on the second RTX 3090. Reload outputs were
  byte-identical; peak total device use was about 7.9 GB and post-unload total
  use about 689 MiB. These observations do not qualify the full signed GPU
  lifecycle fault matrix; `managed_verified` remains false.
- Real-model platform acceptance used two source-bound frames from scene-0061.
  Both generated successfully; a second job reused both ready assets. Front-camera
  display showed 60,505 and 50,935 AI points alongside unchanged 34,688 and 34,720
  measured points. Browser checks covered comparison reset, frame switches,
  neighbor/off source switching and surface appearance. Source hashes and all
  published PCD/audit hashes passed; no annotations or predictions were created,
  and temporary staging objects were absent after completion. The acceptance
  fixture and dedicated container were removed. This browser used Legacy WebGL2,
  so the observations do not qualify target-device rendering performance.
- Added [ADR 0076](../adr/archive/0076-current-frame-point-cloud-auxiliary-assets.md),
  [operations instructions](../../docs-site/ops/vggt-omega.md) and
  [API documentation](../../docs-site/api/guides/point-cloud-augmentation.md).
  Rendering performance, broader geometry quality and annotator benefit are not
  promoted by this implementation. No release milestone was assigned.
- Retired offline reconstruction and preview code after the user requested final
  cleanup and confirmed previews were no longer needed. Kept the 39 mono prediction
  files, 234 camera clouds and binding records needed by the offline publisher,
  compact historical evidence and the complete platform acceptance evidence.
  Per-file deletion hashes and unchanged retained-file hashes are recorded in
  `.artifacts/model-review/reconstruction-cleanup-20261014/`.

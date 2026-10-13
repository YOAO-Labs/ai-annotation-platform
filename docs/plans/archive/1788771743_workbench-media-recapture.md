# Workbench media recapture after dockable layouts

> Status: completed and archived. All assigned replacements and new layout stories were reviewed and registered; deliberately retained or unrelated historical media remain outside this plan's scope.
> Baseline: `af2eb6ab`, 2026-09-07. No release milestone is assigned.

## Goal and acceptance

Update the documentation media to teach the current dockable workbench: task-appropriate presets, movable panels, tabs, hiding/restoring, canvas focus, and the 3D camera/tri-view modes. Preserve correct business demonstrations and real model outputs. Re-plan the shots before recording so that the action, its controls, and its result remain visible together.

The user's latest feedback accepts the basic content of the real SAM3 candidate demonstration, while identifying the larger layout migration. This is content feedback, not a completed per-asset review of the media registry.

Completion means every asset in the inventory has a recorded keep/recapture decision, all selected replacements show the current UI, missing generation evidence is repaired through actual capture, documentation and derivatives agree, and the final media review is performed against the committed files. A passing interaction test alone does not certify the published crop or poster.

## Evidence and scope

The UI source of truth is `apps/web/src/pages/Workbench/layout/`: `workbenchLayoutSnapshot.ts`, `workbenchLayoutPresets.ts`, `WorkbenchDockWorkspace.tsx`, and `workbenchPanelRegistry.tsx`. ADRs 0072 and 0073 describe the accepted boundaries. Current source has nine panel IDs, six annotate/review × image/video/3D contexts, and workspace schema 5. Do not copy historical schema numbers from older plans.

### Inventory, counted by current references

| Group                                       | Recording content groups | Dynamic derivative files | Static screenshots | Hero derivatives |
| ------------------------------------------- | -----------------------: | -----------------------: | -----------------: | ---------------: |
| Image basics                                |                       10 |                       19 |                  2 |                0 |
| AI and review, including cross-page stories |                       16 |                       47 |                  6 |                1 |
| Video                                       |                       18 |                       36 |                  1 |                1 |
| 3D                                          |                        5 |                       10 |                  1 |                1 |
| Total                                       |                       49 |                      112 |                 10 |                3 |

The **125 files are an inspection scope, not a blanket replacement list**. A content group may supply a video, poster, GIF, and homepage encodings; those are not independent recordings. Reuse one qualified source for its registered derivatives.

At planning time, the existing audit reported 199 referenced files. Its manifests contain 193 files: 132 flow artifacts and 61 screenshots. Two `ai-tracker-panel` artifacts are currently unreferenced; eight referenced artifacts lack generation entries: `docs-site/public/media/workbench/bbox-draw.mp4`, its poster, and the MP4/poster pairs under `docs-site/public/media/sam/` for smart-point, smart-box, and exemplar. The four Hero images are referenced through relative imports and are outside the current audit collector; three belong to this scope. Thus the broader actual reference inventory is 203 files, with 78 outside this workbench migration.

The earlier 135 stale review records are a dependency-based review result. They must not be substituted for this content inventory or automatically renewed.

### Immediate static-image decisions

| Action                                          | Exact assets                                                                                                                                                                                             | Reason                                                                                       |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Recapture                                       | `docs-site/user-guide/images/workbench/video-real-scene.png`, `pointcloud-real-scene.png`, `ocr-real-scene.png`; `docs-site/user-guide/images/review/workbench.png`                                      | Inspected full-workbench images still show the previous sidebars or tool-window arrangement. |
| Derive again after source replacement           | `docs-site/.vitepress/theme/assets/home/hero/video-track.webp`, `pointcloud.webp`, `review.webp`                                                                                                         | These embed the affected video, 3D, and review screenshots.                                  |
| Keep                                            | `docs-site/user-guide/images/workbench/layout-overview.png`                                                                                                                                              | Already recaptured with Dockview in this session.                                            |
| Inspect, then keep if the actual controls match | `docs-site/user-guide/images/mask-brush/toolbar-overview.png`; `docs-site/user-guide/images/sam/smart-point-toolbar.png`, `interactive-toolbar.png`, `magic-box-toolbar.png`, `exemplar-output-mode.png` | These crops do not expose the changed surrounding layout.                                    |

Retain the recently validated candidate-keyboard-review, e2e-quickstart, large-image-progressive, large-image-pyramid-recovery, and hotkey-cheatsheet media when their current composition fits the assigned profile. Re-record them only for an identified shot or interaction mismatch. Preserve their real inference evidence and existing validated behavior.

## Recommended approach

Use **four representative recordings to establish composition and interaction rules, then migrate the remaining affected content by task family**. The minimal alternative is rerunning all existing scripts unchanged. Reject it: those scripts can successfully reproduce an outdated composition, cannot teach the new layout features, and in two cases still inject inference results.

Use the application's existing presets and layout menu. Extend the existing recorder and source/derivative manifests; add no second layout engine, service, dependency, account, or public command. This is a broad media change involving more than eight files: approximately 49 existing content groups, selected screenshots, new layout stories, recorder helpers, and their documentation references. Work in independently reviewable batches.

The most fragile assumption is that an old script passing means its published composition is suitable. Remove that assumption by validating panel geometry and inspecting each actual clip and poster before publishing the batch.

### Composition profiles

| Profile           | Starting layout and visible controls                                                                                          | Use                                                                                                                                      |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Image annotation  | Standard annotation; task queue and class palette visible; canvas unobscured                                                  | Box, polygon, polyline, mask, and onboarding. Use the real focus command only when the story benefits from it.                           |
| Image AI review   | Image AI review preset; current-task AI and inspector share the intended group; switch the active tab as the story progresses | Real inference, candidate review, and attributes. Hide unused discussion through its real command when additional canvas area is needed. |
| Human review      | Review collaboration; wider inspector and discussion below                                                                    | Review and rejection stories, using the reviewer role.                                                                                   |
| Video tracking    | Video tracking preset; tracker/inspector grouped; timeline and selected frame remain visible                                  | Seeds, range selection, prediction review, propagation, and mask editing.                                                                |
| 3D box refinement | Standard workspace plus the existing box-refinement mode; tri-view visible                                                    | Main-view/tri-view editing with shared geometry and selection.                                                                           |
| 3D sensor fusion  | Sensor-fusion mode; explicitly demonstrate all-camera floating/docked switching                                               | Projection, camera-seeded boxes, and camera selection. Use the gallery's real responsive grid/scroll behavior.                           |

Do not treat the selected-object information card as a Dockview panel: it remains an independent overlay. Collapse or reposition it through its actual supported UI when it hides the target. The canvas is menu-repositioned/maximized; it cannot be closed, floated, or tabbed. Cameras switch as a whole group; the docked gallery is not a third kind of independent floating window.

Document videos retain a consistent desktop composition and dark theme. Use the existing docs profile and measured source frame rate; never advertise repeated frames as 60fps. Homepage replacements retain the existing qualified marketing-master requirements. Review loading only outside the core action; preserve the causal sequence from input to result.

## First representative batch

| Order | Flow and status                                                        | Shot sequence                                                                                                                                                                                               | Required evidence                                                                                                                                                                             |
| ----- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | `ai-tracker-panel` — reuse and reintroduce its already migrated script | Open tracker → float → dock right → hide → reopen → open current-task AI → combine tabs → return to tracker. Target 18–24 seconds with readable pauses.                                                     | Single instance for each panel; populated form/selection survives hiding and grouping; no model-inference claim because this story dispatches no job. Place it in the workbench layout guide. |
| 2     | `current-task-image-inference` — reframe existing live OCR story       | Apply image AI review → show current-task configuration → dispatch → show real candidates → switch to inspector → accept one → show saved annotation. Target 20–30 seconds excluding pre-interaction setup. | Real job and model result, correct candidate index, accept response and reload count; AI panel and target stay visible.                                                                       |
| 3     | `video-track` — reframe existing video overview                        | Establish video layout → play/pause → choose frame → draw or select the subject → inspect the timeline/track → show the saved result. Target 15–25 seconds.                                                 | Correct task/frame, actual media rectangle, visible timeline, real annotation state. A tracking inference is required only if the final storyboard claims one.                                |
| 4     | `pointcloud-camera-seed-3d-box` — migrate camera presentation          | Establish sensor-fusion mode → show docked camera gallery → choose front camera → seed a box → show main 3D and tri-view correspondence → save. Target 20–30 seconds.                                       | Official nuScenes fixture/pose, correct camera role, projection and pointer alignment, shared selection and saved geometry. Validate on the real rendering host.                              |

These establish layout teaching, AI review, video, and 3D composition. The four clearly outdated static views are recaptured with their corresponding profile in the same batch. Update only the references delivered by that batch.

## New layout teaching stories

Create these after the representative layout clip has established the visual pacing. Each story is independently useful and can be published without waiting for all old videos.

| Planned flow ID                | Target and placement                                                                                | Story and acceptance                                                                                                                                                                                                                                 |
| ------------------------------ | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `workspace-layout-basics`      | `docs-site/public/media/workbench/layout-basics.mp4` plus poster; workbench index, layout section   | Standard preset → resize a divider → combine tabs → title-bar hide → restore from layout menu → focus canvas → restore. Keep the same canvas instance, current task/tool, selected annotation, and any unsent discussion draft during rearrangement. |
| `workspace-layout-persistence` | `docs-site/public/media/workbench/layout-persistence.mp4` plus poster; workbench settings           | Adjust and save layout → switch task/context and return → reload → enter/leave compact width. Verify the real preferences PATCH/GET, context separation, and restored desktop tree. Do not promise unsent discussion drafts survive a full reload.   |
| `pointcloud-panel-layout`      | `docs-site/public/media/pointcloud/panel-layout.mp4` plus poster; 3D box and pointcloud-view guides | Move/float/redock tri-view as one panel → switch all cameras floating/docked → hide/restore → switch frame. Verify rendered backgrounds, picking, synchronized selection, and stable camera roles.                                                   |

Planned IDs are not currently executable recording selections. Register their requirements, recipes, and assertions before using the existing recording CLI with them.

## Recorder changes before bulk capture

1. Extend `flows/_workbench-layout.ts` to select the real task-specific presets and wait for the expected visible panels, active tabs, and bounds. Retain one preferences sandbox for composition-only flows. Stop clearing workspace and driving every recording through legacy `both`/`none` settings.
2. For layout persistence, use the existing isolated screenshot account with the real preferences API. Preserve and restore that account's starting preferences in teardown. An intercepted in-memory PATCH is not persistence evidence. Reuse assertions from `workbench-layout.spec.ts`, `workbench-ai-tracker-layout.spec.ts`, and the 3D layout tests.
3. Keep media coordinates relative to `renderedMediaBounds`. Replace fixed selected-card positions, retired `triViewFloat` settings, fragile parent-chain selectors, and camera locators tied exclusively to `[data-floating-panel]`. Locate the explicit camera mode/role and `[data-workbench-panel="camera-view"]` when docked. Native floating camera mode remains valid when that is the story being taught.
4. Convert `candidate-review-lifecycle` to real model results with original prediction IDs and shape indices, reusing `prepareLiveCandidateReview`. Remove `smart-scribble` setup/inference response interception from its published real-model story; use the advertised live scribble capability. If the required backend rejects the real operation, fail and diagnose that flow rather than substituting fixture output.
5. Distinguish **backend availability** from **performed inference** in recording declarations/evidence. A panel-only demonstration may need model capabilities but performs no inference. Archive real job/model/result evidence for inference stories and mark non-inference stories accordingly. Keep standard versus marketing quality independent of that distinction.
6. Put per-flow preparation and recording metadata with the individual flow modules; keep the common runner stable. Newly completed reviews should watch the actual flow, shared helpers, and consuming documentation. Do not retroactively remove watched paths or move verification commits just to clear old failures.
7. Repair the eight referenced-but-unrecorded manifest entries through actual source capture/derivation. Include the three workbench Hero imports in the maintained inventory and review workflow; keep the unrelated data-manager Hero. Align the media collector with actual static imports so its published-file total includes them.

## Remaining recording groups

The following are executable flow names unless noted. Final target mappings are owned by `apps/web/scripts/derive-doc-media.mjs`, not a second hand-written output-path registry.

| Family            | Existing flows                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Work                                                                                                                                   |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Image basics (10) | `bbox-draw`, `rotated-bbox`, `polygon-draw`, `polyline-draw`, `mask-draw`, `large-image-progressive`, `large-image-pyramid-recovery`, `large-image-mask-limit`, `hotkey-cheatsheet`, `e2e-quickstart`                                                                                                                                                                                                                                                                                                                  | Reframe older full-workbench shots; retain recently validated current-layout captures; add missing bbox generation evidence.           |
| AI/review (16)    | `sam-interactive`, `sam-tool-smart-point`, `sam-tool-smart-box`, `sam-tool-exemplar`, `ocr-inference`, `candidate-keyboard-review`, `candidate-review-lifecycle`, `current-task-image-inference`, `secondary-inference-attribute`, `ai-prediction-import`, `review-reject`, `pipeline-apply-project`, `jobs-retry-recovery`, `project-ml-routing`, `smart-scribble`, `ai-preannotate`                                                                                                                                  | Use the AI/review profiles and real result lineage. Reframe workbench endings in cross-page stories as well as direct workbench flows. |
| Video (18)        | `current-frame-video-inference`, `video-tracker-job-states`, `video-mask-correction-propagate`, `video-mask-track-edit`, `video-timeline-prediction-navigation`, `video-propagate-track-vs-copy`, `video-track-batch-propagate`, `video-tracker-box-seed`, `video-tracker-combo-discovery`, `video-tracker-cross-frame-points`, `video-tracker-positive-negative`, `video-tracker-range`, `video-tracker-text-discovery`, `video-chapter`, `video-draw`, `video-timeline-zoom`, `video-track-carryover`, `video-track` | Keep panel, timeline, target and decisions visible; preserve real seed frame/range and accepted track identities.                      |
| 3D (5)            | `pointcloud-billboard-label`, `pointcloud-camera-seed-3d-box`, `pointcloud-controls`, `pointcloud-crossframe-track`, `pointcloud-view`                                                                                                                                                                                                                                                                                                                                                                                 | Migrate presentation-specific locators and obsolete controls; show the new compact toolbar and menu-driven modes.                      |

Aliases: `sam-interactive` produces asset `ai-assisted-annotation`; `ocr-inference` produces `ocr-real-scene`; `sam-tool-*` produces `sam-tools/*`. Their homepage/document variants are derived from the corresponding source, rather than separately reenacted. The two existing unreferenced `ai-tracker-panel` files are outside the 125-file baseline; publishing the new layout story introduces references to them.

Keep the 13 non-workbench flows: `background-export-download`, `storage-connector-create-test`, `jobs-bell-active`, `pipeline-template-create`, `ai-pre-variant-selector`, `batch-bulk-actions`, `project-create-existing-resources`, `project-actions-menu`, `model-market-gpu-resource-overview`, `model-market-runtime-partial-failure`, `model-market-runtime-pool`, `model-market-video-pool`, and `platform-overview`. Keep unrelated authentication, administration, project/data management and settings screenshots. Review `settings/workbench-prefs.png` for actual settings changes only.

## Execution and verification

Use the existing local Python virtual environment and screenshot runtime. The verified disposable database is `annotation_screenshots_test`; the development services remain separate. Capture API and dedicated `screenshots@` workers must use identical database and Redis DB 15 settings, including migration/broker overrides. Port 3001 is occupied on this machine; use the established capture web port 3020 and API port 8010. No credentials are copied into plans, logs, or media.

Preflight already confirmed the installed ffmpeg, X11 display socket, and reachable SAM3, YOLO and RapidOCR health endpoints. The dedicated capture API/workers are started only for the selected batch and stopped after verification. Before each run, verify actual mounts, service targets, exclusive seed ownership, live capabilities and media access. Marketing qualification still needs its existing display/cadence checks; availability of a GPU alone does not qualify a master.

For each independently publishable batch:

1. Establish the declared layout and source requirements; use `screenshots:record -- --list` and the selected flow's `--plan`. Enroll unsupported flows before selecting them. Complete scoped interaction validation before bulk encoding.
2. Record the real flow, checking recent API failures and browser console errors. Verify successful paths and relevant edge cases: hidden/tabbed panel restore, no duplicate instances, saved decisions, task/frame switching, compact round-trip, and failed preferences recovery for the persistence story.
3. Inspect the source beginning, core action and ending. Derive only the selected registered targets with `docs:media:derive`; standard sources require an explicit inspected clip. Regenerate associated posters and Hero derivatives from their new source.
4. Review the actual published beginning/core/end/poster, then run `screenshots:record:test`, `screenshots:docs-media:test`, relevant locator/assertion tests and `screenshots:lint`. Run type/lint checks for changed scripts and `git diff --check`; do not repeat unrelated application acceptance tests for media-only edits.
5. Update affected user-guide references/captions, `docs-site/maintainers/image-checklist.md`, generation manifests, and CHANGELOG in the same batch. Commit before final per-asset human review. Run strict media audit against the resulting commit. A release audit is required only for a requested release.
6. Clean generated test data/intermediates and stop the dedicated runtime when finished. Retain the verified private source archive until its requested backup is confirmed. Publish/push only within the user's existing authorization.

If one model/backend fails, keep that batch's previous published assets and diagnose the failed flow; other completed batches remain useful. A failed derivative must leave its previous target untouched. Rollback is a Git revert of the affected recording scripts, media, manifests and references; production annotation data is outside this work. Restore the isolated recording account's preferences and clean its jobs/annotations regardless of success.

## Completion record

At implementation completion, append an Outcome section with actual commits, delivered documentation and remaining per-asset review gaps. This proposed plan does not mark any asset reviewed, approve a push, or assign a release version.

## Outcome: first representative batch

Delivered four real-browser recordings with their four posters, four updated workbench/review screenshots, and three regenerated homepage Hero images (15 published files). All four recording tests and all four selected desktop-dark screenshot scenes passed. Published video beginnings, core operations, endings and posters were inspected; all 15 assets loaded successfully in the browser preview. Documentation build, web typecheck, focused script lint, recording unit tests and static reference/manifest checks passed.

- `ai-tracker-panel`: 21.4 seconds; floating, docking, hiding/restoring and tab grouping preserve the bidirectional 60-frame configuration. This panel demonstration performs no model inference.
- `current-task-image-inference`: 18 seconds; real RapidOCR results, prominent heading selection, original prediction/shape identity, manual acceptance, and persistence after reload. The source archive contains the actual job and prediction evidence.
- `video-track`: 21.2 seconds; verified forward/backward frame stepping, real playback/pause, a manually drawn truck keyframe, saved track selection and reload verification. The poster shows the saved track and timeline.
- `pointcloud-camera-seed-3d-box`: 20.667 seconds; docked cameras, real mouse-based seed geometry, double-click focus and the box-refinement preset. Hardware capture measured 59.97 fps with 66 unique frames out of 67 calibration frames. The 2592×1458 capture was resampled into a 4K master; it is not native 4K capture and performs no model inference.

The source recordings used live browser time to match API lease timestamps. Ordinary deterministic screenshots retain the fixed-clock default, while the four live workbench scenes explicitly select live time. Source and derivation commit/dirty state are recorded separately for Hero assets. Annotation cleanup registers saved IDs before subsequent UI assertions can fail.

Relevant integrated commits: `3b0a8a84` (tracker/video recording assertions), `04a3e15e` (persisted annotation identity for discussion), and their merges `ee0e0550` / `a6044bbf`. The four screenshots and final recordings were captured from `a6044bbf` with local recording changes; their dirty provenance is intentional and preserved. This outcome accompanies the media batch commit rather than inventing a clean capture commit. Final source archives were copied outside the checkout and verified by SHA-256 before intermediate cleanup.

The collector now includes all four statically imported homepage Hero images and the two reintroduced panel-demo references: 205 published files in the broader inventory. No human review record was renewed. Missing historical generation evidence, affected historical review records, the remaining content families, and the three planned dedicated layout stories still need the subsequent batches described above. The 49-group / 125-file planning scope must not be reported as fully recaptured. No release version or push approval is implied by this batch.

## Batch 2: image basics and layout teaching

The user accepted the first 15 published files and requested continuation. Commit `49584ba6` records exactly that approved set against `ea71c3e4`; it does not approve unrelated media or authorize a push.

This batch records the nine image-basics groups other than the retained `e2e-quickstart`, plus the new `workspace-layout-basics` teaching flow. Publish each video and its poster, and replace the Mask toolbar crop: 21 files. The retired, unreferenced drawing/hotkey GIF targets remain archived recipes, not additional publication targets.

| Decision  | Content                                                                                           | Evidence / action                                                                                                                                                                                                                                                 |
| --------- | ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Recapture | bbox-draw, rotated-bbox, polyline-draw, polygon-draw, mask-draw                                   | Use the standard image-annotation preset with visible class palette and inspector; confirm the real saved geometry and reload it. Give the class picker a readable pause and clean exact annotation IDs in finally.                                               |
| Add       | workspace-layout-basics                                                                           | Actual divider resizing, tab grouping/switching, title-bar hiding, menu restore, canvas focus and restore; retain the same canvas/editor instances, selected persisted annotation and unsent discussion. This is composition, not backend preference persistence. |
| Recapture | large-image-progressive, large-image-pyramid-recovery                                             | Composition and real tile requests remain valid, but existing posters showed impossible lease timers around 80,984 minutes. Keep the intended large-canvas story with the live-clock environment.                                                                 |
| Recapture | large-image-mask-limit                                                                            | Existing poster/video at 11.8 seconds showed 48,567 minutes, old toolbar icons and a pet overlay. The actual size limit and vector-annotation story remain correct.                                                                                               |
| Recapture | hotkey-cheatsheet                                                                                 | Existing ending at 11.7 seconds showed about 80,951 minutes; shortcut groups and search behavior still match current source.                                                                                                                                      |
| Recapture | mask-brush/toolbar-overview                                                                       | The old title used scissors; the current toolbar uses a brush. Replace obsolete annotation selectors with the actual brush/eraser accessible labels.                                                                                                              |
| Keep      | sam/smart-point-toolbar, sam/interactive-toolbar, sam/magic-box-toolbar, sam/exemplar-output-mode | Agent image/source inspection confirmed current icons and relevant control ordering. No human review record is inferred from this inspection.                                                                                                                     |
| Keep      | e2e-quickstart                                                                                    | The recent GIF shows login, drawing/class selection and successful submission to the next task with the current layout.                                                                                                                                           |

The first bbox probe passed real drawing, class save and reload checks. Its source was inspected; the class-confirmation moment was too short for teaching, so the final recordings use an explicit one-second confirmation pause. The shared capture runtime remains isolated to `annotation_screenshots_test` and Redis DB 15, with exclusive seed ownership. Source archives, publication clips and final results will be recorded after the selected runs complete.

## Outcome: second image/layout batch

Published ten MP4/poster pairs and one Mask toolbar PNG (21 files). The first ten-flow run passed nine interactions but exposed an actual rotation bug; visual inspection also caught a Mask load failure and a layout recording artifact that the original interaction assertions did not detect. Those three sources were rejected. After fixing their causes, all three replacement recordings passed. The other seven clips were inspected and retained without repeating their completed acceptance work.

| Published story              | Duration | Qualified standard source / inspected clip      |
| ---------------------------- | -------: | ----------------------------------------------- |
| bbox-draw                    |    8.0 s | `2026-09-07T10-48-02-433Z-2190341`, `72.2:8.0`  |
| polyline-draw                |   13.0 s | same run, `66.2:13.0`                           |
| polygon-draw                 |  14.32 s | same run, `65.4:14.3`                           |
| large-image-progressive      |  13.56 s | same run, `69.5:13.5`                           |
| large-image-pyramid-recovery |   17.6 s | same run, `69.2:17.6`                           |
| large-image-mask-limit       |  12.84 s | same run, `66.7:12.8`                           |
| hotkey-cheatsheet            |  11.16 s | same run, `68.1:11.1`                           |
| workspace-layout-basics      |   20.2 s | `2026-09-07T11-23-57-107Z-2321898`, `66.6:20.2` |
| rotated-bbox                 |   9.96 s | same replacement run, `6.1:9.9`                 |
| mask-draw                    |   8.52 s | same replacement run, `72.4:8.5`                |

Published standard videos are 1280×720 at 25 fps; they are not marketing masters and perform no model inference. The tile-recovery story injects one failure into a real tile request, then verifies actual retry and full coverage. The final toolbar screenshot uses the current brush/eraser controls and enough padding for its labels. Its fixed clock is outside the crop; recording sources use live time.

The layout teaching flow retains the same canvas/editor DOM instances, task, tool, selected saved annotation and unsent discussion through resizing, tab switches, hiding/restoring and focus/restore. It does not claim that an unsent draft persists across reloads. All five drawing stories verify real saved geometry and reload the same annotation. The Mask story additionally requires native raster content, ready decoded statistics both before and after reload, zero temporary-ID content requests and zero failed content responses.

Three bounded corrections accompany this batch:

- `3da4b76f`: image rotation uses pixel-space offsets, fixing aspect-ratio-dependent angles. Three regressions failed before the fix; eleven related tests passed afterward. The replacement recording also verifies the saved 35-degree angle.
- `ed294a89`: optimistic image/video Mask IDs no longer fetch persisted content. A shared SHA-only in-flight request had transferred a temporary ID's failure to the saved ID; a different source now checks its own endpoint after a shared failure, while successful content deduplication and genuine errors remain intact. Seven new regressions failed before the fix; 33 relevant tests passed afterward.
- `ab82bde7` / `ecc00afc`: a pure Konva reproduction isolated the visual ghost to accelerated 2D Canvas under the screenshot browser's SwiftShader configuration. Disabling that 2D path removes stale pixels without adding application redraw logic. The independent real-browser regression fails with the old configuration, passes with the new one, and confirms that software WebGL2 still works.

Final source beginnings, core operations, endings and published posters were inspected. The three replacement traces contain no HTTP errors or browser console errors. The selected toolbar capture passed; recording unit tests (5), derivation tests (2), screenshot lint and strict static manifest checks passed. The documentation build passed. Broader e2e configuration TypeScript checks still expose pre-existing marketing-recorder/config and unrelated legacy-flow typing issues; changed drawing helpers and the new canvas regression passed focused type checks, and application type/lint checks passed in the implementation commits.

The local preview serves all 21 exact files successfully with matching SHA-256 hashes. The existing browser connection was unavailable for a final preview-page inspection; this does not replace or invalidate the successful real-browser capture tests and published-frame inspections. No second-batch human approval is recorded. The broader inventory now contains 207 referenced files; remaining task families and historical review gaps continue under the original plan. No release or push is implied.

## Batch 3: SAM interaction and candidate decisions

The user approved the second batch after review. Commit `01512ab1` records exactly its 21 published files against `174cc739`; no push or unrelated review approval is implied. The primary checkout remains on main; continuation stays on the existing feature checkout so that the completed media and fixes are retained.

This batch covers six real-inference stories and 24 referenced derivatives: three SAM tools (five files each: documentation MP4/poster plus homepage MP4/WebM/poster), Magic Box acceptance (the same five-output arrangement), candidate review lifecycle (MP4/poster), and saved-Mask smart scribble (MP4/poster). The currently referenced six `/media/sam/` smart-point/smart-box/exemplar files have no generation entries because only the homepage mappings existed. Add their three documentation mappings and derive both surfaces from the same qualified new source. Do not copy provenance from differently encoded historical homepage files.

Keep the current candidate-keyboard-review pair: its real SAM3 source, current Dockview controls, target geometry and final counts were inspected again and remain appropriate. Retired SAM interaction GIF recipes are not publication targets.

SAM recordings use the image AI-review preset, hide unused discussion and show the class palette and annotation-detail tab. They record actual model/routing/result evidence, register saved annotation IDs before subsequent checks, verify saved geometry after reload and delete exact temporary annotations. Candidate readiness is read from the canvas rather than optional pet text. Lifecycle decisions preserve the original prediction/shape identity and actual candidate count. Smart scribble retains a real stored native Mask input while removing the fabricated setup/inference response; its positive and negative prompts must run against an advertised live capability.

The capture API, worker and Vite remain isolated at ports 8010/3020 with `annotation_screenshots_test` and Redis DB 15. All selected flows share exclusive seed ownership. The selected sources and visual inspection results are recorded below.

### Batch 3 outcome

All six selected browser stories passed with real SAM3 results and exact temporary-record cleanup. Publish only these qualified continuous clips; rejected attempts remain in the private evidence archive.

| Story               | Source run                         | Clip (start:duration, seconds) | Published files |
| ------------------- | ---------------------------------- | ------------------------------ | --------------: |
| Smart point         | `2026-09-07T14-08-23-014Z-2749963` | `69.8:9.6`                     |               5 |
| Smart box           | `2026-09-07T13-50-37-363Z-2703051` | `66.4:8.4`                     |               5 |
| Exemplar            | `2026-09-07T13-50-37-363Z-2703051` | `4.3:9.4`                      |               5 |
| Magic Box           | `2026-09-07T14-08-23-014Z-2749963` | `70.3:8.5`                     |               5 |
| Candidate lifecycle | `2026-09-07T14-08-23-014Z-2749963` | `4.6:15.0`                     |               2 |
| Saved-Mask scribble | `2026-09-07T14-42-20-019Z-2832894` | `73.0:16.0`                    |               2 |

The interactive recorder excludes endpoint warmup requests from inference evidence. Smart point switches through the actual three candidates with two Tab presses to select the whole car; the initial candidate covers only a license plate. The selected mask matches the saved mask digest. Exemplar accepts the model's original top result and preserves its geometry. Magic Box selects the class in the post-draw picker; its earlier palette is read-only.

Lifecycle starts with five real candidates. Skip leaves five, acceptance leaves four plus one saved annotation, rejection leaves three, and reload verifies those identities and counts. The accepted annotation retains the original prediction ID and shape index.

Scribble starts from a stored 26,933-pixel Mask and makes two real positive/background-hint requests. Both change the result; the final 25,391-pixel whole-car Mask updates the same annotation from version 1 to 2 with matching signed candidate, model routing and content digest. No additional annotation is created. The clip retains the actual reload and final persisted state. Prompts are model hints, so the guide does not promise monotonic pixel addition or removal. Candidate readiness checks decoded changed pixels and a real Konva layer draw event; animated candidate borders make the internal pending-draw flag unsuitable as an idle signal.

Validation includes 29 recording/helper unit tests, two media-derivation tests, focused TypeScript and ESLint checks, and independent visual inspection of the six source clips. Final derivative, documentation, preview and provenance checks are recorded with the private publication archive. Third-batch generation does not renew human approval or complete the remaining video, 3D and cross-page families.

Final review extended the shared commit callback to register native-Mask prediction lineage before any later UI assertion. SAM tools and Magic Box now use the same exact annotation/prediction cleanup as lifecycle and scribble, including marketing captures that do not rebuild the seed at the end. A regression case verifies registration even when class validation fails. This cleanup-only follow-up does not change the qualified recordings.

## Batch 4: video basics and timeline editing

The user reviewed and accepted the third batch. Commit `e3c5de73` records exactly its 24 files against `96bf0cad`; no push or approval of another batch is implied.

This batch covers five existing stories and ten referenced derivatives (one MP4 and one poster per story): `video-draw`, `video-track-carryover`, `video-mask-track-edit`, `video-timeline-zoom`, and `video-chapter`. All five mappings and generation entries already exist. No homepage or active GIF targets belong to this batch. Keep the previously approved `video-track` overview.

Inspection of the old beginnings, core actions, endings and posters confirmed that all five still use the old workbench shell. The carryover information card obscures the track list, Mask editing ends with a hidden timeline, and chapter text wraps in an overly narrow sidebar. Timeline zoom also reaches the minimum window span before its later gestures, and double-click both seeks and resets the window. The new composition uses the video-tracking preset, actual tabs and expanded timeline. A visible timeline requires checking opacity, because its auto-hide state remains present in the DOM.

| Story           | Required demonstration and verification                                                                                                                         |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Video draw      | Two manually saved keyframes on the same vehicle track; inspect the real intermediate geometry and reload the same annotation.                                  |
| Track carryover | Actual next-frame ghost and Tab continuation; preserve track identity, verify saved keyframe and auto-advance behavior.                                         |
| Mask track edit | Create F0 Mask, inspect held F5, erase and commit a distinct F5 keyframe; retain F0 content, verify both rendered masks, exact ID/version and reload.           |
| Timeline zoom   | Select a real saved track; use two effective pointer-anchored zoom steps, pan the window and reset with “Fit all frames”; frame and selection remain unchanged. |
| Chapter editing | Brush-select a range, create its named chapter, resize both ends, and verify the same chapter and final range after reload.                                     |

All five stories are manual interactions, so their recording plan requires no ML backend and makes no inference claim. Capture behavior evidence separately from inference evidence. Register created annotations and chapters before later assertions can fail and remove those exact records in finally/afterAll, including marketing mode. The shared disposable database remains `annotation_screenshots_test`, API 8010/web 3020 and Redis DB 15; the root recorder exclusively owns seeding.

### Batch 4 outcome

The five manual stories passed real browser verification, with ten MP4/poster derivatives regenerated from inspected standard sources. Their browser traces have no failed HTTP requests, console errors or page/test errors. The earlier four accepted stories were retained while the Mask composition was corrected; failed captures remain private diagnostic evidence.

| Story           | Verified result                                                                                                                                                                           | Published clip |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| Video draw      | One annotation/track, manual F0 and F8 keyframes, rendered dashed F4 interpolation, unchanged keyframe count after reload                                                                 | 17.6 s         |
| Track carryover | Tab selects the bus at F1; only that track receives its new keyframe; the truck keeps its original class, identity and version and becomes the next selection                             | 10.9 s         |
| Mask track edit | F0 foreground remains 28,766 pixels; the erased F5 keyframe has 25,643 pixels and distinct content, on the same annotation/track with version 1 → 2; native bitmap and reload checks pass | 20.0 s         |
| Timeline zoom   | Two effective pointer-anchored zoom steps, pan and fit-all preserve frame 0 and the selected annotation                                                                                   | 10.2 s         |
| Chapter editing | Manual F14–F34 chapter resized to F20–F48; both handle saves and reload preserve the same chapter without seeking the video                                                               | 15.1 s         |

Production fixes in `0372bf9c` and `7c13fb7a` restore explicit chapter brushing when a tracking session is retained, and distinguish an open docked tracking panel from its actually displayed tab. Only a displayed tracking tab temporarily folds the selection card; user folding preferences and tracking configuration remain intact. The final visibility regression uses the real Dockview owner and selection card, covering background tabs, reactivation and canvas focus. The affected three groups pass 61 tests.

Recorder fixes align with the current UI: select the full track list for cross-frame checks, use the new object's class picker instead of relabeling a selected track, and close the floating selection card before clicking a row it covers. This last ordering prevents browser actionability retries from scrolling the workspace; the final Mask clip keeps the whole canvas visible. Cleanup now drains every owned record queue before each project reseed, including the separate routing flow, and retains failed cleanup work for retry.

Validation includes 36 recording/helper tests, two derivation tests, focused TypeScript and ESLint checks, screenshot lint (61 files, none missing), the strict static-image manifest check and the documentation build. The static manifest retains three historical warnings. Final publication hashes, browser preview results and source files are retained in the verified private batch archive. This batch does not approve the new ten files, claim marketing/4K60 quality, authorize a push or release, or complete the remaining video, 3D and cross-page families.

## Batch 5: video AI ranges, seeds, and discovery

The user explicitly requested continuing to the next batch without rerecording completed content. Preserve the first four batches byte-for-byte, including the fourth batch's recordings made before the hidden-range fix in `177176ee`. That fix applies to subsequent captures; this request does not renew any media-review record.

Record five stories: `video-tracker-range`, `video-tracker-cross-frame-points`, `video-tracker-positive-negative`, `video-tracker-box-seed`, and `video-tracker-text-discovery`. Publish only their ten existing MP4/WebP targets under `docs-site/public/media/video/`. Old GIF and article recipes remain archived; do not replace earlier recordings or unrelated derivatives.

The old clips and posters show obsolete floating tracker forms. The three seed stories still drag the form across the canvas; the text story assumes fixed model-generated instance IDs. Use the current video-tracking dock layout with an expanded timeline, keeping both bus targets and the active control/result visible. Retain the actual model wait and complete seed/box gestures. Select a contiguous inspected source window; do not manufacture shorter inference or repeated frames.

All five stories require live `video_tracker` capabilities. Declare those exact flows in the existing portable recording plan, preserve job/request/preview/decision lineage, match model results to reviewed target geometry, and verify saved track IDs and geometry after reload. Range brushing must include its seed frame; multi-frame seeds must retain their target/frame/polarity; text discovery must select the actual matching instances rather than historical IDs. Register each job and each new annotation before later checks can fail; delete jobs and accepted outputs separately through the existing scoped cleanup guards before reseeding.

Use the existing isolated screenshot database, Redis DB 15, API 8010, web 3020, and a dedicated `screenshots@batch5` worker. Preflight confirmed no active capture DB sessions or nonterminal tracker jobs. Preserve unrelated queued audit records. Inspect beginnings, core interactions, endings, posters, browser/API errors, hashes, documentation and provenance before publishing a reviewable batch; keep verified private source backups outside the checkout.

### Captured and verified

The five live flows passed on the isolated screenshot stack. Range brushing produced one bus track with 21 keyframes (F0–F20); each seed variant produced two bus tracks with 31 keyframes each (F0–F30). Text discovery selected the two geometrically matched buses, accepted 22 candidates into two tracks (F0–F10), and rejected the remaining candidates. All flows verified persisted IDs and geometry after reload, with no collected browser, console, or API errors. Reloads remain outside the published windows.

| Story                    | Source run                         | Reviewed source window | Output duration |
| ------------------------ | ---------------------------------- | ---------------------- | --------------- |
| Range                    | `2026-09-08T02-18-10-093Z-4162935` | `74.8:33.16`           | 33.16 s         |
| Box seeds                | `2026-09-08T02-18-10-093Z-4162935` | `4.2:20.85`            | 20.88 s         |
| Cross-frame points       | `2026-09-08T02-29-09-027Z-6986`    | `64.95:26.25`          | 26.28 s         |
| Positive/negative points | `2026-09-08T02-29-09-027Z-6986`    | `4.1:29.55`            | 29.60 s         |
| Text discovery           | `2026-09-08T02-47-14-567Z-77508`   | `68.8:14.6`            | 14.60 s         |

Each published MP4 is standard 1280×720 at 25 fps with a separately inspected WebP poster. Every clip is contiguous and retains actual inference latency, including the longer range wait and the warm text-discovery response. Source setup and refresh-only tails were trimmed after frame inspection; no completed earlier story was rerecorded. The tracked-media baseline confirms exactly these ten replacements and 241 unchanged media files, including all four earlier batches.

The text pilot exposed a recorder actionability problem: the review card covers the usual top-edge pointer parking location, leaving playback controls hidden. The successful flow parks below the card before using native frame controls. Its two failed pilots and the earlier range locator pilot are retained privately, not published. Cleanup recovers newly committed annotations when browser responses are lost, validates the task scope before deletion, and keeps failed cleanup records for retry. The docs profile also repairs isolated seed after the run; this validation does not certify the separate marketing teardown path.

Validation: 40 recording/helper tests, two derivation tests, focused TypeScript and ESLint checks, screenshot lint (61 files, none missing), strict static-image manifest validation, and the documentation build passed. The static manifest retains its historical warnings. The local review page decoded five posters and played all five MP4s with no browser or HTTP errors. This batch remains pending human content review; it does not update `media-reviews.json`, authorize push/release, or certify marketing/4K60 capture.

## Parallel completion of remaining recordings

The user approved the fifth batch; `8a1c0a56` records its ten exact assets against `9d29c405`. The subsequent instruction explicitly assigns remaining work to `gpt-5.6-luna` workers at `max` reasoning, with the primary thread performing review. This authorizes implementation, real capture, verification, and primary-thread content review of the remaining stories; it does not authorize a push or release or claim a new human review of earlier batches.

All workers start from local `8a1c0a561067c4581d87d02330f4daf6d968f84b`. The primary thread preserves a SHA-256 baseline of 251 tracked media files and owns integration, shared-file conflict resolution, the aggregate plan, and final review records. Workers own disjoint flow modules and only their assigned entries/sections in shared registration files. Each delivers real commits, source manifests, inspected clip/poster windows, API and browser evidence, saved-state checks, and verified private source archives. Each worker uses its own disposable database, seven storage buckets, Redis container, API, worker, and web server. Shared model and X11 use is serialized with separate host locks; setup, ordinary captures, encoding, and review remain parallel.

| Worker        | Existing stories                                                                                                                                                                                                          | New story                    | Isolated runtime                                             |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------ |
| Video         | current-frame-video-inference; video-tracker-job-states; video-mask-correction-propagate; video-timeline-prediction-navigation; video-propagate-track-vs-copy; video-track-batch-propagate; video-tracker-combo-discovery | workspace-layout-persistence | `annotation_luna_video_test`, API 8031, web 3031, Redis 6391 |
| AI and review | ocr-inference; secondary-inference-attribute; ai-prediction-import; review-reject; pipeline-apply-project; jobs-retry-recovery; project-ml-routing; ai-preannotate                                                        | —                            | `annotation_luna_ai_test`, API 8032, web 3032, Redis 6392    |
| 3D            | pointcloud-billboard-label; pointcloud-controls; pointcloud-crossframe-track; pointcloud-view                                                                                                                             | pointcloud-panel-layout      | `annotation_luna_3d_test`, API 8033, web 3033, Redis 6393    |

The remaining 21 stories are content groups, not file counts. Their expected public output scope is 19 existing MP4/poster pairs, two new MP4/poster pairs, and the three existing OCR homepage encodings: 45 files. Confirm this against actual references before integration. OCR homepage and the hardware-qualified 3D flows retain the existing marketing requirements; standard capture is not an implicit fallback. Previously completed recordings and the 13 deliberately retained non-workbench flows remain outside this replacement scope.

The primary reviewer must inspect each delivered source and derivative, including the complete represented interaction, beginning, ending, and poster; verify actual target geometry, inference/decision lineage, persistence, error evidence, clip bounds, and source hashes; and request corrections where needed. Encoding or passing tests alone do not constitute approval. Any review record made under the user's delegation must identify the primary agent as reviewer rather than attributing an unseen human review to the user.

### AI and review integration

All eight assigned AI/review stories passed primary-thread content review. The final import clip was extended to make candidate selection and acceptance readable; the routing flow uses one recorded page for both project settings and workbench capability selection. Source archives, hashes, complete-video contact sheets, inspected posters, and available API evidence are retained privately.

| Story                | Verified content                                                                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| OCR                  | Real RapidOCR configuration, dispatch, running state, and 17 returned candidates; five homepage/documentation encodings share the inspected source.          |
| Secondary inference  | Crop OCR writes three AI attributes; a manual text correction clears only that field's AI origin.                                                            |
| Prediction import    | Five externally imported candidates become four pending candidates and one manual annotation after acceptance; no live inference claim.                      |
| Review rejection     | Real claim and rejection requests record the geometry issue and reason.                                                                                      |
| Pipeline application | A saved project pipeline runs detection followed by vehicle attribute classification and returns attributed shapes.                                          |
| Job recovery         | An intentionally seeded failure is retried through the real API and RapidOCR worker; the completed retry leads to visible OCR candidates.                    |
| Project routing      | Project backend configuration determines the single-task model and interactive tool backend; the recording demonstrates routing without executing inference. |
| Preannotation        | Real asynchronous detection completes and returns visible candidates.                                                                                        |

The OCR hardware source measures 2592 × 1458 pixels with 58.17 unique frames per second and is resampled to a 3840 × 2160 master; it is not native 4K capture. The marketing manifest does not carry an inference receipt, so its derivative metadata retains `unverified`; the separate review evidence records the successful real-backend assertions and visible result. This limitation is not repaired by inventing receipt data.

These are agent reviews under the user's explicit delegation, not newly attributed human approvals. Integration preserves previously delivered media and does not complete the remaining video and cross-frame 3D stories or authorize a push or release.

### Video propagation integration

The primary thread reviewed the complete geometry-copy comparison, batch propagation, and combination-discovery recordings and their posters. The copy story shows fixed geometry across frames, undo, then real AI tracking of the moving truck. Batch propagation and combination discovery retain the actual inference wait and visible results. Only leading setup/loading frames were removed; final source windows are `5:35.48`, `6.5:57.3`, and `5.5:64.46` respectively, from the clean `3adad68b` capture source. Exact exported first frames were rechecked after trimming.

The combined recording helpers pass 41 tests, and media derivation passes two tests. The optional existing seed-repair skip flag now also avoids the duplicate per-flow reset after an isolated catalog has already been repaired; catalog loading and flow requirements remain active. The mask flow's native frame navigation update is included, while its final media remains subject to separate content review.

### Point-cloud integration

All five point-cloud stories passed primary-thread review: billboard labels, controls, orbit, panel layout, and cross-frame continuation. The new panel story demonstrates three-view and camera panels through docking, floating, hiding, restoration, and a scene-frame change. The cross-frame story propagates one real 3D box twice, saves a 0.2 m center correction in the middle frame, and returns through the same track. Per-frame pointer double-click focusing keeps each stable target completely visible; neighboring references remain faint beneath the selected solid box.

The final cross-frame source is the clean `80f02347` run `20260908T083414Z-80f023476054`; its 30.634-second derivative and poster were inspected across the complete interaction. Hardware cadence measured 59.07 unique frames per second. The 2592 × 1458 capture was resampled to a 3840 × 2160 master, so it is not native 4K capture. This is deterministic continuation using scene poses, with no AI-tracking claim. The live capture test passed and its raw/master hashes were verified against a separate private archive.

### Final mask correction and completed content review

The final mask recording starts on F5 while holding F0 geometry, opens the editor through a physically reachable button, adds and erases brush strokes, saves a correction frame, waits for real SAM3 propagation, reviews F18 and F10, and accepts the result. The reviewer rejected the earlier upper-background spill and the remaining outer-right fringe. The final script reads the saved F5 RLE and checks its bounds and distinct content hash in addition to the manual/prediction keyframe checks.

The clean `e3f10312` source run `2026-09-08T09-03-00-000Z-mask-correction-final` supplies the contiguous `12:26.3` window. The 26.32-second public MP4, exact first frame, complete interaction, and result poster at source second 37 passed primary-thread review. F0 setup is outside the public window; the actual inference wait remains intact.

All 21 assigned stories now have completed content review covering 45 public files: 41 replacements and four new layout assets. The other 210 files in the original 251-file media baseline remain byte-for-byte unchanged. Source commits remain reachable through integration, and private source archives have verified hashes. The integrated recording/helper suite passes 41 tests and media derivation passes two tests. Review registry entries are recorded separately after the integrated assets are committed, identifying the delegated primary agent as reviewer.

### Final registry, preview, and cleanup

The committed integration `2345bbc4` is the review anchor for exactly these 45 assets, with reviewer `Codex primary thread (user-delegated)`. All 45 classify as current with zero provenance issues. The full-site audit reports 0 broken, 115 stale, 1 review due, and 95 current; unrelated historical review records were not renewed. This completion does not claim a new human review of earlier batches or the excluded media.

The local `/latest` gallery lists all 21 reviewed stories. HTTP readback matched all 45 file hashes, cache headers prevent stale previews, and MP4 byte-range requests returned 206 correctly. Content review used complete-video sampling and full-resolution key frames/posters; a fresh in-app browser playback session was unavailable. The earlier documentation build remains the build evidence for the affected guide integration.

All three worker-owned API/web/Celery stacks, Redis containers, disposable databases, and their 21 precisely scoped storage buckets were removed after idle checks. Temporary worker worktrees were removed after verifying integration and private source backups. Shared application/model/storage services and the review preview remain available; source branches and the accepted private capture archives are retained.

## Outcome

- Landed commits: the staged batches culminated in integration and review anchor `2345bbc4`; earlier accepted batches remain identified in their completion records above.
- Release milestone: the shipped user-visible media refresh is recorded in the `0.25.x` changelog history.
- User documentation: `docs-site/user-guide/workbench/` and the affected workflow guides now reference the current videos, posters, and layout teaching assets.
- Developer documentation: `docs-site/dev/how-to/update-screenshots.md` and the maintained media manifests/review registry describe the reproducible capture, derivation, and review workflow.
- CHANGELOG: the media refresh entries are archived in `docs/changelogs/0.25.x.md`.
- Remaining work: none within this plan. Unrelated stale or review-due assets and any future marketing-master proposals remain governed by their own active plans.

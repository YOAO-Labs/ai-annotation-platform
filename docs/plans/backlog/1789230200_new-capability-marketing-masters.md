# New capability marketing masters

> Status: research-draft; new stories are unscheduled. Mac recording capability is integrated; none of these stories has been enrolled, captured, published or backed up by this plan.
> Baseline: local `52ad4497`, inspected on 2026-09-13. Priority selected by the maintainer: recent capability demonstrations reusable in documentation, the website, and presentations.

## Recommendation

Produce eight independent masters first, followed by four collaboration and team-management stories. Each asset demonstrates one observable result. Reuse the existing recorder, catalog, seed fixtures, and media derivation pipeline. Keep one UI theme per story; additional light/dark variants do not count as new assets.

The smallest useful delivery is the first two stories: named layout presets and polygon boundary tracing. Each subsequent story can ship independently with its own evidence, documentation placement, and derivatives. Completing all twelve is not a prerequisite for publishing a useful batch.

## Verified inventory and existing gaps

| Evidence                                    | Observed state                                                                                                      | Planning consequence                                                                                                                                                       |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_helpers/marketing-assets.ts`              | 65 declared asset specifications                                                                                    | A registered specification is not proof of a captured or qualified master.                                                                                                 |
| Cloud `AAP资产:营销素材/高清母版/current/`  | 63 asset entries; 63 MKV sources and 63 MP4 masters; all 126 manifest paths and byte sizes match the remote listing | Extend this library without replacing it with a partial batch. This check did not download videos, recompute hashes, or renew visual reviews.                              |
| Cloud manifest                              | `current-20260908T094417Z-fd38c7282b41`; historical labels: 56 approved, 7 pending                                  | These labels are historical evidence, not a current-content acceptance result.                                                                                             |
| Declared assets absent from cloud `current` | `workspace-layout-basics`, `workspace-layout-persistence`                                                           | Separate archive/capture reconciliation items; neither is a new proposed story. No local marketing archive was present in this checkout or the primary checkout inspected. |
| Published asset catalog                     | Missing the two workspace IDs above and `pointcloud-panel-layout`                                                   | Repair catalog coverage during implementation, without describing missing masters as available.                                                                            |
| Recording backlog                           | All five existing batches say recorded                                                                              | Preserve those rows as history; put new proposed work in this plan.                                                                                                        |
| Repository media audit from this session    | 211 referenced assets: 0 broken, 182 stale, 1 review due, 28 current                                                | Validate the new asset scope and report the existing library debt separately.                                                                                              |

Existing SAM/OCR, candidate review, video tracking, basic drawing, point-cloud workflows, large-image handling, project creation, and model operations already have substantial coverage. Repeating their current storyboards would add less value than the proposed interactions.

## First batch: eight new stories

Durations below are proposed **minimum / target / maximum seconds**, measured over the meaningful interaction. They are not permission to add pauses or slow playback. All eight use real application/API behavior with inference requirement **none**.

| Order / asset ID              | Result and four-shot storyboard                                                                                                                                                                                                                                                                                             | Seconds      | Documentation destination and output stem                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------- |
| 1. `workspace-named-preset`   | Start with a selected object and a custom panel arrangement → save it as “边界精修” → apply standard layout, then apply the saved preset → undo the application and show the previous arrangement with the same task and selection. Verify account persistence separately after reload, before introducing an unsent draft. | 18 / 26 / 38 | `user-guide/workbench/index.md`, named presets section; `workbench/named-preset`                                 |
| 2. `polygon-boundary-trace`   | Show two adjacent regions and one saved source Polygon → choose two points on its boundary → compare the two directions and confirm one path → complete and save the neighboring Polygon with a shared boundary. The source remains unchanged.                                                                              | 16 / 24 / 36 | `user-guide/workbench/polygon.md`, 沿已有边界追踪; `polygon/boundary-trace`                                      |
| 3. `polygon-slice-undo`       | Select one saved, unlocked simple Polygon → draw a crossing polyline → inspect the two-region preview and confirm → undo and redo once to show the same two saved objects return.                                                                                                                                           | 18 / 26 / 38 | `user-guide/workbench/polygon.md`, 切割为两个对象; `polygon/slice-undo`                                          |
| 4. `mask-slice-instances`     | Select a saved Raster Mask covering adjacent instances → enter the straight-line split tool and drag the line → inspect two colored regions and pixel counts → commit two instances and select each result. Verify undo/redo outside the hero clip.                                                                         | 16 / 24 / 36 | `user-guide/workbench/mask-brush.md`, 直线切割为两个实例; `mask/slice-instances`                                 |
| 5. `discussion-target-drafts` | Type “整体边界待复核” with current task as recipient → select a saved object and type a different draft → change only the reading scope → return between recipients and show each original draft in its own input. End by sending the task draft and reading it back under the task source label.                           | 18 / 28 / 40 | `user-guide/workbench/discussion.md`, 不选标注也能留言; `discussion/target-drafts`                               |
| 6. `task-drawing-comment`     | Start with no selected annotation and an unannotated region → choose “在题图上绘制” → circle the missing object and attach the drawing → send with no text and open the posted drawing. Saved annotation geometry/count remains unchanged.                                                                                  | 14 / 22 / 32 | `user-guide/workbench/discussion.md`, image task drawing instructions; `discussion/task-drawing`                 |
| 7. `video-issue-resolution`   | Open a seeded issue title while viewing another frame, showing that reading leaves the frame unchanged → choose “定位” and wait for the actual target frame/object → reply and resolve → keep reading the resolved detail, then open the next unresolved issue.                                                             | 22 / 32 / 46 | `user-guide/workbench/discussion.md`, 打开问题与回复 and 连续处理未解决问题; `discussion/video-issue-resolution` |
| 8. `project-guide-autosave`   | Open an existing short guide in the visual editor → edit a boundary rule in its table → show the real saving/saved status without clicking Save → open Preview, then reload and show the saved rule.                                                                                                                        | 16 / 24 / 36 | `user-guide/projects/index.md`, 编写标注指引; `projects/guide-autosave`                                          |

Output stems resolve to `docs-site/public/media/<stem>.mp4` and `<stem>-poster.webp`. Each story gets one canonical `DocsVideo` placement; related pages link to that explanation instead of repeating the same player. The polygon page will place tracing and splitting beside their relevant instructions, not stack them in an opening gallery.

## Second batch: four independent extensions

| Asset ID                    | Result and shot sequence                                                                                                                                                                                                                         | Seconds      | Destination / output stem                                                                      |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ | ---------------------------------------------------------------------------------------------- |
| `video-comment-badges`      | Show a commented trajectory and a single-frame annotation → move between two known frames → click the visible object's badge → show the correct object discussion. Demonstrate total comment count, not unread count.                            | 14 / 22 / 32 | `user-guide/workbench/discussion.md`, 画布上的评论提示; `discussion/video-comment-badges`      |
| `discussion-mention-return` | Select an isolated project member through the real `@` picker → send a task comment → switch to that member's isolated browser context → open its notification and highlight the original comment.                                               | 18 / 28 / 42 | `user-guide/reference/notifications.md`, 讨论通知; `discussion/mention-return`                 |
| `project-invitation-join`   | Choose an existing isolated user's email, role, and target project → create the invitation → accept through that account's browser context → show project membership and the honest “waiting for assignment” result. Use no email-delivery step. | 22 / 32 / 46 | `user-guide/superadmin/user-management.md`, 邀请新用户; `team/project-invitation-join`         |
| `batch-assignment-preview`  | Select one small batch and a new assignee → inspect current/target responsibilities and added/existing workload → confirm the real preview → read back the resulting batch owners. Workload numbers mean task counts, not estimated hours.       | 18 / 28 / 40 | `user-guide/projects/batch.md`, assignment preview instructions; `projects/assignment-preview` |

These flows also need no ML inference. They add two-account, video, or role/permission setup, so they follow the first batch. Use only disposable local identities; no actual colleague notifications or external email delivery belong in a capture. Keep invitation token fields outside the published view and revoke owned invitations during cleanup.

## Composition and reuse

- Use the existing 16:9 logical composition, one Chinese UI language, and one dark UI variant per new story. Show enough surrounding controls to identify the action and keep the final state readable at the documentation player width. Use `applyScreenshotTheme`; do not add a second theme implementation.
- Keep each source free of burned-in captions, arrows, music, and branding. Explain the operation through the page caption or presentation text. Use the real pointer/tool preview and real, correctly aligned geometry.
- Begin with a ready, recognizable task or settings section. Setup/login/loading outside the meaningful action can be trimmed. Preserve preview → confirmation → real result, including an actual saving state where it proves the capability.
- Choose the poster from the completed result or a clear preview: shared polygon boundary, two split regions, recovered layout, posted drawing, located issue, or saved guide. Determine its exact timestamp from the accepted master; do not copy timestamps from unrelated clips.
- First-batch delivery is **8 stories, 16 private video files, and 16 public derivatives**: one archived source + one universal MP4, and one documentation MP4 + one WebP poster per story. The second batch adds 4 stories with the same contract. Do not multiply this count by theme or output format.
- The masters remain suitable for later website excerpts and presentations. Add a homepage derivative only when it has a concrete placement; no speculative GIF, vertical-video, mobile-theme, or promotional montage matrix is needed now.

## Implementation evidence and reuse

Paths below are existing sources, not proof that the proposed flows have run.

| Stories                             | Existing behavior and verification references                                                                                                                                                           | Capture adaptation                                                                                                                                                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Named presets                       | `apps/web/src/pages/Workbench/layout/WorkbenchLayoutSettings.tsx`; `apps/web/e2e/tests/workbench-layout.spec.ts` named-preset and undo cases                                                            | Reuse panel action helpers and the real preference read/write/restoration pattern in `flows/workspace-layout-persistence.ts`. Do not use the GET/PATCH sandbox in `_workbench-layout.ts` to claim account persistence.    |
| Polygon tracing                     | `stage/usePolygonBoundaryTrace.ts`; `apps/web/e2e/tests/polygon-boundary-trace.spec.ts`; polygon guide                                                                                                  | Adapt source geometry and stage-coordinate helpers to the screenshot catalog. Read back both saved objects and confirm the source version/geometry is unchanged.                                                          |
| Polygon split                       | `stage/usePolygonSlice.ts`; `apps/web/e2e/tests/polygon-slice.spec.ts` preview, restore, conflict, and lost-response cases                                                                              | Reuse the real commit/restore API sequence and geometry assertions. Verify one original ID, one new ID, retained attributes, and no duplicated area.                                                                      |
| Raster Mask split                   | `apps/web/e2e/tests/mask-slice.spec.ts`, especially `seedMask`, `checkPartition`, and restore coverage; mask guide                                                                                      | Use a real saved mask from the isolated catalog and mask-content API. Check disjoint pixels whose union equals the source, retained identity, and persisted result. Do not replace raster splitting with polygon footage. |
| Task/annotation drafts and drawing  | `shell/CommentsPanel.tsx`, `shell/CommentInput.tsx`, `state/useDiscussionDraftStore.ts`; `apps/web/e2e/tests/workbench-discussion.spec.ts`; discussion guide                                            | Use current comment targets, exact owned comment IDs, and actual drawing attachment storage. Session draft recovery does not promise text survives reload.                                                                |
| Issue resolution and video comments | `shell/DiscussionIssueDetail.tsx`, `stage/VideoStageCommentBadges.tsx`; `apps/web/e2e/tests/video-issue-context.spec.ts`, `workbench-discussion.spec.ts`, and badge component tests                     | Reuse real video frame readiness and issue context fixtures. Wait for the rendered target frame, not only a changed frame counter.                                                                                        |
| Guide autosave                      | `apps/web/src/pages/Projects/sections/AnnotationGuideSection.tsx`; `apps/web/e2e/tests/guide-autosave.spec.ts` and `guide-experience.spec.ts`                                                           | Reuse `markdown-editor` / `guide-save-status`, real project PATCH/GET, and reload readback. Restore only the owned project's original guide.                                                                              |
| Mentions and management             | `workbench-discussion.spec.ts`; `apps/web/src/components/users/InviteUserModal.tsx` and its tests; `apps/web/src/components/projects/BatchAssignmentModal.tsx` and its tests; corresponding user guides | Reuse isolated account contexts, invitation acceptance, and preview/confirm APIs. Add real capture-path checks where existing coverage is component-only.                                                                 |

Unqualified `stage/`, `shell/`, and `state/` paths above are under `apps/web/src/pages/Workbench/`; `flows/` paths are under `apps/web/e2e/screenshots/`.

Implementation effort is concentrated in capture automation and review: six first-batch stories can adapt direct real-browser behavior checks; two need additional browser-path proof. The second batch adds four such proof paths. No application feature work is assumed.

- **Strong source/test evidence:** named presets, polygon tracing, both split operations, separate recipient drafts, and guide autosave have direct E2E cases for their core result. These tests were inspected, not executed in this planning task.
- **Medium confidence until capture validation:** drawing-only task comments are implemented in `CommentsPanel.tsx` and covered at component level, but the current discussion E2E covers task text and annotation popup drawings separately. The new flow must prove unselected task → canvas drawing → posted comment and unchanged annotations.
- **Medium confidence until capture validation:** `video-issue-frame.spec.ts` covers video location; `workbench-discussion.spec.ts` covers reply/resolve/next behavior. The proposed video flow must verify those steps together on the same real video issue.
- **Second-batch gaps:** video badges have component/rendering tests; the existing mention E2E creates the mention through an API rather than the actual picker; invitation creation/acceptance and single-batch assignment preview have component tests. Add a real browser capture path for each. The assignment story intentionally uses one batch, matching `BatchAssignmentModal.tsx`; project-wide distribution is a separate, larger story and is not included.

## Capture and archive contract

The maintainer clarified that platform requirements may be relaxed, **not quality requirements**. Non-ML operations may be captured on this Apple M4 Pro Mac when they meet the same visual, geometry, frame-pacing, and provenance checks. The existing `docs` profile is a functional rehearsal path; its 1440 × 810 / 25 fps video is not accepted as a replacement for a high-quality master.

Retain **3840 × 2160 / 60 fps** delivery, hardware browser compositing, the 120-frame rAF probe with p95 interval at most 20 ms, and the 1.1-second capture calibration with at least 58 sampled frames, effective unique frame rate at least 55 fps, and unique-frame ratio at least 90%. Do not pad durations, manufacture motion by repeating frames, or weaken those gates merely to admit macOS.

The existing Linux composition uses logical **1440 × 810**, DPR **1.8**, and source pixels **2592 × 1458**, resampled using Lanczos to 4K. A Mac native content capture of **2880 × 1620** preserves at least that source detail. Record actual logical viewport, browser DPR, capture dimensions, crop, and resampling separately; neither route is native 4K capture. The X11 desktop minimum of **2700 × 1750**, `xrandr`, and NVIDIA encoding are Linux implementation requirements rather than universal quality requirements.

The maintained `screenshots:record --profile marketing` command now selects ScreenCaptureKit on macOS for explicitly registered non-ML flows. It preserves the existing flow, archive, and derivative contracts. This capability integration does not enroll or schedule the twelve proposed stories.

Verify the serving checkout, `.env` symlink target, disposable capture database, object-storage scope, nonzero isolated Redis DB, and API/worker agreement before fixture mutations. Linux recording also checks its screenshot worker. The explicitly non-ML Mac selection checks API/database health without requiring an unused ML worker; manual stories retain runtime-isolation requirements. Reuse the existing runtime workflow, read back actual ports, and keep the recording clock live. `--list` and `--plan` are read-only; `--validate-only` seeds and mutates capture fixtures.

For each story, enroll the specification and independent flow through:

1. `apps/web/e2e/screenshots/_helpers/marketing-assets.ts`: title, narrative theme, objective, duration bounds, shots, and editing notes.
2. `apps/web/e2e/screenshots/flows/<asset-id>.ts` and `flows/flows.spec.ts`: one story, stable readiness checks, real result readback, and cleanup of only owned resources in success and failure paths.
3. `apps/web/e2e/screenshots/recording-plan.mjs`: explicit selection and inference requirement `none`. Proposed IDs must not be presented as runnable until registered.
4. `apps/web/scripts/derive-doc-media.mjs`: output stem and visually selected poster frame. Reuse shared derivation. Mac support reuses the local native capture helper and platform/driver checks; no new processing framework, package, or service is needed.
5. Canonical guide, `marketing-asset-catalog.md`, and recording backlog: link the accepted derivatives and keep recorded, archived, qualified, and reviewed evidence distinct. A complete eight-asset implementation affects more than eight files; deliver per story or small independent batches.

Archive each run immutably under `.artifacts/marketing/<run-id>/`, with `manifest.json`, `raw/<asset-id>/<sha>.mkv`, and `masters/<asset-id>/<sha>.mp4`. Both video roles preserve the meaningful action window. The recorder's temporary untrimmed capture is not an additional retained archive promised by this plan.

For a requested cloud backup, retain the local sources until remote verification succeeds. The current remote is a **consolidated latest-per-asset library**, not a single partial run: copy new hash-named files without deleting existing asset directories; merge only their entries and per-asset `source_runs` into a candidate current manifest, preserving original capture provenance. Verify remote files before replacing that manifest. Keep the previous manifest locally for rollback. Never sync an eight-asset run directly over the 63-asset `current` root. If backup is interrupted, keep the old manifest active and retry only the missing new files. Cloud writes are not part of this planning task.

## Verification and completion

After enrollment, use the existing commands for each selected asset; `workspace-named-preset` is the concrete first story below. The run variable is the actual immutable run directory reported by capture.

```sh
rtk proxy pnpm --filter @anno/web screenshots:record -- --list
rtk proxy pnpm --filter @anno/web screenshots:record -- --flow workspace-named-preset --profile marketing --plan
rtk proxy pnpm --filter @anno/web screenshots:record -- --flow workspace-named-preset --profile docs --validate-only
rtk proxy pnpm --filter @anno/web screenshots:record -- --flow workspace-named-preset --profile marketing
rtk proxy pnpm docs:media:derive -- --run "$capture_run" --asset workspace-named-preset
rtk proxy pnpm docs:media:audit
rtk proxy pnpm docs:build
rtk git diff --check
```

The validation and recording commands require the verified disposable runtime. The maintained marketing command also supports Mac for explicitly selected flows with no ML capabilities or inference. During this planning task, only the existing flow listing and an existing `workspace-layout-persistence` / `polygon-draw` selection plan were exercised. No browser tests, seeding, or captures were run for these proposed IDs.

Acceptance for each delivered story:

- Inspect the beginning, core interaction, ending, and poster. Confirm legible Chinese text, no loading placeholders, truthful target geometry/frame/recipient, no secrets, and correct final state.
- Verify the real persisted outcome. Preserve the relevant existing negative-path coverage: invalid/locked split input, stale source, save/send failure retaining drafts, missing discussion permission, and stale assignment preview. Run affected existing cases in the isolated test environment; do not build another broad test framework or put every failure into the hero clip.
- Record source commit and dirty state honestly, seed revision, capture facts/cadence, clip bounds, source and derivative hashes, and inference `none`. Derived files must match the accepted archive.
- View each new canonical player at desktop and narrow width, in both document themes. One video remains visible; poster, caption, controls, and focus remain usable. Theme QA does not require two recordings.
- Update media reviews only after the required completed visual/content review and committed clean-tree prerequisites. An encoding pass or sampled agent review cannot refresh unrelated human review records. Report stale-library counts separately from the accepted new scope.
- Complete the requested Git/publication/backup scope only when authorized. A rollback removes the new guide embeds and derivative registrations; retained private masters and preexisting user data are unaffected.

## Planning outcome

The deliverable is this prioritized twelve-story plan and the backlog pointer. Inventory paths and sizes were checked remotely; existing feature documentation, implementation owners, and relevant test sources were inspected. The initial planning pass produced no media or runtime changes. The subsequently authorized Mac trial uses an isolated E2E runtime and private sample assets; its outcome is recorded below. No review approval, commit, or upload is implied.

## Mac pilot outcome

The maintainer authorized one non-ML trial and clarified that only environment requirements may be relaxed. The existing `hotkey-cheatsheet` flow first passed its ordinary browser test on macOS, but the resulting 1440 × 810 / 25 fps recording was explicitly rejected as master-quality evidence.

A private ScreenCaptureKit + AVAssetWriter prototype then recorded the real isolated image workbench: open keyboard shortcuts, search “采纳”, search “视频”, clear, and close. The selected color-managed 60 Hz trial uses the existing high-quality H.264 universal-output settings (CRF 16, High profile, level 5.1), with an 8-second operation window and no calibration frames in the preview.

| Check                       | Selected trial result                                    | Existing requirement                                     |
| --------------------------- | -------------------------------------------------------- | -------------------------------------------------------- |
| Source content              | 2880 × 1620, cropped from a 2880 × 1906 window           | Preserve at least the existing 2592 × 1458 source detail |
| Preview                     | 3840 × 2160, 60 fps, 480 frames / 8 seconds              | 4K UHD / 60 fps                                          |
| Browser rendering           | Apple M4 Pro / ANGLE Metal; hardware compositing enabled | Hardware compositing                                     |
| rAF p95                     | 17.4 ms                                                  | At most 20 ms                                            |
| Capture calibration         | 63 unique frames / 66 sampled over 1.1 seconds           | At least 58 sampled frames                               |
| Effective unique frame rate | 57.27 fps                                                | At least 55 fps                                          |
| Unique-frame ratio          | 95.45%                                                   | At least 90%                                             |
| Independent cross-check     | 63 distinct consecutive calibration-color states         | Avoid counting compression noise as useful motion        |
| Color metadata              | BT.709; explicit sRGB capture conversion                 | Preserve readable UI and consistent SDR colors           |
| Inference                   | None; isolated catalog backend requirements empty        | Non-ML pilot scope                                       |

Local evidence is under `.artifacts/recordings/mac-hotkeys-hq-trial/`: `window.mp4`, `hotkey-cheatsheet-4k60.mp4`, `cadence.json`, `events.json`, and `trial-evidence.json`. Prototype sources are under `.artifacts/recordings/mac-smoke-support/`. These are private ignored artifacts, not a registered marketing archive or a human media approval. Native window capture is variable-rate; the preview is normalized to 60 fps using the existing export approach, and independent capture cadence is measured separately. The default macOS recording utility and a 120 Hz capture experiment were not selected; neither is used to justify acceptance.

The pilot demonstrates that this Mac can capture this non-ML operation at the retained quality thresholds. It does not certify every workflow or sustained batch stability. The owned browser and API/Web runtime were stopped; isolated database and object data are retained. No cloud upload or commit was made.

### Maintained integration scope

The maintainer requested capability integration first and deferred actual asset recording. The implementation reuses existing asset specs, application interactions, fixture cleanup, trimming, hashing, and publication. Completed integration:

1. Added a macOS native capture helper for window-scoped ScreenCaptureKit frames and H.264 writing. Emit first-frame timing, source geometry, color settings, and capture failures; ensure the target window is full size before calibration and exclude browser chrome from the asset crop.
2. Extended `apps/web/scripts/run-recording-capture.mjs` and `run-marketing-capture.mjs` to admit the Mac driver for selections with backend requirements **none** and inference **none**, using macOS preflight instead of X11/NVIDIA tools. Retain runtime isolation.
3. Extended `apps/web/e2e/screenshots/_helpers/marketing-external-recorder.ts` for native window identification, readiness/start/stop, geometry, and cadence; adapt `apps/web/playwright.screenshots.config.ts` so the Mac browser uses Metal rather than Linux-specific GL launch arguments.
4. Added the native driver to `marketing-recorder.ts` provenance types and to the explicit driver allowlist in `apps/web/scripts/media-derivation.mjs`. Preserve the same output geometry, frame-rate, and unique-frame validation.
5. Added focused driver, protocol, geometry, cadence, Mac trim/archive, black-frame rejection, and derivation regression cases. A backend-free synthetic page exercises the maintained native recorder; no new business flow was recorded during integration.

The native helper requires macOS 14+, Xcode Command Line Tools, Screen Recording permission for the launching terminal/agent, and a display that provides the qualified Retina content dimensions. It compiles into the ignored local artifact cache. Startup restores a hidden Chromium window before measuring its PID/title-matched native bounds. The helper reports the first native-frame host timestamp, preserves the final static interval, and stops on `q`, stdin EOF, or termination signals. It rejects window size changes during capture.

Verification includes 42 recording-selection tests, 26 marketing recorder/archive tests, and two derivation tests. The synthetic native check measured a 2880 × 1620 content crop and produced 3840 × 2160 at 60 fps. With continuous native bounds checking enabled, its calibration had 63 unique frames out of 66 over 1108.4 ms: 56.84 effective fps and 95.45% uniqueness. An earlier native sample failed the retained cadence gate and was discarded. A second synthetic check resized the window after readiness and confirmed that native capture failed instead of archiving scaled content. These checks qualify the integration path, not sustained batch performance or the twelve new stories; every future run must pass the same gates.

The twelve stories remain unregistered and unscheduled. No new business assets, publication files, media approvals, cloud uploads, commits, or pushes were produced by capability integration.

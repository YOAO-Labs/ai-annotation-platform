# Documentation media refresh — macOS-capturable scope

> Status: static matrix and all Mac-recordable workflow recordings executed and approved (sixteen of seventeen flows); S3 pointcloud canary and the Linux/GPU batch remain. See `## Outcome` and `## Remaining work` at the end of this file.
> Maintainer decisions (2026-09-13): refresh the existing library only; re-record only what this Apple Silicon Mac can record; agent-delegated visual review is authorized and must be recorded honestly.
> The twelve proposed marketing masters remain in [new capability marketing masters](1789230200_new-capability-marketing-masters.md) and are not enrolled here.

## 1. Audit-gate baseline

`pnpm docs:media:audit` at `b1dd26a4`, 211 referenced files:

| Broken | Stale | Review due | Current | Provenance warnings | Tier A open |
| -----: | ----: | ---------: | ------: | ------------------: | ----------: |
|      0 |   202 |          0 |       9 |                 160 |           0 |

- The integrity gate (`--integrity`, used by ordinary PRs) passes: no missing files and no manifest-hash mismatch.
- `--strict` / `--release` fail: 202 stale reviews plus 160 provenance warnings (140 captures recorded on a dirty worktree; 77 assets captured under seed `screenshots-2026-08-e`/`-f` instead of the current `-g`).
- Stale composition (mutually exclusive): 16 assets whose file content changed after review; 144 affected by capture/flow/seed/script churn; 42 only by documentation-page edits; every stale asset differs from its review commit on at least one watched path.
- Static screenshots genuinely lag. `manifest.json` was last rebuilt 2026-08-15 (`435b32ac`, seed `e`, dirty tree) — before the filtering rollout, the comments/@-mention features, and the documentation redesign. `check-image-manifest --release` fails on the seed warning and on two registered scenes that were never produced (`sam/ai-inspector-panel`, `workbench/video-ai-tracking-panel`).
- Flow media are newer (133/150 entries generated after 2026-09-01), but 36 of the 55 Mac-recordable assets still carry dirty-capture provenance, and review watch paths moved for essentially all assets.
- Four review-registry entries point at retired GIFs that are no longer referenced (`bbox/draw-in-progress.gif`, `sam/*-interaction.gif`).

Regenerate these numbers with `pnpm docs:media:audit -- --json --output <path>`; do not copy them into user-facing documentation.

## 2. Capability boundary

Verified on this Mac with `screenshots:record -- --list/--plan` and `--plan --profile marketing`:

| Class                                           | Referenced assets | This Mac                                                                                  | Action in this plan                           |
| ----------------------------------------------- | ----------------: | ----------------------------------------------------------------------------------------- | --------------------------------------------- |
| Static screenshots (all referenced docs images) |                61 | Yes; AI scenes use the running `screenshot-ml-stub`                                       | Full matrix re-capture                        |
| Home hero images derived from statics           |                 4 | Yes                                                                                       | Regenerate after statics                      |
| Docs-profile non-ML flows — dirty or old seed   |                34 | Yes                                                                                       | Re-record (17 flows × 2)                      |
| Docs-profile non-ML flows — clean provenance    |                 9 | Yes                                                                                       | Visual review only                            |
| Pointcloud flows (marketing-only, no ML)        |                12 | Yes, subject to 4K60 / independent-frame gates                                            | Canary re-record 2; review 10; defer if gated |
| ML flows and legacy Linux marketing suite       |                91 | No (`image_interactive` / `ocr` / `video_tracker` / live inference; legacy orchestration) | Explicit Linux/GPU follow-up                  |

Re-record reasons: dirty capture tree, stale seed, or content replaced after review. Review-only means re-inspection and approval without new capture when content matches the current product.

## 3. Working policy

- Re-record when any holds: file content changed after the recorded review; seed is not `screenshots-2026-08-g`; capture provenance is dirty; the asset visibly predates a shipped UI iteration (for example the filtering rollout at `b6ba19c7`/`50178fe9`).
- Review-only when content is current and provenance is clean. Do not approve deferred assets merely to clear flags; they are replaced in the Linux/GPU follow-up.
- Never rewrite provenance flags, hashes, or review records to silence the audit. A clean provenance claim requires an actual clean-tree capture.
- Approve only on a clean tree with committed assets, after per-asset inspection, and only for assets covered by a completed review. Keep private capture archives under `.artifacts/` until their backup is verified; backup is out of scope.
- Agent-delegated review runs follow the existing record convention: set `GITHUB_ACTOR="Codex primary thread (user-delegated)"` for `docs:media:approve`; the maintainer authorization above is the recorded basis. Per-asset checks: asset identity and path, loading states, real target geometry, sensitive-data masking, no placeholder content, and for video the beginning, core interaction, ending, and poster.
- Freeze documentation content before capture: watched paths include Markdown pages and capture sources, so any commit touching them after approval makes the approval stale again. If a later page edit is necessary, re-approve the affected assets.

## 4. Stages

### S0 — Freeze and runtime

1. Land any pending documentation or source edits first; record the frozen commit and confirm a clean tree.
2. Provision the isolated capture runtime per [update-screenshots.md](../../docs-site/dev/how-to/update-screenshots.md): dedicated `annotation_screenshots_test` database, explicit nonzero capture Redis DB for both `REDIS_URL` and `CELERY_BROKER_URL`, API on `8010` with `E2E_SEED_ENABLED=true`, one `screenshots@` worker, web on `3001`, offline seed with `--ml-backend-mode stub` (the `screenshot-ml-stub` service is already healthy on `9100`).
3. Save the pre-capture audit JSON as baseline evidence: `pnpm docs:media:audit -- --json --output .artifacts/media-refresh/baseline.json`.
4. Smoke-check the selected flows read-only: `pnpm --filter @anno/web screenshots:record -- --flow bbox-draw --plan`.

### S1 — Static screenshot matrix

1. Run the full matrix on the frozen clean tree (all three projects must succeed before the reporter rebuilds `manifest.json`): `pnpm --filter @anno/web screenshots:matrix`.
2. Reconcile the two registered-but-unproduced scenes. Prefer embedding the produced image where the panel is already documented; if the panel is no longer worth documenting, retire the scene and target instead. Either way `check-image-manifest` strict/release must be clean. This placement choice needs a maintainer confirmation at execution time.
3. Regenerate hero derivatives from the refreshed sources: `pnpm --filter @anno/docs-site media:home-hero` (no `--asset` processes all four cards).
4. Verify: `pnpm --filter @anno/web screenshots:lint`, `node docs-site/scripts/check-image-manifest.mjs --strict`.
5. Review every refreshed static image plus the hero WebPs (content, loading state, theme variants, masking), then commit the assets and manifests.
6. Approve the batch: `pnpm docs:media:approve -- --asset <paths>`.

### S2 — Docs-profile flow re-records

Seventeen flows, each producing one poster and one documentation video: `bbox-draw`, `rotated-bbox`, `polyline-draw`, `polygon-draw`, `mask-draw`, `workspace-layout-basics`, `batch-bulk-actions`, `video-track`, `video-timeline-zoom`, `video-chapter`, `video-track-carryover`, `video-mask-track-edit`, `video-draw`, `large-image-progressive`, `large-image-pyramid-recovery`, `large-image-mask-limit`, `hotkey-cheatsheet`.

Per flow:

1. Record the standard source into the isolated runtime: `pnpm --filter @anno/web screenshots:record -- --flow <id>` (writes `.artifacts/recordings/<run-id>/`).
2. Inspect the recording: beginning, core interaction, ending, cleanup result, real geometry, no secrets, no loading placeholders. The recorder must not be treated as accepted until this inspection passes.
3. Derive the published assets with the reviewed clip window, for example:
   `pnpm docs:media:derive -- --quality standard --run <run-id> --asset <id> --format video --clip <start:duration>`.
4. Commit the assets and `flow-manifest.json`; batch-approve after review.

Also review-only (no re-capture) for the clean docs-profile assets: `ai-prediction-import`, `review-reject`, `video-tracker-job-states`, `workspace-layout-persistence`, and the `e2e-quickstart` GIF. Re-record these if the inspection finds outdated UI; note `e2e-quickstart` is not registered for `screenshots:record` and uses the legacy flows project (`screenshots:flows`) instead.

### S3 — Pointcloud marketing masters

1. Canary: re-record `pointcloud-camera-seed-3d-box` at `--profile marketing` (native ScreenCaptureKit). The run must pass 4K60, cadence, independent-frame, and archive gates.
2. If the canary passes, derive and review; if a gate fails, stop and defer the re-record with the failed evidence.
3. Review-only for the clean pointcloud assets: `pointcloud-controls`, `pointcloud-view`, `pointcloud-billboard-label`, `pointcloud-crossframe-track`, `pointcloud-panel-layout`.

### S4 — Review and approval sweep

- Approve every Mac-covered asset only after its own inspection; split approvals into per-stage batches so a failure never ships unreviewed approvals.
- Do not approve the 91 deferred assets in this plan even when their current content looks acceptable; they are scheduled for replacement.
- Optional tidy-up (maintainer choice): drop the four retired-GIF review entries, which no longer correspond to referenced assets.

### S5 — Verification and evidence

```sh
pnpm docs:media:audit -- --json --output .artifacts/media-refresh/after.json
node docs-site/scripts/check-image-manifest.mjs --release
node docs-site/scripts/check-orphan-images.mjs --strict
pnpm --filter @anno/docs-site media:test
pnpm docs:build
git diff --check
```

- `--release` is expected to keep failing on exactly the deferred 91 assets; prove the residual set equals the deferred list rather than asserting a green run.
- Add the Unreleased `CHANGELOG.md` entry for the refreshed documentation media (follow the existing media-commit convention).
- Update the maintainer checkboxes in `docs-site/maintainers/image-checklist.md` for the completed scope.
- Append an `## Outcome` section here with landed commits, review records, the residual deferred list, and the release-readiness statement.

## 5. Deferred release gap

The following flows cannot be re-recorded on this Mac and remain stale/provenance-flagged until a Linux/GPU session (ML capabilities or the legacy `screenshots:marketing` orchestration): `ai-assisted-annotation`, `ocr-real-scene`, `sam-tools`, `candidate-keyboard-review`, `candidate-review-lifecycle`, `current-frame-video-inference`, `current-task-image-inference`, `ai-preannotate`, `secondary-inference-attribute`, `background-export-download`, `storage-connector-create-test`, `jobs-bell-active`, `pipeline-apply-project`, `pipeline-template-create`, `ai-pre-variant-selector`, `project-create-existing-resources`, `project-ml-routing`, `project-actions-menu`, `smart-scribble`, `model-market-gpu-resource-overview`, `model-market-runtime-partial-failure`, `model-market-runtime-pool`, `model-market-video-pool`, `platform-overview`, `ai-tracker-panel`, `video-mask-correction-propagate`, `video-timeline-prediction-navigation`, `video-propagate-track-vs-copy`, `video-track-batch-propagate`, `video-tracker-box-seed`, `video-tracker-combo-discovery`, `video-tracker-cross-frame-points`, `video-tracker-positive-negative`, `video-tracker-range`, `video-tracker-text-discovery`, `jobs-retry-recovery`.

This campaign therefore reduces the audit debt to the deferred set but cannot make `docs acceptance` (`--release`) pass by itself; that stays blocked until the Linux/GPU batch re-records and re-reviews those assets.

## 6. Open items and risks

- Pointcloud native capture on Mac may fail the retained cadence gates; the canary decides, and failure is reported rather than worked around.
- The full static matrix runtime is long and can flake; do not rebuild the manifest from partial runs (`SCREENSHOT_VALIDATE_ONLY=1` stays validation-only).
- AI static scenes under the protocol stub must not be used to refresh the homepage SAM masters; those masters are deferred with the ML flows by design.
- Seed note: `c5938c6e` changed recording-anchor fixture content while keeping revision `screenshots-2026-08-g`. Re-recordings use current fixtures, so this does not block the plan; if maintainers want stricter revision semantics, bumping the revision later invalidates all `-g`-stamped media and must be planned separately.
- Rewritten upstream of approval: any new documentation edit after approvals invalidates the watched paths and requires re-approval of the affected assets.

## Outcome (static and workflow recordings)

- Landed commits: `6b5a7a5b` (evidence dir ignored), `1c6355af` (workbench UI drift: mask capsule, discussion title, audit fixture note), `6ea7f396` (failed-jobs API contract, SMTP scroll, exemplar padding), `df2752a1` (63 static images + 2 new scenes + hero WebPs + embeds + changelog), `8adc14a8` (static review records), then the recording series `9350a29b`, `4209f57f`, `e21d60ff`, `415c9b17`, `5528aed3`, `1d4a6ea8`, `1cd34dda`, `3207fc2f`, `1dca194e`, `e4a2824f` (video scopes, selection-card occlusion, mask ContextToolbar/shortcut, copy drift) and their media/review commits (`19bf3450`, `ee6300fa`, `6b9f93ee`, `24a3a03b`, `a1766b48`, `8c331369`, `8d4889d6`).
- Static matrix: all 63 user-guide images and the four hero WebPs were recaptured on a clean tree at seed `screenshots-2026-08-g`; the two registered scenes that had never produced output are now embedded (`docs-site/user-guide/ai/current-task-inference.md`, `docs-site/user-guide/workbench/video-propagate.md`).
- Workflow recordings: sixteen flows re-recorded and derived with reviewed clip windows — the eleven drawing/batch/video/large-image/hotkey flows plus `video-track`, `video-timeline-zoom`, `video-track-carryover`, `video-draw`, and `video-mask-track-edit`. Every published video and poster carries clean provenance and a delegated review record.
- Review-only approvals for the clean assets `ai-prediction-import`, `video-tracker-job-states`, `review-reject`, `workspace-layout-persistence`, and `e2e-quickstart` after frame inspection.
- Audit progression: baseline `0 broken / 202 stale / 9 current`, 160 provenance warnings → final `0 broken / 105 stale / 108 current`, 66 provenance warnings. Every remaining partial or stale asset belongs to the deferred set below.
- Environment evidence and warm-up scripts are in the git-ignored `.artifacts/media-refresh/` (audit partitions, business audit event, DuckDB analytics sync, task-event activity, large-image fixtures). macOS specifics: `libvips` via Homebrew plus `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`, and `IMAGE_PYRAMID_SRGB_PROFILE=/System/Library/ColorSync/Profiles/sRGB Profile.icc` for pyramid generation.
- Checks: `check-image-manifest --strict`, `check-orphan-images --strict`, media policy tests, `pnpm docs:build`, and `git diff --check` all pass.

## Remaining work

- S3 pointcloud canary (`pointcloud-camera-seed-3d-box` marketing master) and the review of the remaining pointcloud assets were not started.
- `workspace-layout-basics` is the only Mac-recordable flow still deferred. The maintainer confirmed that dragging a panel divider works manually, but no automated drag reproduced it: the recorder run, plain Playwright mouse drags, and real CDP drags through agent-browser all left the panel sizes unchanged. The next attempt should start from the exact manual drag position, or the project should investigate dockview sash input handling under automation.
- The ML and legacy Linux-suite assets (91 referenced files) still require the Linux/GPU batch; their seed revisions (`-e`/`-f`) and dirty-capture provenance remain untouched. `video-mask-correction-propagate` received the same scope/ContextToolbar/Enter fixes so the future Linux recording can use them.

## Review follow-up (PR #96 comments)

- `notifications/panel-overview`: the capture database has no persisted notifications, so the panel rendered the empty state. The scene now serves a deterministic list before opening the bell (7 rows across task / batch / feedback / annotation-comment / export / async-job, 3 unread, `created_at` aligned with the fixed screenshot clock) and waits for the first row before capturing. A DB-seeded variant was rejected on purpose: unread rows add the topbar bell dot to every capture and would invalidate unrelated assets.
- `settings/system-smtp`: the scene now scrolls the complete “邮件与访问地址” card to the top of the viewport (`scrollIntoView({ block: "start" })`; card 47.5 → 857 in the 900px viewport), so 发件人 / 密码 / 发送测试邮件 are all in frame.
- Approval reachability: review records bind `last_verified_commit` to branch commits. Merge with a merge commit (as done for PR #96) to keep them reachable; after a squash re-bind the records to an equivalent-content commit, otherwise `changedWatchPaths()` reports `<复核提交不存在>` and approved assets turn stale.
- The static matrix was re-run after the scene edits. The fresh captures also picked up data drift (seeded task display IDs, project ordering/counts, audit rows, worker heartbeats); every changed capture was diff-reviewed and re-approved at the new commit.

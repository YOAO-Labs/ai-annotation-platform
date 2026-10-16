# Isolated capture and visual checks

Use `apps/web/playwright.screenshots.config.ts` and the existing screenshot catalog/flows. The maintained human guide is [update-screenshots.md](../../../../docs-site/dev/how-to/update-screenshots.md).

## Environment and fixtures

Default screenshot isolation uses database `annotation_screenshots_test`, API port 8010, web port 3001, and Redis DB 15. Verify actual configuration and availability before launching processes. API and dedicated worker must share that test database and isolated Redis; everyday workers must not consume capture jobs.

Pin or clear `MIGRATION_DATABASE_URL` and `CELERY_BROKER_URL` in each capture process as well as setting `DATABASE_URL` and `REDIS_URL`. In `app/config.py`, those overrides take precedence for migrations and Celery broker/results. Check the effective targets, including host/port/database, without printing credentials. The database-preparation suffix guard does not protect a separately invoked Alembic command from an inherited migration URL.

Use `apps/api/scripts/prepare_e2e_db.py`, Alembic, and `apps/api/scripts/seed.py --profile screenshots --repair --ml-backend-mode <live|stub>` in the isolated environment. Preserve the test-database suffix guards. Keep `E2E_SEED_ENABLED` process-local. Do not externally repair seed while a capture matrix is using its catalog snapshot. Selected recordings and marketing masters intentionally repair their own seed between flows and immediately reload catalog IDs; give each run exclusive use of its capture database.

Resolve project/task/backend IDs through the catalog, not copied UUIDs. Check logical fixture keys and media readiness. Coordinate anchors use the media's actual rendered rectangle, excluding letterboxing. nuScenes scenes, camera roles, coordinate conventions, and targets must agree with current seed metadata; do not carry old synthetic box positions into a new dataset.

Protocol stubs are appropriate for supported ordinary screenshot/GIF validation. They are not evidence of real-model inference or replacements for live-model marketing masters.

## Capture the requested output

Use targeted `--grep`/project selection and `SCREENSHOT_VALIDATE_ONLY=1` when only validating navigation and locators. Existing commands include `screenshots`, `screenshots:matrix`, `screenshots:flows`, and `screenshots:regression` in `apps/web`.

All production video/GIF refreshes must archive qualified marketing masters before publication, then use `pnpm docs:media:derive` for every project derivative. `screenshots:record` defaults to `marketing`. The `docs` profile and legacy `screenshots:flows` are functional rehearsal only (`--validate-only` / `SCREENSHOT_VALIDATE_ONLY=1`); they must not create a parallel production archive. The derivation command retains explicit standard-source compatibility for existing archives when requested. Marketing capture preserves the same 4K60 and cadence gates on both platforms. Linux uses X11/NVIDIA; macOS uses ScreenCaptureKit only for explicitly selected flows with no ML capabilities and no inference. Never relabel a standard recording as a qualified master or bypass capture-quality gates. Static screenshots and static Hero image transformations remain independent.

## Cross-platform recording

The browser/encoder host and the inference host are independent. A Mac can rehearse real SAM3 or OCR interactions against registered remote services with `--profile docs --validate-only`; native Mac marketing capture remains non-ML only. Qualified ML masters currently require the Linux capture path. Do not publish a Mac rehearsal as a substitute. The capture API and workers still run against the same isolated database and Redis; changing `PLAYWRIGHT_API_BASE` alone does not move the local seed/cleanup scripts to a remote host.

```bash
# Read-only selection and dependency preview, from the repository root.
pnpm --filter @anno/web screenshots:record -- --list
pnpm --filter @anno/web screenshots:record -- --flow bbox-draw --plan
pnpm --filter @anno/web screenshots:record -- --flow ocr-inference --plan

# After preparing the isolated stack and verifying its process environment:
pnpm --filter @anno/web screenshots:record -- --flow bbox-draw
pnpm --filter @anno/web screenshots:record -- --flow sam-tool-smart-point
pnpm --filter @anno/web screenshots:record -- --flow bbox-draw --profile docs --validate-only
```

- The launcher requires `SCREENSHOT_DATABASE_URL`, plus identical `REDIS_URL` and `CELERY_BROKER_URL` selecting an explicit nonzero Redis DB. It pins both database overrides for child processes. Verify the API and worker targets separately; a suffix check does not prove they use the same database. Real inference requires dedicated `screenshots@` workers and no unrelated workers on that broker.
- The launcher repairs the screenshot profile offline before capture, using `--ml-backend-mode live --backend-requirements <scope>`. Manual selections use `none` and do not probe or create any ML backend. OCR uses `ocr`; SAM tools use the existing `image_interactive` bundle, including exemplar. Combining `--flow` flags takes the union. Omission of the scope in legacy commands still requires the complete backend profile.
- This scopes **backend requirements**, not dataset preparation. The existing screenshot asset cache, MinIO media, fixed projects, and nuScenes fixtures remain required; install/cache them first. Media readiness and ownership validation are not bypassed. Optional large-image recordings still require the large-image fixture.
- Register remote backend URLs in the capture environment's existing model registry and verify live capabilities. URLs must be reachable by the API and worker, and the backend must be able to fetch signed media from capture storage. Do not assume the Mac's `localhost` refers to the Linux host; keep storage routing and credentials out of videos and manifests. Do not use `--ml-backend-url` for remote live discovery: that flag configures the protocol stub.
- `docs` is an explicit rehearsal profile and requires `--validate-only`; it produces no production video/archive. Historical `.artifacts/recordings/<run-id>/` files were Playwright/CPU recordings at measured standard quality, usually 1440×810/25fps, with setup footage and approximate action offsets. Preserve those facts when explicitly reusing an existing archive; they are not qualified marketing masters.
- Marketing manifests include asset ID, hashes, measured media facts, browser/platform, source revision, dirty-tree state, capture geometry/cadence, inference/behavior evidence and GIF recipes. Historical standard manifests additionally identify their untrimmed source paths. Capture leaves public assets and reviews untouched. Legacy full-profile flows record inference as `unverified`: a live binding does not prove that a flow did not intercept model responses.
- `marketing` automatically selects `screencapturekit` on macOS and `x11grab` on Linux; the existing `MARKETING_CAPTURE_DRIVER` override still supports Linux `gpu-screen-recorder`. Mac selections must have both `backendRequirements: none` and `recordingInference: none`; an empty capability scope alone is insufficient. `--plan` validates this before runtime setup.
- Mac requires macOS 14+, Xcode Command Line Tools, ffmpeg/libx264, ffprobe, Screen Recording permission for the executing terminal/agent, and a Retina window with a measured 2880×1620 content area at a 1440×810 logical viewport / DPR 2. The native helper is compiled and cached under `.artifacts/marketing/.native/`. Keep the capture window in front rather than a Stage Manager thumbnail. Process/title identity and calibration geometry are checked before recording; first-frame host-clock timestamps drive trimming. Manual Mac capture checks its isolated API/database without requiring an unused ML worker.
- Linux retains X11, NVIDIA, DPR 1.8 / 2592×1458 content geometry, and its existing worker preflight. `--resize-display` is Linux-only. Both platforms retain hardware compositing, rAF p95 ≤20ms, at least 58 calibration samples, effective unique fps ≥55, unique ratio ≥0.9, and 3840×2160/60fps archive output. Native window streams are cropped and normalized before publication; source dimensions remain in provenance. Capture qualification is not a separate point-cloud/Apple renderer performance qualification.
- `--list` also reports profile restrictions. The billboard-label, camera-seed-3d-box, and crossframe-track point-cloud flows require the existing hardware WebGL/60Hz surface even though they do not need ML; selecting them under `docs` fails before seeding, rather than succeeding with a skipped recording.
- Only explicitly enrolled flows are accepted; inspect `apps/web/e2e/screenshots/recording-plan.mjs`. To add a flow, trace its inference, fixture, and cleanup calls, declare its requirement, and test it. `smart-scribble` uses a stored native Mask as its input and requires real positive/negative scribble responses, changed candidate content, preserved source identity and saved-version readback; do not replace these responses with fixtures when recording live evidence. Complex pipeline/tracker flows remain on their existing full-profile commands until their dependencies are declared.

Use `--validate-only` for interaction validation without writing video assets; it still prepares and mutates the isolated fixtures. `--plan` and `--list` do not contact services or seed data. Run `pnpm --filter @anno/web screenshots:record:test` for selector/encoding/manifest regression checks; these checks are not a product-flow or Linux GPU recording acceptance test.

## One derivation pipeline

```bash
# A qualified marketing run; only requested assets are replaced.
pnpm docs:media:derive -- --run <run-id> --asset video-chapter

# Historical standard archive compatibility only: inspect, then select the window.
# This example starts at 2 seconds and retains 8 seconds; use the actual reviewed window.
pnpm docs:media:derive -- --quality standard --run <run-id> --asset video-track --format video --clip 2:8

# Only archived GIF recipes, without publishing a video or poster.
pnpm docs:media:derive -- --quality standard --run <run-id> --asset bbox-draw --format gif --clip 2:4

# An asset with multiple GIF subwindows needs one explicitly selected target per override.
pnpm docs:media:derive -- --quality standard --run <run-id> --asset video-chapter --format gif --gif-target docs-site/user-guide/images/video-timeline/brush-create-chapter.gif --clip 2:4

# Article presets use the same validation, encoding, and provenance implementation.
pnpm docs:media:derive -- --run /absolute/path/to/run --article 06-video-track --asset video-draw
pnpm --filter @anno/web screenshots:docs-media:test
```

`--quality` defaults to `marketing`; never silently fall back to standard. New production refreshes use marketing sources; explicit `standard` examples above apply only to historical archives whose reuse was requested. `--run` accepts a run name under the selected archive root or an absolute run directory containing `manifest.json`. Without `--asset`, only registered assets present in that run and supported by the requested format are processed, not the complete asset catalog. `--format all` is the default; `video` includes video/poster outputs and `gif` uses recorded GIF recipes. Historical marketing manifests without GIF recipes still support video/posters; recapture before explicitly requesting their GIFs. Implicit GIF batches skip video-only assets.

Every standard-source publication, including GIFs, requires an explicit `--clip start:duration` and one selected asset, because those sources retain setup footage and test-start offsets are approximate. The explicit clip overrides a recipe's estimate. Do not apply one override to multiple GIF subwindows: use `--format video` for the main video, then `--format gif --gif-target <registered-path>` with each reviewed subwindow. Chapter creation and chapter resizing must remain distinct. Without an override, qualified marketing sources use their recorded GIF recipes. Marketing archives trim both MKV and MP4, so the recorder translates GIF windows into the archived timebase exactly once and clamps only trailing padding to the actual end. Never apply the original capture offset again during derivation.

The shared implementation is `apps/web/scripts/media-derivation.mjs`. It checks source hashes, media facts, paths, clip bounds, and quality evidence before publishing. Standard outputs are bounded by the source's dimensions and frame rate; upscaling or repeated frames do not upgrade quality. Marketing sources must retain 4K60 file facts and hardware/cadence evidence. Encoders need ffmpeg/ffprobe; WebP uses ffmpeg's `libwebp` or an installed `cwebp` fallback. Failure to encode any requested derivative leaves existing published files untouched. Unrelated manifest entries are preserved; `source_asset` records the source hash, quality, and actual clip for each output.

`--article <id>` selects paired article GIF/PNG presets through the same public entry point; do not combine it with `--format` or `--gif-target`. The older `scripts/derive-article-media.mjs --archive ...` command remains a compatibility entry to the same implementation.

Do not reinterpret successful derivation as visual acceptance or update `media-reviews.json` automatically. Inspect the source and generated core interaction, then follow the separate review workflow. Existing published media is not retroactively regenerated or reclassified by the code migration.

Trim loading only before the core interaction; do not cut off the action or pad footage with repetition. Keep manual `auto: false` images intact unless explicitly updating them. Regenerate affected homepage derivatives after their source screenshots change.

## Diagnose a failing visual test

Open the trace from the actual failing test and build/platform. A geometry save may succeed while a preferences PATCH fails; inspect the request rather than guessing from a screenshot. Check float bounds normalization, canvas readiness, selected annotation identity, and floating overlays/pets intercepting clicks.

Update intentional screenshot baselines for the relevant platform, then rerun without update mode. Missing locators should fail, not fall back to a full-page screenshot. Use real annotation save/accept responses as evidence, not only the last frame.

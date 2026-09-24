# Point-cloud surface enhancement phase 1

> Status: implemented; real-project RTX 3090 benchmark completed with promotion-gate exceptions

## Context

The point-cloud workbench already renders the current LiDAR frame, can project camera RGB onto it, and can align bounded neighboring frames with ego poses. Those capabilities are sufficient for a useful first visual-enhancement phase without training or a generative model. The workbench must not present inferred geometry as annotation truth, and it must preserve its single renderer owner and point-index contracts.

This phase adds an optional **surface enhancement** display style. It uses soft circular splats and the existing bounded, pose-aligned neighboring points to make static surfaces easier to read. It remains a view-only aid: the current LiDAR frame is the sole source for picking, point masks, measurements, boxes, and saved annotations.

## Goals

- Add a persisted point-cloud display-style preference with `points` as the compatibility default and `surface` as the opt-in enhancement.
- Render soft circular splats in both the legacy WebGL2 and WebGPU paths without adding a second renderer, canvas, or animation loop.
- Apply the display style consistently to the main view and orthographic tri-view; neighbor-frame points may contribute to visual density but never to picking or annotation indices.
- In surface mode, reuse the existing bounded neighbor-frame loading, ego alignment, dynamic-point handling, cache, cancellation, and task/scene ownership rules.
- Keep the feature usable on one RTX 3090 by bounding additional point counts and avoiding server inference, model weights, or training.
- Document the annotator-visible behavior, limitations, and fallback semantics.

## Non-goals

- No NeRF, 3D Gaussian Splatting training, diffusion generation, mesh reconstruction, or hole-filling geometry.
- No synthesized points, object completion, semantic segmentation, or changes to annotation payloads.
- No promise of photorealism. Camera colorization remains an independent existing option; absent RGB coverage, the normal point-cloud colors remain authoritative.
- No multi-session preprocessing service or GPU scheduling changes in this phase.

## Design

### Preference and UX

Add `pointcloud.displayStyle: "points" | "surface"` to account-level workbench preferences. The settings panel describes `surface` as a view-only soft-splat enhancement. Existing accounts and tasks resolve to `points`.

Surface mode implicitly requests the same bounded neighbor-frame data used by the existing neighbor overlay when scene ego poses are available. The explicit neighbor-overlay setting continues to control diagnostic trail colors and its configured frame count. Surface enhancement uses at most one frame on each side by default and the existing neighbor point budget; if poses or neighboring frames are unavailable, it degrades to soft splats on the current frame. Dynamic-point culling/alignment uses the existing preference and annotation-derived transforms.

### Rendering and truth boundaries

- Extend the existing point layer with a display-style input rather than introducing another renderer owner.
- `points` preserves current point appearance and behavior.
- `surface` uses a soft circular/elliptical alpha profile and a conservative size multiplier. Depth testing remains enabled, and depth writes must not make transparent borders occlude later splats.
- The main point geometry remains the only raycast object and the only geometry exposed to point-mask selection.
- Neighbor visual layers stay non-pickable and do not enter tri-view editing geometry.
- Camera colorization and selection highlighting remain compatible with both display styles.
- Renderer device loss and unsupported WebGPU continue through the existing legacy fallback path with the selected style retained.

### Lifecycle and performance

- Bind asynchronous neighbor results to the current task/scene generation through the existing query key, abort signal, and scene-clearing effect. A stale completion must not repopulate a switched task.
- Keep the current point-decimation policy. Surface-mode neighbors are capped by the existing `min(80,000, pcdDecimate / 8)` per-frame target and at most the nearest previous/next frame unless the explicit overlay asks for more.
- Avoid per-frame CPU buffer rebuilding. Style changes update materials/nodes in place; geometry changes continue through the existing scene load owner.
- Record benchmark evidence on the actual adapter instead of copying historical thresholds. Acceptance is no regression outside the repository's current point-cloud benchmark limits and no sustained unbounded frame-time or GPU-memory growth during task switches.

## Implementation workstreams

1. **Rendering core**
   - Add a shared display-style type and soft-splat material profile.
   - Implement legacy WebGL2 and WebGPU parity in `PointCloudScene`, `PointCloudTriViewPass`, and the WebGPU point layer.
   - Preserve raycast geometry, clipping, camera colorization, selection, disposal, and renderer ownership.
   - Add focused unit coverage for style updates and resource disposal.

2. **Preference and workbench orchestration**
   - Extend API validation, frontend preference types/defaults, settings metadata, and tests.
   - Pass the style through `usePointCloudScene` without recreating the renderer.
   - Make surface mode request bounded neighbor data while preserving explicit overlay behavior and stale-result clearing.
   - Add regression tests for defaults, validation, scene switching, and preference synchronization.

3. **Documentation and qualification**
   - Update the point-cloud user guide and the Unreleased changelog.
   - Run focused API and web tests, CSS-token lint if UI styles change, web type/lint checks for touched code, and `git diff --check`.
   - Run the repository point-cloud renderer benchmark in supported legacy and WebGPU modes on the RTX 3090 host. Report the detected adapter, browser/backend, scenario point counts, and artifacts. Clean generated temporary artifacts after recording results.
   - Perform a real-browser smoke test covering points/surface switching, current-task picking, neighbor fallback, task/scene switching, tri-view, and WebGPU fallback where the environment supports it.

## Acceptance criteria

- Existing users see the unchanged `points` default.
- Switching to `surface` changes only rendering and does not reload the page or recreate the renderer.
- Current-frame point picking, point-mask indices, box editing, and saved annotation payloads are identical between styles.
- Surface mode works with no cameras and no neighbors; camera RGB and aligned neighbors enhance it when available.
- Stale neighbor loads cannot appear after task/scene switching, and disabling the feature clears its visual-only neighbor layers.
- Main view and tri-view render the selected profile in both legacy WebGL2 and WebGPU, with normal device-loss fallback.
- The current benchmark gate passes on the available RTX 3090, and the final report states measured results rather than inferred feasibility.
- User documentation and `CHANGELOG.md` describe the feature as a non-generative annotation aid and state its limits.

## Risks and mitigations

- **False confidence from visually filled surfaces:** keep the feature opt-in, use explicit “visual enhancement” wording, and never feed neighbors or soft coverage into annotation truth.
- **Dynamic-object ghosting:** reuse keep/cull/per-object-align behavior; default surface assistance to the nearest neighbor pair and document residual artifacts.
- **Transparency ordering or depth artifacts:** use an alpha-tested soft profile with depth testing and verify overlapping surfaces in both backends.
- **GPU pressure:** retain decimation, cap neighbors, reuse materials and geometry, and validate repeated task switches on the target hardware.
- **Backend drift:** keep a single style contract and focused parity tests for legacy and WebGPU implementations.

## Follow-up boundary

Only after phase 1 usability and performance evidence should a separate plan evaluate offline-trained dynamic 3D Gaussian Splatting or other learned reconstruction. That work would require dataset-specific training, calibration/pose quality gates, confidence visualization, artifact caching, and a strict separation between generated context and annotation truth.

## Outcome

- Landed commits: `0ead62ba1`, `50ce6246a`, `cc28e2cc0`, `32353788b`, `7b3dd307a`, `b2450e6ce`, `a627a0443`, `b91b242de`, `d12881364`
- Release milestone: Not yet determined
- User documentation: `docs-site/user-guide/workbench/pointcloud-view.md`, `docs-site/user-guide/reference/settings.md`, and generated workbench settings reference
- Developer documentation: no persistent data-model or renderer-ownership change; the existing `docs-site/dev/reference/point-cloud-data-model.md` contract remains applicable
- ADR: not required; the implementation extends the existing single-renderer architecture without a new service or persisted annotation contract
- CHANGELOG: Unreleased entry added
- Verification: 46 focused web tests and focused API preference tests passed; web typecheck and lint passed with five unrelated pre-existing warnings; Legacy point-cloud smoke/settings/PointMask qualification passed; 21 strict WebGPU edit/playback tests passed; the strict surface-style preference test passed while preserving one canvas and one renderer owner. A separate browser probe selected one non-fallback NVIDIA Ampere WebGPU adapter. The real-project benchmark used nuScenes mini `scene-0061` (39 frames, six cameras, ego poses, 34,688 points in the initial frame and 34,720 in its next frame) at 1440×900/DPR 1 with 20 cold and 20 adjacent-warm samples per style. Both `points` and `surface` selected the real `webgpu` backend, kept one renderer owner, performed zero WebGPU canvas readbacks, emitted no runtime errors, and held idle rAF p95 at 16.8 ms. WebGPU `points` recorded cold/warm geometry p95 of 379.7/117.5 ms, warm RGB p95 of 135.5 ms, and refinement first-open/reopen of 128.6/126.5 ms. WebGPU `surface` recorded 399.4/122.8 ms, 143.6 ms, and 202.7/133.3 ms respectively. The surface steady-state overhead was about 5% for geometry and 6% for warm RGB; its refinement reopen overhead was about 5%, while the first open also paid the initial neighbor-load cost. GPU-process RSS did not grow over the measured cycle, cache entry counts remained bounded, and surface mode ended with one depth-raster request still settling.
- Promotion result: the benchmark run was valid, but the repository promotion gate did not pass. The unchanged `points` baseline already exceeded the 250 ms cold-geometry and 50 ms refinement-open thresholds. `surface` also exceeded the 120 ms warm-geometry threshold by 2.8 ms and its first-open-within-10-ms-of-reopen check. These are recorded as measured exceptions rather than treated as a pass.
- Remaining work: profile the adjacent-warm navigation-to-paint path and Dockview tri-view activation, then rerun both display styles before promotion. Thresholds must not be relaxed solely to make a run pass. Learned reconstruction remains outside phase 1.

## Cold-load follow-up

The shared cold-load profile showed that the timeline waited 160 ms to settle a click before starting target-frame prefetch. After navigation, it immediately prefetched the following frame through the same latest-only speculative lane, often cancelling the target prefetch before the active scene could use it. The timeline now starts the first selected target's prefetch at click time, coalesces rapid clicks to the first and final targets, and waits for target prefetch completion before warming the following frame. Navigation itself does not wait for prefetch. The selected target still owns the active scene; stale prefetch results cannot enter annotation data.

On the same nuScenes mini `scene-0061` fixture and RTX 3090 browser configuration, two complete 20-cold/20-adjacent-warm rounds in `surface` display style measured WebGPU cold geometry p95 of 168.9 and 181.9 ms, down from the initial 399.4 ms result. Cold decoded-frame cache hits were 14/20 and 16/20; both rounds passed the 250 ms cold-geometry check. The actual backend remained `webgpu` with one renderer owner, zero canvas readbacks, and no runtime errors. The browser's same-origin adapter probe reported NVIDIA Ampere; the `isFallbackAdapter` field was unavailable in this Chrome build. The shell's forwarded `DISPLAY=localhost:11.0` could not initialize Vulkan, so qualification used the host's local `DISPLAY=:0` and its X authority.

The full promotion gate remains unmet. WebGPU adjacent-warm geometry p95 was 152.1 and 133.1 ms against the 120 ms check. Tri-view first-open times were 190.1 and 195.2 ms versus reopen times of 153.9 and 140.3 ms, exceeding both the 50 ms absolute check and the within-10-ms check. Legacy tri-view first opens were 136.7 and 135.0 ms, confirming that much of the opening cost is shared UI/layout work. These thresholds were not changed. Next work should profile the warm navigation-to-paint path and Dockview panel activation separately, then retest both styles before promotion.

### Hot-frame navigation and tri-view activation follow-up

Profiling separated click settling, route update, cached frame handoff, and paint. In the original five-sample `surface` check, every adjacent frame hit the decoded cache, but history update to decoded-frame readiness still took about 73–90 ms; a separate 160 ms click-settle interval preceded history update. Moving point-cloud loading into a layout effect or reading a completed decode synchronously only moved work into the same main-thread task and did not improve geometry-to-paint p95, so those experiments were reverted.

The timeline now uses a 120 ms click-settle interval only when the target's cached manifest and decoded frame are ready. WebGPU additionally requires all camera bitmaps and the exact depth raster for that frame, point count, camera dimensions, and calibrations to be ready; otherwise it retains the 160 ms interval. Rapid 100 ms clicks still coalesce to the final target, and playback timing is unchanged. Dockview visibility commands now publish the tri-view and camera-panel visibility in the same update as the layout action. The tri-view retains its measured layout while toggled, applies active and box state before paint, and unregisters layout only when the panel or scene owner unmounts. One renderer and canvas remain in use.

On the same nuScenes mini `scene-0061` fixture, RTX 3090 browser launch, 1440×900 viewport, and DPR 1, a complete 20-cold/20-adjacent-warm run per backend and display style measured:

| Display style / backend   | Cold geometry p95 | Warm geometry p95 | Warm click-to-history p95 | Warm RGB p95 | Warm depth hits | Tri-view first / reopen |
| ------------------------- | ----------------: | ----------------: | ------------------------: | -----------: | --------------: | ----------------------: |
| `surface` / Legacy WebGL2 |          132.1 ms |          126.4 ms |                  121.6 ms |     543.5 ms |               — |         127.4 / 94.5 ms |
| `surface` / WebGPU        |          140.7 ms |          136.7 ms |                  122.4 ms |     161.7 ms |           20/20 |        174.6 / 100.7 ms |
| `points` / Legacy WebGL2  |          138.7 ms |          136.2 ms |                  121.8 ms |     499.8 ms |               — |         119.0 / 92.0 ms |
| `points` / WebGPU         |          124.4 ms |          116.1 ms |                  161.4 ms |     137.2 ms |           20/20 |        102.5 / 100.7 ms |

All four runs had 20/20 adjacent-warm samples with all relevant cache stages ready, one renderer owner, and no runtime errors. Both WebGPU runs selected the actual `webgpu` backend and performed zero canvas readbacks. The `surface` run still fails the 120 ms warm-geometry, 50 ms first-open, and first-open-within-10-ms-of-reopen checks. The `points` run passes warm geometry and the reopen gap but still fails the 50 ms first-open check. Absolute geometry and panel timings vary between browser runs; the stable improvement is the bounded click-settle reduction on fully prefetched targets, without a depth-cache regression. The performance promotion gate remains closed.

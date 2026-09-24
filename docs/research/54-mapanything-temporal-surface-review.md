# MapAnything three-frame temporal surface review

## Decision

Keep the three-frame result as an **offline visual experiment**. Feeding adjacent views to MapAnything reduced depth error on identical held-out LiDAR returns in five of six cameras of tuning scene `scene-0061` and four of six cameras of separate check scene `scene-0103`. The improvements are inconsistent, and the bounded temporal fusion added only 0.04–1.91% of image pixels beyond the middle frame's model output. A stricter consistency display removed substantial coverage. The model's depth MAE still exceeded the current-frame LiDAR-nearest baseline on model-valid held-out points in all 12 camera/scene pairs. This does not open the [production model-context gate](../plans/1790239103_pretrained-model-rendering-context.md).

That LiDAR-nearest baseline uses only the middle frame's input points, while the model also receives adjacent-frame LiDAR prompts. A pose-aligned multi-frame LiDAR baseline is still needed for a full geometry comparison; the narrower current-frame baseline already exposes a failure to outperform available measured geometry.

The [offline generator](../../apps/api/scripts/build_mapanything_review.py), [temporal geometry helper](../../apps/api/scripts/mapanything_temporal.py), and [static viewer](../../apps/web/scripts/mapanything-review-ui/index.html) make the tradeoff visible. The viewer offers **middle-frame raw surface**, **three-frame hole fill**, and **strictly corroborated area** in the same six-camera scene. It remains outside the workbench, picking, PointMask, measurements, and annotation data.

## Method

Use nuScenes mini key frames 0, 1, and 2, approximately 0.5 seconds apart, for every camera role. The anchor is frame 1. On one RTX 3090, run six separate three-view MapAnything inferences, one per camera role, with the same pinned checkpoint/source as the [previous review](52-mapanything-visual-review.md). Inputs are RGB, each camera's intrinsics and timestamped ego pose, and deterministically split sparse LiDAR depth. Each frame's held-out returns are excluded from that frame's input raster. Separate groups keep the 18-view experiment bounded on one card; the six anchor surfaces are transformed into frame 0's ego reference for display.

The checkpoint revision is `00f9c245bbcb60522d1ed7f9e9d88462c6e3f38a`, the MapAnything source revision is `3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9`, and the local run used PyTorch `2.6.0+cu124`.

A source pixel is temporally corroborated when its depth, projected using measured poses, agrees with another time within `max(0.75 m, 5% of nearer depth)` or agrees with its own input LiDAR prompt. A source pixel may fill an invalid middle-frame pixel only after that check, an exact or nearby middle-frame input LiDAR check (3 pixels, `max(1 m, 10%)`), a nearby middle-frame model-depth check (2 pixels, the temporal depth tolerance), and mean RGB difference at most 35/255. The closest passing projection wins per target pixel. Hole-fill mode retains all original middle-frame pixels. Strict mode displays only corroborated middle-frame pixels and accepted fills. Both use the [existing depth-edge mesh rule](53-mapanything-six-camera-surface-review.md); neither is a semantic dynamic-object mask or a watertight reconstruction.

The [artifact comparator](../../apps/api/scripts/compare_mapanything_review.py) requires matching scene, camera, frame, model revision, processed size, and **identical held-out pixel/depth arrays**. Its paired MAE uses only returns valid in both the single-frame and three-frame middle-view outputs. Hole-fill MAE uses only returns newly covered by fusion. These are observed LiDAR locations; they cannot certify LiDAR-empty surfaces.

## Paired results

The valid-pixel columns show the independent single-frame inference, the three-frame hole-fill surface, and the strict surface. The MAE pair is single-frame / three-frame model output on exactly the same held-out returns; filling holes does not change either depth on this common set. Newly covered return counts are small, so their MAE is diagnostic rather than a stable quality estimate.

| Scene | Camera      | Single valid | Filled valid | Strict valid | Common returns | Paired MAE, m | New returns / MAE, m |
| ----- | ----------- | -----------: | -----------: | -----------: | -------------: | ------------: | -------------------: |
| 0061  | Front       |        62.1% |        62.8% |        51.8% |            490 | 0.387 / 0.347 |           10 / 0.467 |
| 0061  | Front right |        62.8% |        64.4% |        46.3% |            444 | 0.931 / 0.862 |            4 / 0.313 |
| 0061  | Back right  |        67.0% |        69.3% |        46.1% |            430 | 0.992 / 0.851 |           17 / 0.395 |
| 0061  | Back        |        76.1% |        76.7% |        56.6% |            644 | 0.572 / 0.660 |            7 / 1.821 |
| 0061  | Back left   |        84.1% |        85.8% |        59.3% |            737 | 0.490 / 0.381 |            7 / 1.107 |
| 0061  | Front left  |        88.3% |        89.4% |        62.7% |            640 | 0.296 / 0.254 |            1 / 0.362 |
| 0103  | Front       |        67.3% |        69.2% |        50.8% |            438 | 0.871 / 0.701 |            6 / 0.552 |
| 0103  | Front right |        74.8% |        74.9% |        48.1% |            384 | 0.345 / 0.316 |            1 / 0.240 |
| 0103  | Back right  |        40.7% |        42.0% |        14.9% |            289 | 0.188 / 0.234 |            1 / 0.387 |
| 0103  | Back        |        46.2% |        47.8% |        38.3% |            487 | 0.375 / 0.340 |            7 / 0.861 |
| 0103  | Back left   |        39.4% |        43.1% |        28.6% |            362 | 0.400 / 0.403 |                0 / — |
| 0103  | Front left  |        64.0% |        64.8% |        49.4% |            472 | 0.723 / 0.686 |           10 / 1.159 |

For `scene-0061`, three-frame inference took 4.07 s versus 1.56 s for the joint six-camera single frame. For `scene-0103`, the corresponding times were 3.95 s and 1.60 s. Total generation before the small viewer bundle took 32.56/27.98 s and 32.30/30.36 s, respectively; the cached model loads accounted for roughly 23–25 s per process. PyTorch peak reserved memory was 7,860 MiB for the six sequential three-view groups and 7,884 MiB for joint six-view inference. These are local CUDA/PyTorch measurements, not process VRAM, browser rendering, or app coexistence benchmarks.

## Visual review and limits

The ignored local artifact `.artifacts/model-review/mapanything-temporal-scene0061/` contains the review page, data, six camera images, meshes, and `preview-temporal-camera.png`, `preview-temporal-overview.png`, and `preview-temporal-strict.png`. The selected-camera preset still shows connected road/building patches. The overview still has holes and seams; the extra pixels do not produce a complete surround scene. The separate `scene-0103` artifact uses the same frozen fusion thresholds and was not used to retune them.

The final page loaded all six anchor cameras in a local headless Chrome 150 session at 1280 × 633, DPR 1. It had one canvas and used WebGL through ANGLE/SwiftShader. Surface-mode switching, strict metrics, camera selection, and the fill overlay worked. The browser logged a WebGL context loss followed by restoration; no page error remained. This checks the static review UI, not hardware WebGPU or the production workbench renderer.

To reproduce from an isolated environment containing the pinned MapAnything source and CUDA PyTorch stack:

```bash
CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/build_mapanything_review.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 --frames 0,1,2 --cameras all --temporal-fusion \
  --output .artifacts/model-review/mapanything-temporal-scene0061

CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/build_mapanything_review.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 --frames 1 --cameras all \
  --output .artifacts/model-review/mapanything-single-frame1-scene0061

python apps/api/scripts/compare_mapanything_review.py \
  .artifacts/model-review/mapanything-single-frame1-scene0061 \
  .artifacts/model-review/mapanything-temporal-scene0061

python3 -m http.server 8767 --bind 127.0.0.1 \
  --directory .artifacts/model-review/mapanything-temporal-scene0061
```

Open `http://127.0.0.1:8767/`. The artifacts contain dataset-derived images and must remain local. This two-scene, three-frame trial is too small to establish temporal stability over complete 39–40 frame scenes, dynamic-actor correctness, long-range safety, or annotator value. Further work should compare against a pose-aligned multi-frame LiDAR surface and evaluate full scenes and dynamic-object strata before considering a scene-level optimized representation.

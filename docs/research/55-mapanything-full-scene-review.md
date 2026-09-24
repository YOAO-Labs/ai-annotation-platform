# MapAnything full-scene sequence review

## Decision

All 39 key frames of nuScenes mini `scene-0061` now participate in an offline six-camera reconstruction review. Each key frame becomes an anchor, with its adjacent one or two key frames supplied as context to six separate camera-role inferences. The 234 anchored surfaces are aligned by measured ego/camera poses and assembled into a bounded scene mesh. This is **not** a single 234-view network inference, a watertight reconstruction, or a production workbench asset.

The full-sequence result does not pass the [model context geometry gate](../plans/1790239103_pretrained-model-rendering-context.md). On model-valid held-out LiDAR returns, the fused surface's weighted MAE is approximately **0.854 m**, versus **0.582 m** for the current-frame input-LiDAR nearest-depth baseline on the same returns. The stricter corroborated region scores **0.610 m** versus **0.424 m**, while reducing coverage. Only 10 of 234 frame/camera views beat that baseline. The offline page remains available for visual inspection; model geometry does not enter picking, PointMask, measurements, or saved annotations.

## Input and assembly

Use the complete 39-key-frame sample chain in the local nuScenes mini `scene-0061`, all six camera roles per anchor, and one RTX 3090. The first and last anchors use their only adjacent key frame; interior anchors use previous/current/next. Every input includes RGB, calibrated intrinsics, measured timestamped camera poses, and a deterministic sparse LiDAR prompt. Returns reserved for scoring are excluded from the anchor prompt. The MapAnything checkpoint is `facebook/map-anything-apache` revision `00f9c245bbcb60522d1ed7f9e9d88462c6e3f38a`; pinned source revision is `3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9`.

Each three-view group is processed separately to keep GPU usage bounded. The [existing temporal rules](54-mapanything-temporal-surface-review.md) corroborate pixels with another frame or the same frame's prompt; accepted neighbor projections fill invalid anchor pixels after depth, nearby LiDAR, and RGB checks. The anchor's original valid pixels remain in fused mode. Strict mode displays only corroborated or accepted pixels. No semantic motion test is performed.

All anchor surfaces are placed in the first key frame's ego reference. The [scene assembler](../../apps/api/scripts/mapanything_scene.py) groups triangles into 0.5 m position/orientation cells and keeps the observation from the nearest camera per cell, with a 250,000-triangle cap. This trades duplicate overlapping faces for bounded browser geometry and can create gaps at cell boundaries. The assembled fused mesh has **170,494 triangles** from 940,657 source preview triangles; strict mode has **118,529 triangles** from 719,866. The displayed mesh uses the full assembled index buffer. A separately aligned, 0.2 m voxelized LiDAR cloud contains **245,388 points**. This cloud is a display reference, not a multi-frame LiDAR-depth baseline.

## Held-out results

The rows below pool per-view metrics by the number of model-valid held-out returns. Source summaries round MAE to 0.001 m before pooling, so the pooled figures are approximate. Coverage and filling are unweighted means of the 234 camera images.

| Display              | Valid returns | Model MAE | Current-frame LiDAR MAE | Mean valid pixels |
| -------------------- | ------------: | --------: | ----------------------: | ----------------: |
| Temporal fill        |       135,181 |   0.854 m |                 0.582 m |            72.76% |
| Strict corroboration |       111,056 |   0.610 m |                 0.424 m |            56.86% |

The accepted neighbor pixels add **1.34 percentage points** of image area on average. The baseline uses only the anchor's input LiDAR, while the model also reads neighboring-frame prompts. These results do not establish an advantage over a pose-aligned multi-frame LiDAR baseline. The model-valid set also leaves some distant and occluded geometry unevaluated; a held-out return is evidence only at its measured ray.

| Camera      | Valid returns | Model MAE | Current-frame LiDAR MAE | Frame wins / 39 |
| ----------- | ------------: | --------: | ----------------------: | --------------: |
| Front       |        19,514 |   0.889 m |                 0.624 m |               0 |
| Front right |        20,331 |   1.184 m |                 0.789 m |               3 |
| Back right  |        22,213 |   1.055 m |                 0.743 m |               4 |
| Back        |        23,721 |   0.864 m |                 0.548 m |               0 |
| Back left   |        26,802 |   0.594 m |                 0.418 m |               0 |
| Front left  |        22,600 |   0.630 m |                 0.431 m |               3 |

Accumulated model inference took **136.59 s**. The sum of per-frame generation times, including I/O, fusion, and per-view assets but excluding initial model load and final scene assembly, was **215.73 s**. The elapsed artifact timestamp interval from manifest creation to final summary was about **243 s**. Peak PyTorch reserved memory on the exposed RTX 3090 was **7,748 MiB**; this is not process VRAM or concurrent app-load evidence. The ignored local artifact occupies about **1.41 GiB**.

A separate interruption check wrote anchor 0, restarted with `--resume`, and then wrote anchor 1 without repeating anchor 0. Its temporary output was removed after the check.

## Human review

The local [static viewer](../../apps/web/scripts/mapanything-scene-review-ui/index.html) in `.artifacts/model-review/mapanything-full-scene0061/` can move through all 39 anchors, select all six cameras, switch between global scene/current six/current camera surfaces, compare fused and strict surfaces, toggle aligned LiDAR, and jump to a camera pose. It loads only the current selection plus the bounded global mesh. Screenshots `preview-scene-overview.png` and `preview-frame39-camera.png` are stored alongside the viewer.

Headless Chrome at 1280 × 633, DPR 1 loaded the global 170,494-triangle mesh and the last anchor's six 285,824-triangle surfaces. Strict mode and LiDAR toggling loaded successfully. The page had one canvas and no page errors; WebGL used ANGLE/SwiftShader, so this is functional browser evidence rather than hardware WebGPU performance evidence. The camera preset shows connected road and object patches, together with large holes, seams, and visibly implausible geometry. The global overview spans the route but is not a complete physical scene. Moving objects can leave repeated or smeared surfaces.

Reproduce with an isolated environment containing the pinned MapAnything checkout and CUDA PyTorch stack:

```bash
CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/build_mapanything_scene_review.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 \
  --output .artifacts/model-review/mapanything-full-scene0061

# If interrupted, rerun the identical source and arguments with --resume.
python3 -m http.server 8769 --bind 127.0.0.1 \
  --directory .artifacts/model-review/mapanything-full-scene0061
```

Open `http://127.0.0.1:8769/`. Dataset-derived images and meshes stay local. The next geometry check is a pose-aligned multi-frame LiDAR surface, plus depth-distance, edge, and dynamic-actor strata on a separate scene; this review alone cannot authorize workbench integration.

# MapAnything visual review on nuScenes mini

## Decision

Keep MapAnything as an **offline, read-only visual experiment**. Its valid depth estimates on two nearby front-camera frames are substantially closer to LiDAR than the previous candidates, but they still lose to the nearest projected LiDAR return on the same held-out points. The output mask also leaves gaps. These two frames do not establish safety in LiDAR-empty regions, across camera roles, or through a scene. Do not attach this model to the annotation workbench or use its generated points for picking, masks, box fitting, measurements, or exports.

The local [visual review generator](../../apps/api/scripts/build_mapanything_review.py) produces a static [browser viewer](../../apps/web/scripts/mapanything-review-ui/index.html). It lets a reviewer switch frames, blend RGB and depth, inspect prompt and held-out-error marks, and rotate LiDAR and model point clouds in one coordinate system. The artifact never calls the project API or writes to the development database. The two-frame example lives under the ignored `.artifacts/model-review/mapanything-scene0061/` directory in the checkout where this test ran; it is not committed because it contains nuScenes-derived images.

## Sources and method

- Model: [MapAnything source](https://github.com/facebookresearch/map-anything) at `3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9`, using the [Apache checkpoint](https://huggingface.co/facebook/map-anything-apache) at `00f9c245bbcb60522d1ed7f9e9d88462c6e3f38a`. The script requires the pinned source checkout and weight revision.
- Data: official local nuScenes mini `scene-0061`, key frames 0 and 1, `CAM_FRONT`, 1600 × 900 source RGB. The model's preprocessor used 518 × 294 output pixels. Each frame uses its own sensor calibration and timestamped ego poses. This is more precise than the current importer contract that reuses first-frame calibration across a scene.
- Inputs: both RGB images, intrinsics, known camera poses, and sparse metric LiDAR depth. Returns are deterministically split before inference. Held-out returns colliding with a prompt cell after preprocessing are excluded. Frame 0 had 2,419 prompt cells and 613 held-out returns; frame 1 had 2,361 and 582.
- Baseline: nearest input LiDAR return in image coordinates, scored at the same held-out locations. The primary paired comparison restricts both model and baseline to pixels the model marks valid and with positive finite depth. The all-held-out comparison counts masked or invalid model depth as errors, to expose coverage loss. These are observed LiDAR locations, not an evaluation of newly filled holes.
- 3D review: valid model depth from a bounded 3-pixel grid and LiDAR returns inside the front camera's 1–80 m view are transformed to the first frame's ego reference. The optional “high confidence” view shows the upper half of the model's own confidence values, **not a calibrated correctness threshold**. Point cloud sampling is only for the viewer.
- Hardware: `CUDA_VISIBLE_DEVICES=1` exposed one NVIDIA RTX 3090; PyTorch `2.6.0+cu124`. App revision at generation was `0c48bc910`.

## Two-frame result

| Frame | Valid held-out / all | Model MAE on valid | LiDAR-nearest MAE on same valid | Model MAE on all | LiDAR-nearest MAE on all | Model pixel mask |
| ----- | -------------------: | -----------------: | ------------------------------: | ---------------: | -----------------------: | ---------------: |
| 0     |    521 / 613 (85.0%) |            0.881 m |                         0.423 m |          4.629 m |                  0.976 m |           60.72% |
| 1     |    507 / 582 (87.1%) |            0.412 m |                         0.183 m |          3.997 m |                  0.726 m |           62.08% |

The model's valid-only 90th-percentile absolute errors were 1.355 m and 0.975 m, versus 0.384 m and 0.337 m for LiDAR-nearest on the same valid points. The large all-held-out model MAE is mainly caused by masked-out predictions. It must not be presented as the error of the valid subset; the valid subset must not be presented as full coverage.

The cached model load took 23.4 s, and joint two-view inference took 0.82 s. PyTorch's peak reserved memory was 7,722 MiB. The first load/download was longer, and these numbers omit process-level VRAM, input preparation, artifact generation, browser painting, and application coexistence. This was CUDA inference, not a WebGPU renderer benchmark.

## Open the local review

Install the pinned MapAnything source into an isolated Python environment with its dependencies, then run from the repository root:

```bash
CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/build_mapanything_review.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 --frames 0,1 --camera CAM_FRONT \
  --output .artifacts/model-review/mapanything-scene0061

python3 -m http.server 8765 --bind 127.0.0.1 \
  --directory .artifacts/model-review/mapanything-scene0061
```

Open `http://127.0.0.1:8765/`. Use `?canvas2d=1` if the browser cannot create a WebGL context; the fallback remains rotatable but draws fewer points. The static page needs HTTP because it fetches local data files. The generated artifact is safe to remove after review and must not be published with the dataset-derived images.

## Next evidence

Run the existing qualification plan across all 39 frames and at least one separate held-out scene, including all camera roles, distance/edge/dynamic strata, per-view valid coverage, calibration sensitivity, and temporal reprojection. Compare accuracy against LiDAR at **matched valid locations** and measure newly visible surface coverage separately. Only after that, assess whether annotators finish representative tasks faster without increased geometry errors or rework. The current production gate in the [pretrained rendering plan](../plans/1790239103_pretrained-model-rendering-context.md) remains closed.

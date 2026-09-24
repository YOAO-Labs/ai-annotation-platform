# MapAnything six-camera surface review

## Decision

The six nuScenes cameras can be transformed into one local 3D reference and displayed as a **bounded, colored surface preview**. The browser now offers both a six-camera overview and a selected-camera view. The latter makes roads and building fronts visibly continuous; the overview exposes seams and missing regions. This is an offline visual review artifact, not a complete or watertight scene, and it remains outside annotation truth and the workbench renderer.

The generator is [`build_mapanything_review.py`](../../apps/api/scripts/build_mapanything_review.py); the static viewer source is [`mapanything-review-ui`](../../apps/web/scripts/mapanything-review-ui/index.html). The generated nuScenes-derived artifact is ignored under `.artifacts/model-review/mapanything-six-camera-scene0061/` in the local checkout. It contains six camera images, metrics, vertex/index buffers, and screenshots for review, but is not committed or published.

## Construction

Use the [same pinned Apache checkpoint and source revision](52-mapanything-visual-review.md) as the two-frame pilot. For key frame 0 of nuScenes mini `scene-0061`, run all six camera images jointly with each camera's intrinsics, its timestamped ego pose, and projected sparse LiDAR depth. The source LiDAR scan is the same key-frame scan; camera-to-LiDAR timestamps differ by about 0.5–43 ms. Model output depth is transformed through each camera pose to the first frame's ego reference.

For each 518 × 294 predicted depth map, sample a 2-pixel grid. Valid model pixels at 1–80 m become vertices with source RGB colors. Adjacent vertices form triangles only when all three are valid and their depth span is at most `0.25 m + 4%` of the nearest depth. A sampled vertex is rejected when it disagrees with an input LiDAR prompt cell by more than the larger of 1 m or 10%. This prevents the most obvious bridges across depth edges; it is not a geometric correctness guarantee. WebGL displays the resulting full preview mesh. The Canvas 2D fallback uses a coarser index set for responsive inspection.

Run in an isolated environment with MapAnything source at `3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9`, PyTorch `2.6.0+cu124`, and the checkpoint pinned in the script:

```bash
CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/build_mapanything_review.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 --frames 0 --cameras all \
  --output .artifacts/model-review/mapanything-six-camera-scene0061

python3 -m http.server 8766 --bind 127.0.0.1 \
  --directory .artifacts/model-review/mapanything-six-camera-scene0061
```

Open `http://127.0.0.1:8766/`. Select **从当前相机看** to inspect a continuous observed surface, **全局总览** for the six-camera assembly, and **聚合显示全部相机** to compare all cameras against one selected camera. The page can switch between surfaces and sampled points and optionally display original LiDAR. The six original camera images are shown below the 3D review.

## One-card result

| Camera      | Model valid pixels | Surface triangles | Valid held-out returns | Model MAE on valid | LiDAR-nearest MAE on same valid |
| ----------- | -----------------: | ----------------: | ---------------------: | -----------------: | ------------------------------: |
| Front       |              61.9% |            41,043 |              514 / 613 |            0.862 m |                         0.388 m |
| Front right |              62.3% |            40,490 |              448 / 618 |            1.274 m |                         0.803 m |
| Back right  |              68.0% |            41,630 |              506 / 696 |            1.415 m |                         0.852 m |
| Back        |              71.7% |            48,243 |              676 / 839 |            0.873 m |                         0.393 m |
| Back left   |              82.7% |            57,534 |              683 / 795 |            0.373 m |                         0.223 m |
| Front left  |              87.0% |            61,018 |              631 / 727 |            0.442 m |                         0.218 m |

There are 289,958 displayed triangles in total. The model loses to the nearest input LiDAR return in all six cameras at matched, model-valid held-out locations. Pixel validity is lower in the forward and front-right cameras. This single key frame cannot measure the correctness of surfaces where LiDAR has no return, surface continuity through occlusions, cross-camera seams quantitatively, or temporal stability. The six camera exposures are not exactly simultaneous, so moving actors can also disagree between views.

The successful run used one RTX 3090, joint six-view inference of 1.52 s, a cached model load of 22.34 s, and a PyTorch peak reserved memory of 7,884 MiB. These are offline CUDA measurements, excluding full process VRAM, model download, asset generation, browser render time, and coexistence with the app. An initial fresh environment resolved PyTorch `2.14.0+cu130` and failed while loading a cuDNN sublibrary; the completed run used the previously validated `2.6.0+cu124` stack.

## Browser evidence and boundary

The static viewer was checked in a headed local Chrome WebGL session: all six surfaces loaded, frame/camera selection updated the paired metrics, the front and rear camera presets showed contiguous observed road/building areas, and the console reported no errors. A separate headless browser lacked a WebGL context; its Canvas 2D surface preview loaded and remained usable. These are local UI checks, not a WebGPU or production workbench benchmark. The generated artifact includes `preview-surface.png` and `preview-overview.png` for review.

The result meets the requested **visual demonstration** of six-view aggregation. It does not meet the geometric or annotator promotion gates in the [pretrained rendering plan](../plans/1790239103_pretrained-model-rendering-context.md). The next evidence is a second scene, all-frame consistency, camera seam analysis, distance/edge/dynamic strata, and a measured annotator comparison against the existing LiDAR surface mode.

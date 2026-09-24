# Depth Anything 3 scene candidate review

## Decision

Two Depth Anything 3 candidates now have real nuScenes mini outputs and local browser review pages. Neither passes the [model-context geometry gate](../plans/1790239103_pretrained-model-rendering-context.md) on the sampled scene anchors. The pose-conditioned `DA3-LARGE-1.1` has more valid image pixels than the sparse-LiDAR-guided MapAnything review but scores **2.497 m** weighted MAE on its model-valid held-out LiDAR returns, versus **1.032 m** for the current-frame input-LiDAR nearest baseline on those same returns. The single-image metric model `DA3METRIC-LARGE`, after the official focal-length scale conversion, scores **2.811 m** versus **1.033 m**. All 18 frame/camera groups for each DA3 model lose to their own matched LiDAR baseline.

These are offline model experiments, not annotation assets. Different model-valid pixels, preprocessing and inputs prevent using those pooled figures as a direct same-point ranking against MapAnything. DA3 receives **no sparse LiDAR prompt**; MapAnything receives the anchor and neighbor prompts. The DA3 viewer's optional LiDAR conflict rule affects only the displayed surface, not the model-depth MAE.

## Method

Use key-frame anchors 1, 19, and 37 (zero based) of the 39-frame `scene-0061`, each with all six calibrated cameras. `DA3-LARGE-1.1` receives the anchor plus both adjacent RGB frames for each camera role, processed at 504-pixel resolution, with measured intrinsics and world-to-camera poses. Its predicted pose scale is aligned to the provided poses by the official API. `DA3METRIC-LARGE` receives one RGB frame and its intrinsics; its depth is converted to meters with `processed_focal × network_output / 300`, following the [official model FAQ](https://github.com/ByteDance-Seed/Depth-Anything-3#-model-cards). Neither run uses LiDAR for inference or post-hoc metric calibration.

The same deterministic original-image prompt/hold-out partition used in the MapAnything review is applied. A hold-out return sharing a processed prompt pixel is excluded, and each candidate is compared to nearest input-LiDAR depth at its own model-valid hold-out locations. Valid depth is finite, 1–80 m, and outside a model sky mask when available. Output pixels are back-projected with the processed camera intrinsics and measured camera pose. The [offline evaluator](../../apps/api/scripts/qualify_da3_scene.py) saves RGB, depth colors, and a triangle surface. Only the **visual surface** also rejects vertices that disagree with an input LiDAR raster; its metric is calculated before this rule. This makes the displayed holes visible without crediting the model for a LiDAR-corrected prediction.

Source code was pinned at `3d835ec1a5802d64a8b8b15f817a1ab54809bfe4`. Checkpoint revisions were `0e109ae307c5982f319a67cf6f9f99ccdc0ec97c` for `DA3-LARGE-1.1` and `4010e39f3634a45bc60553321fb49fb760bd594e` for `DA3METRIC-LARGE`. Both ran in an isolated Python 3.11 environment with CUDA PyTorch `2.6.0+cu124` on one RTX 3090. The [official API](https://github.com/ByteDance-Seed/Depth-Anything-3/blob/main/docs/API.md) defines the pose-conditioned interface; the pinned checkout controls this experiment's actual behavior.

## Results

The table pools the 18 per-view MAEs by their model-valid hold-out counts. Each per-view MAE was rounded to 0.001 m before pooling, so the pooled figures are approximate. Pixel coverage is an unweighted mean across 18 views.

| Candidate                              | Model-valid hold-out returns | Model MAE | Same-return input LiDAR MAE | Mean valid image pixels | Total 18-group inference | Peak PyTorch reserved |
| -------------------------------------- | ---------------------------: | --------: | --------------------------: | ----------------------: | -----------------------: | --------------------: |
| DA3-LARGE-1.1, three RGB + poses       |                       12,849 |   2.497 m |                     1.032 m |                  98.31% |                   4.80 s |             3,752 MiB |
| DA3METRIC-LARGE, one RGB + focal scale |                       12,829 |   2.811 m |                     1.033 m |                  92.43% |                   3.43 s |             2,238 MiB |

The model-load times in these cached-weight runs were 3.62 s and 3.23 s. The inference totals include the API's image processing and one warm-up group; they exclude model load, source image/LiDAR I/O, evaluation, surface generation, and browser rendering. PyTorch reserved memory does not measure whole-process VRAM or app coexistence. For context only, MapAnything's three corresponding anchors average **0.862 m** model MAE versus **0.591 m** current-frame LiDAR MAE on **10,372** of its own model-valid returns, at 72.3% mean fused pixel coverage. The return sets and inputs differ; these are separately matched baseline comparisons.

## Visual review and limits

The ignored local artifacts `.artifacts/model-review/da3-large-scene0061/` and `.artifacts/model-review/da3metric-scene0061/` each contain a [static review page](../../apps/web/scripts/da3-review-ui/index.html), 18 RGB/depth pairs, mesh buffers and `preview-front-camera.png`. The page switches among the three anchors and six roles, shows the depth picture and matched-return metrics, rotates one connected surface, or loads six surfaces for the selected anchor. Dataset-derived files stay local and outside the workbench.

Headless Chrome loaded the large candidate's front-camera mesh (55,498 triangles), the final anchor's six-camera aggregation (350,641 triangles), and the metric candidate's front-camera mesh (49,645 triangles), each with one canvas and no page errors. These are browser functional checks on a local static page; they do not qualify hardware WebGPU or the production point-cloud renderer. The camera view shows recognizable road/building/object patches, but LiDAR-conflict holes, depth discontinuities and likely incorrect object surfaces remain.

The [official model table](https://github.com/ByteDance-Seed/Depth-Anything-3#-model-cards) lists `DA3-LARGE-1.1` as CC BY-NC 4.0 while its [Hugging Face model page](https://huggingface.co/depth-anything/DA3-LARGE-1.1) currently declares Apache 2.0. That inconsistency would need resolution before commercial integration. The official table lists `DA3METRIC-LARGE` as Apache 2.0. This experiment does not publish either model as a product feature.

To reproduce with the pinned DA3 source installed in an isolated environment:

```bash
CUDA_VISIBLE_DEVICES=0 python apps/api/scripts/qualify_da3_scene.py \
  --model da3-large-1.1 \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 --anchors 1,19,37 --cameras all \
  --output .artifacts/model-review/da3-large-scene0061

CUDA_VISIBLE_DEVICES=0 python apps/api/scripts/qualify_da3_scene.py \
  --model da3metric-large \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --scene scene-0061 --anchors 1,19,37 --cameras all \
  --output .artifacts/model-review/da3metric-scene0061

python3 -m http.server 8770 --bind 127.0.0.1 \
  --directory .artifacts/model-review/da3-large-scene0061
```

Open `http://127.0.0.1:8770/`; use port 8771 and the metric artifact for the second page. Further comparisons require common valid returns, depth-distance/edge/dynamic-object strata, a pose-aligned multi-frame LiDAR baseline, and annotator evidence before deciding whether any model layer helps the workbench.

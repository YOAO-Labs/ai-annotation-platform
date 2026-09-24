# Pretrained depth as point-cloud visual context: initial qualification

## Decision

The two tested pretrained depth checkpoints are **not qualified** for generated 3D context in the annotation workbench. Both can infer on one RTX 3090, but their depth estimates on held-out nuScenes LiDAR returns are substantially worse than a simple LiDAR-nearest baseline in every sampled camera/frame. This does not establish whether a different model or a carefully gated generated layer would help annotators in areas with no LiDAR return. It does rule out wiring these checkpoints directly into the renderer on the strength of inference speed alone.

The follow-up implementation plan is [pretrained model rendering context](../plans/1790239103_pretrained-model-rendering-context.md). The read-only evaluator is [`qualify_depth_reconstruction.py`](../../apps/api/scripts/qualify_depth_reconstruction.py).

## Sources and candidates

- [Prompt Depth Anything](https://github.com/DepthAnything/PromptDA) accepts an image and a low-resolution sparse metric depth prompt. Its published evaluation targets include ARKitScenes and ScanNet++; the small checkpoint was released but the paper's benchmark used the large checkpoint. The [Transformers processor](https://huggingface.co/docs/transformers/model_doc/prompt_depth_anything) converts supplied prompt values from millimetres to metres. The [small checkpoint card](https://huggingface.co/depth-anything/prompt-depth-anything-vits) lists Apache-2.0.
- [Depth Anything V2 Metric Outdoor Small](https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Outdoor-Small-hf) is an image-only outdoor metric-depth checkpoint fine-tuned on synthetic Virtual KITTI 2. It does not directly consume LiDAR. An affine LiDAR fit was evaluated separately as a diagnostic.

The model card and repository support an inference feasibility hypothesis, not accuracy on this project's camera/LiDAR distribution. License and checkpoint provenance must be verified again before any redistribution or production deployment.

## Reproducible method

- Source: local official nuScenes mini `scene-0061`, frames `0,10,20,38`, all six key-frame cameras, native 1600 × 900 images. This is a 24 camera/frame sample from a 39-frame scene.
- Device: `CUDA_VISIBLE_DEVICES=1` exposed exactly one NVIDIA RTX 3090 to the process; PyTorch `2.6.0+cu124`, Transformers `5.15.0`. This is an offline CUDA inference test, not a WebGPU/browser rendering test.
- Application source revision for the test: `77222fcdc`.
- Geometry: project each frame's raw LiDAR into its camera using that camera's own nuScenes calibration and both sensor timestamps' ego poses. Keep the nearest return in each original image pixel; retain depths from 1 to 80 m. This per-frame source calibration is more accurate than the current importer contract that uses the first frame's calibration across a scene, so this pilot is optimistic for the present workbench.
- Deterministically reserve about one fifth of visible original image pixels as held-out returns. Remove held-out returns that collide with a prompt cell after reducing the sparse depth raster to 256 × 192. Compare predictions at held-out pixel locations only. Each model sees the RGB image; PromptDA also sees the remaining LiDAR prompt in millimetres as required by the processor.
- Baseline: use the depth of the nearest remaining projected LiDAR return in image space. The optional affine model calibration fits scale and offset on prompt pixels and scores only on held-out pixels.
- Metrics below are medians of 24 separate camera/frame MAEs, rather than one point-weighted average. The two models used the same 16,990 held-out returns. The checkpoint revisions were `6f3768a5383d95a904ec00d2a7ca3bd186f8797e` for PromptDA small and `fd2c22027eaf20374204f14099b8341e1925ad39` for outdoor small.

```bash
# Run from the repository root with torch, transformers, numpy and Pillow in an
# isolated environment. The nuScenes path is the local fixture, not the dev DB.
CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/qualify_depth_reconstruction.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --model promptda-small
CUDA_VISIBLE_DEVICES=1 python apps/api/scripts/qualify_depth_reconstruction.py \
  --nuscenes-root ~/.cache/ai-annotation-platform/nuscenes-mini/content \
  --model outdoor-small
```

If `XDG_CACHE_HOME` is set, replace `~/.cache` with that directory. This run used an existing isolated PyTorch virtual environment without modifying it. Downloaded checkpoints and raw JSON outputs were kept in temporary paths and deleted after the summary was recorded.
The evaluator pins the checkpoint revisions above by default; `--revision` is reserved for a separately reported comparison.

## Results

| Candidate                       | Median MAE | MAE after affine LiDAR fit | LiDAR-nearest median MAE | Camera/frames beating baseline | Warm inference median | Peak PyTorch allocation during inference |
| ------------------------------- | ---------: | -------------------------: | -----------------------: | -----------------------------: | --------------------: | ---------------------------------------: |
| PromptDA small                  |   15.238 m |                    8.392 m |                  0.967 m |                         0 / 24 |               80.9 ms |                                503.9 MiB |
| Depth Anything V2 outdoor small |    5.601 m |                    3.563 m |                  0.967 m |                         0 / 24 |               30.2 ms |                                290.7 MiB |

The LiDAR-nearest median of per-view 90th-percentile absolute error was 1.044 m. The corresponding values were 32.751 m for PromptDA and 11.422 m for outdoor small. First inference was slower than the warm medians (roughly 440 ms and 350 ms respectively). PyTorch allocation is **not** a process VRAM peak or complete scene-generation budget. Model load, image decoding, sparse raster construction, asset transfer, and browser painting are excluded from the inference times.

PromptDA small yielded near-zero or negative depth over parts of the first front image despite a metric prompt; its first-view median prediction at held-out returns was approximately 0.4 m versus approximately 10.7 m measured LiDAR depth. Its sparse prompt occupied only about 5% of the 256 × 192 prompt grid. This mismatch is consistent with a domain/prompt-density problem, but the test does not isolate its cause. The outdoor model gave plausible positive ranges yet remained less accurate than the LiDAR baseline even after fitting scale and offset.

## Interpretation and next evidence

Held-out LiDAR measures agreement **where LiDAR already has returns**. It cannot score invented surfaces in holes and may favor LiDAR interpolation in well-sampled regions. A dense model could still make an image look more continuous while introducing false geometry elsewhere. Therefore the geometric result blocks automatic use as annotatable truth, and a separate blind annotator study must test whether a clearly marked, non-pickable visual layer reduces work without increasing geometry errors or rework.

Before production integration, evaluate a candidate on the whole 39-frame scene and at least one separate scene, stratified by camera, distance, depth discontinuity, static/dynamic region and LiDAR density. Record temporal consistency, invalid/negative depth, cross-camera conflicts, full job wall time, true process VRAM peak and downstream browser render cost. Keep the final held-out scenes untouched during candidate tuning. If no candidate beats the LiDAR baseline and the annotator study shows no benefit, retain the current surface enhancement and stop this phase.

# Pretrained model rendering context for the 3D workbench

> Status: offline qualification in progress; production integration is blocked by the geometric gate

## Goal and boundary

Continue the three-stage rendering sequence agreed in the 3D workbench investigation. Phase 1, LiDAR-anchored surface enhancement, is implemented. This phase evaluates **pretrained depth inference without fine-tuning** and, only if evidence supports it, publishes a clearly marked visual context layer for the 3D workbench. Scene-specific 3D Gaussian optimization remains a separate later decision.

The user's target is easier 3D annotation on a single RTX 3090. Generated surfaces cannot become points used by picking, PointMask, measurements, box fitting, projection truth, annotation serialization or exports. The current `points` and `surface` display modes remain available as the safe fallback. The renderer remains one owner and one canvas. No model is run synchronously when an annotator opens a task.

## Current evidence

The phase-1 plan and real-project benchmark are in [point-cloud surface enhancement](1789979548_pointcloud-surface-enhancement.md). The performance promotion gate is still closed after hot-frame and panel optimization. Model context must be evaluated against that current baseline; historical timing thresholds cannot be silently redefined.

The first offline test used 4 frames × 6 cameras of nuScenes mini `scene-0061` on one RTX 3090. [The full method and numbers](../research/51-pretrained-depth-for-pointcloud-context.md) show that PromptDA small and Depth Anything V2 Metric Outdoor Small each lost to projected LiDAR nearest depth in all 24 views. Median per-view MAE was 15.238 m and 5.601 m, against 0.967 m for the LiDAR baseline. Even prompt-fitted affine calibration did not close the gap. Warm inference on one camera was feasible, but that is insufficient to authorize renderer integration. The read-only qualification script is in `apps/api/scripts/qualify_depth_reconstruction.py`.

A two-frame, sparse-LiDAR-guided MapAnything pilot now has a [local human review page and paired results](../research/52-mapanything-visual-review.md). On model-valid held-out points, its MAE was 0.881 m and 0.412 m; the nearest LiDAR input scored 0.423 m and 0.183 m at those same locations. Across all held-out points, model MAE rose to 4.629 m and 3.997 m because some predictions were masked out. This is a useful candidate for wider evaluation, not a promotion: two adjacent front-camera frames do not cover the required camera, scene, temporal, or annotator gates.

The subsequent [six-camera surface review](../research/53-mapanything-six-camera-surface-review.md) combines one key frame into a colored, rotatable 3D mesh preview. All six model-valid held-out MAEs remain above the matched LiDAR-nearest baselines; per-camera valid pixel coverage ranges from 61.9% to 87.0%. A selected camera view looks surface-like, while the overview exposes holes and seams. This meets the visual experiment request but does not open the production gate.

## Work packages

### 1. Candidate qualification

1. Extend the offline evaluator to all 39 frames and at least one separate scene. Preserve fixed held-out scenes for final testing, separate from model/prompt tuning. Evaluate camera roles and depth bands (1–20, 20–40, 40–80 m), depth edges, dynamic actors, LiDAR density, and temporal reprojection consistency. Record prompt occupancy and camera/LiDAR timestamp offsets.
2. Check model input contracts explicitly: prompt units, resizing, sparse mask behavior, invalid depth and model output scale. PromptDA's original evaluation domain and the current sparse car-mounted LiDAR prompt differ; test improved prompt construction only on the tuning split. Compare an outdoor depth candidate with LiDAR anchoring and the existing LiDAR-only surface mode. Model checkpoints and dependency versions must be pinned in the report.
3. Report total scene-generation wall time, image I/O, preprocessing, cold and warm model latency, process VRAM peak and failure recovery on one 24 GB card. Benchmark under realistic coexistence with the app's GPU workload. Inference-only CUDA allocation is not this measure.
4. Do not progress a candidate solely on RGB appearance or depth metrics. A blind within-subject study with 3–5 annotators should compare `points`, `surface` and a visibly marked model layer on identical vehicle, pedestrian and distant-object tasks. Record median task time, box geometry, 2D projection review, rework and QA findings.

Qualification to begin production integration requires: no model-generated layer participating in annotation truth; accepted generated samples matching or improving the no-model depth baseline on final held-out distance/edge strata while reporting the accuracy-versus-coverage tradeoff; a measured increase in useful visual coverage beyond the current `surface` mode; no material long-range, dynamic-object or camera-role regression; acceptable temporal consistency; one-card generation and browser budget measured end to end; and at least a 20% median annotation-time improvement without worse geometry or rework. Full-frame model output would need to beat the baseline in at least five of six camera roles before it could be displayed without confidence filtering. If these conditions fail, retain phase 1 and record the rejection rather than weakening the gates.

### 2. Asset generation, conditional on package 1

Use a scene-level asynchronous job; do not put render assets into `Prediction` or run inference from the API request path. Freeze an input digest covering point-cloud and camera object content, calibration revision/digest, ego poses, model/weight revision, preprocessing and filtering parameters. Recheck the source fence before publishing. Reuse the image-pyramid pattern of immutable generation directories and atomic active-generation switching. A stale or failed generation must never be served as current.

Start with a bounded per-frame context asset, not an unbounded full-scene point set. Reproject accepted depth pixels through the exact camera calibration to the LiDAR/ego frame, apply near/far and invalid-depth filters, suppress pixels that disagree with measured LiDAR, and remove surfaces that fail cross-frame or cross-camera consistency. Keep provenance and confidence per generated sample. Do not convert model depth into database annotations. A failed job leaves the prior active generation usable only when its source digest still matches; otherwise the workbench falls back to `surface` or `points`.

Provide scene/task authorization and short-lived asset URLs. Use the existing GPU admission controls or a dedicated reconstruction queue with a one-card concurrency cap, cancellation, retries and observability. The detailed schema/API and an ADR are required when qualification opens this package; do not add a table or new user setting during the offline research phase.

### 3. Workbench layer, conditional on package 2

Add a model-context option as a separate, opt-in visual layer with model/version, source frame range, generation state and low-confidence marking visible to the annotator. `points` remains the compatibility default. Selecting an object reduces or hides generated opacity so the LiDAR and camera projections can be checked. Keep orthographic editing views on original LiDAR until their own qualification is complete.

Load only the active frame's bounded asset through the existing scene/cache owner. Add it to the same renderer scene as a non-raycast layer with independent disposal, clipping and device-loss fallback. Generation changes, task/scene switches, calibration edits and stale fetch completions must clear or reject the old layer. Point indices and saved payloads must remain byte-equivalent with the model layer on and off.

### 4. Verification and documentation

- Unit/integration tests: exact source digest and revision invalidation, generation publication race, authorization, bounds, stale response, disposal, fallback, annotation/pick equivalence and no model points in PointMask.
- Browser tests: strict WebGPU and Legacy behavior on a real project, one renderer/canvas, zero forbidden WebGPU readbacks, tri-view/edit behavior, task switch, model failure and device-loss fallback.
- Run the maintained point-cloud benchmark in `points`, `surface` and model-context modes using the same fixture, backend, viewport, cache-state definitions and current repository gates. Report cold, adjacent-warm and fully-warm samples separately, plus asset request latency and process VRAM. Do not promote while the existing phase-1 gate remains unmet or the added layer regresses interaction.
- Update the point-cloud user guide, developer render-asset concept, relevant API documentation, ADR and Unreleased changelog in the implementation commit. Clean all generated test annotations, traces, model trial caches and temporary environments owned by the run.

## Decision points

The offline evaluator, initial 24-view result, and MapAnything point and six-camera surface reviews are completed first slices. The next slice is candidate diagnosis and a held-out multi-scene qualification, reporting valid-output coverage alongside paired depth error and camera seam quality. Production asset generation and workbench wiring are explicitly contingent on that result. If direct pretrained depth remains inaccurate, do not treat scene-specific 3DGS as a drop-in fix: it requires a separate plan for scene optimization time, actor separation, calibration quality, asset lifecycle and annotator benefit.

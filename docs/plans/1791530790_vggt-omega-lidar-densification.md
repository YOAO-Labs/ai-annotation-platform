# VGGT-Omega and LiDAR point-cloud densification

Status: completed offline pilot; 12 official model forwards, exact-array
reproduction, acquisition-heldout geometry comparison and CPU Canvas browser
review completed. Production geometry qualification remains closed.

## User objective and scope

Increase useful point density around observed scene surfaces using synchronized
camera views, continuous frames and measured LiDAR. Judge metric geometry,
newly supported coverage and dynamic-object consistency. RGB sharpness and
Gaussian count cannot establish success for this objective.

The user requested multi-frame consideration, nominated VGGT-Omega, and reports
having checkpoint download access. Persist this priority in the
[main investigation](1790239103_pretrained-model-rendering-context.md).
Model-generated samples remain a separate, identifiable candidate layer; preserve
original measurement positions, identities and annotation behavior.

## Verified upstream contract

- Official source: [facebookresearch/vggt-omega](https://github.com/facebookresearch/vggt-omega),
  reviewed main revision `48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6`.
- The [project](https://vggt-omega.github.io/) reports improved reconstruction on
  static and dynamic scenes. This supports trying it, not a guarantee of accurate
  densification on our driving sequences.
- The public inference model consumes images and returns camera encoding,
  per-view depth and confidence. Its public `forward(images)` interface does not
  accept LiDAR prompts or calibrated-camera priors, and does not return explicit
  scene flow or object trajectories. Plan LiDAR correction and motion handling
  as downstream processing; do not describe them as existing model features.
- Official weights: [facebook/VGGT-Omega](https://huggingface.co/facebook/VGGT-Omega).
  The README recommends `VGGT-Omega-1B-512` for practical video applications;
  [reproduction guidance](https://github.com/facebookresearch/vggt-omega/blob/main/reproduction.md)
  requests `VGGT-Omega-1B-416-Reproduction` for benchmarking. Use the reproduction
  checkpoint for quantitative comparisons. A 512 run may be retained as a
  separately labeled practical candidate, without making benchmark claims from
  that checkpoint. Pin actual weight revision and SHA before inference.
- Public model metadata was inspected at revision
  `1041e80fc0e911235d3426b0a3d9a81075111a53`. The 416 reproduction file is
  4,576,703,488 bytes with SHA256
  `e900833330eb0e570e10543e6108d0dd704fa7c8dbc32738a645987875298896`;
  the 512 file is 4,576,706,117 bytes with SHA256
  `c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934`.
- HF CLI authentication is now available. The authorized download succeeded and
  its SHA256 matches the pinned 416 reproduction checkpoint above. No account
  token is recorded in experiment artifacts.

## Implemented pilot protocol

The executable protocol was frozen before model inference at
`.artifacts/model-review/vggt-omega-lidar-densification/protocol.json`.
Use `scene-0061` for development and `scene-0103` for validation, centres 8, 19
and 30 (zero-based), with three- and five-keyframe windows around each centre.
Both scenes appeared in previous research; validation is not an unseen-dataset
claim. There are 12 forwards of 18/30 images, with centre `CAM_FRONT` first in
both window sizes. All centre RGB exposures are inputs; the complete centre
LiDAR scan is held out. The single measurement baseline is the preceding scan;
the temporal baseline contains the other two/four scans. Neither reads centre
LiDAR. Sensor transforms use each acquisition's own ego pose.

Official balanced preprocessing at 416 yields 560×320 for these 1600×900 inputs.
Use integer array-index pixel centres throughout, with the PIL resize affine
`cx'=(cx+0.5)*sx-0.5`, and the analogous vertical expression. Do not combine
this K with the upstream training helper's `u+0.5` unprojection convention.

Scale is a robust median log ratio, with one median vote per scan-token/raw-row
identity across projected observations. Local log residuals use eight neighbours
within 24 pixels, at least two passing endpoint RGB/depth gates, and a ±log(1.3)
bound. Endpoints with similar colour/depth may still lie on different surfaces;
these gates are not a strict connected-edge barrier. Unsupported regions retain
only the global scale.

Static samples require one same-camera observation from another acquisition
within max(0.4m, 3% depth) and a 2px cycle, or direct real-anchor agreement within
max(0.3m, 3% depth). Visible model conflicts and observed LiDAR free-space
conflicts veto acceptance. The LiDAR veto was added during implementation audit
before the complete CPU comparison; all windows are recomputed with it, without
changing frozen thresholds. Same-window predictions supply consistency evidence,
not independent model estimates. All vehicle, human and movable-object oracle
boxes exclude actor rays from static fusion. Dynamic samples come only from the
centre RGB exposures, whose time offsets remain explicitly reported.

All branches retain original source representatives in 0.1m voxels. Score the
actual output clouds through a common 1.5px projection footprint. Each visible
centre-scan raw return is assigned to one fixed centre camera, then counted once.
Report fixed-denominator correct depth support at 0.2/0.5/1m, own/common-support
errors, additions/losses relative to temporal LiDAR, RGB-edge and distance strata.
Sparse holdout 3D proximity is a proxy, not generated-surface precision.

The initial protocol captures scaffold source hashes. Final inference wrappers,
geometry helpers and their imports are additionally archived and checked by
exact-array reproduction, while keeping the initial prediction NPZ files and
their recorded hashes unchanged. This distinguishes protocol freezing from later
implementation formatting and audit fixes.

## Pipeline rationale

1. **Freeze acquisition identity and calibration.** Use original RGB, calibrated
   intrinsics/extrinsics, ego poses and exposure timestamps. Preserve the exact
   resize/crop affine when transferring LiDAR and rays to the model grid. Begin
   with bounded continuous windows of three frames, then five; six camera views
   per time step are 18 and 30 exposures. Do not present different camera views
   as consecutive video times. Retain camera identity and time for every output.
2. **Predict per-exposure dense geometry.** Use model depth and confidence as a
   prior. Keep predicted camera parameters for diagnostics. In the calibrated
   densification branch, correct per-pixel depth and backproject with the known
   processed K and sensor pose. Do not freeze a field generated with predicted
   rays and then force it to fit a different camera, as in the AnySplat probe.
3. **Anchor before fusion.** Project only allowed real LiDAR into each exposure
   with visibility and timing checks. Fit a robust positive metric scale, then
   evaluate a bounded smooth depth correction anchored to measured samples.
   Keep corrections separated across depth edges, occlusions and moving objects.
   Shared window constraints should limit frame-to-frame scale flicker. Sparse
   anchors cannot prove accuracy on unsupported surfaces.
4. **Fuse static geometry.** Transform accepted static samples with the known
   poses. Require cross-view/time reprojection and occlusion consistency, then
   use bounded voxel/surfel consolidation. Introduce TSDF only if it improves the
   measured coverage/error tradeoff. Reject visible conflicts with LiDAR; avoid
   treating confidence as a calibrated probability without validation.
5. **Handle moving objects at the output time.** Preserve a per-time dynamic
   layer. Accumulate rigid tracked objects only after object-motion compensation;
   without reliable motion, keep the current-time reconstruction. Ego-pose
   alignment alone must not fuse moving cars or pedestrians into static geometry.
   Record whether diagnostic masks/tracks use ground truth or deployable inputs.
6. **Recheck geometry after fusion.** Measure residuals against independent
   LiDAR, inspect object boundaries and temporal trails, and expose original
   measured points plus accepted generated samples with provenance. Retain frame,
   camera, confidence and support for each generated sample. Claim completeness
   only over the observed and accepted surface region, not hidden surfaces.

LiDAR therefore participates before and after fusion. Post-fusion global Sim(3)
or ICP may initialize/check alignment, but cannot substitute for the earlier
depth, visibility and motion constraints.

## Evaluation design

- Compare identical observation windows and point identities for: preceding-scan
  measured LiDAR; pose-aligned multi-frame LiDAR; globally scaled model depths;
  LiDAR-corrected model depths; and the corrected temporal fusion. This separates
  the benefit of collecting more real observations from learned completion.
- Measure **new useful coverage at fixed metric error tolerances**, paired
  independent-LiDAR error, precision/completeness, distance/edge strata, dynamic
  trails and temporal consistency. Report support losses and rejected samples.
  Total point count, point size and surface smoothness are insufficient metrics.
- Keep calibration LiDAR disjoint from scoring LiDAR. Exclude a heldout target
  acquisition from all correction anchors and LiDAR baselines when scoring that
  acquisition. The target RGB can be an input for current-frame densification;
  explicitly distinguish this task from the earlier heldout-RGB novel-view test.
- Use one development sequence and a separate final sequence selected before
  fitting correction/filter parameters. Report static and dynamic results
  separately. Do not extrapolate a static-background result to moving actors.
- Run in a private environment on physical GPU 1, measure actual 3090 memory and
  latency for the selected image grid/window, and preserve source/weight/input
  hashes. Upstream A100 memory figures do not establish local capacity.
- Keep endpoint point assets, metrics, rejected candidates and review evidence.
  Clean task-owned environments, downloaded checkpoint copies, input caches and
  test intermediates after reproducible evidence is preserved.

## Completion boundary

All 12 corrected point-cloud comparisons and the final source/array checks are
complete. On 87,065 fixed static returns, three-frame fusion raises 0.5m correct
depth support from 25.76% to 49.05%; five-frame fusion raises it from 34.47% to
50.20%. Common-support MAE worsens (three frames 1.067m to 1.237m; five frames
1.327m to 1.491m). New coverage is supported near sparse targets; arbitrary new
voxels are not validated surfaces. The three-frame prototype is the default.

Chrome 150 browser interaction passed using Canvas2D CPU fallback after WebGL
context creation failed on this host. Rotation/zoom, scene/window/method changes,
source layers and all six RGB images were checked; this does not certify the
production renderer. Preserve endpoint point/depth assets, measurement identity,
source snapshots, metrics and browser evidence. Task-owned environment,
checkpoint copies and testing intermediates are removed after qualification.

See [the measured report](../research/68-vggt-omega-lidar-densification.md).
This pilot does not qualify every generated surface or production annotation
geometry. Production changes remain governed by the main investigation's
qualification contract.

## Authorized RGB renderer extension

The user subsequently requested a mesh or 3DGS renderer. The pilot now includes
an original-RGB textured triangle mesh, using the exact saved filtered vertices
without position optimization. Per-exposure/per-kind triangulation checks the
original acceptance mask, pixel edge, world edge and depth discontinuity.
Static-only, actor-only and all-layer RGB/depth renders are retained for all
12 windows, plus a 0.5m displaced source-camera visual probe without RGB truth.

Static and actor scoring use separate same-kind depth buffers. The initial
all-layer scoring implementation is preserved with its source/protocol and an
explicit frozen amendment; repair retains identical vertices, UV, triangles and
targets. The browser supports mesh/point switching and calibrated source cameras,
clears prior control inertia on camera snaps, and hides stale geometry while
loading another window. CPU triangle rendering is actually validated; WebGL
context creation remains unavailable on this host. Holes remain intentional and
novel-view photorealism is not established by source-image reprojection.

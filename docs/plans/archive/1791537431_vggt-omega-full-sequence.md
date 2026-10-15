# VGGT-Omega full scene reconstruction

> Retention update: offline trial executables and previews are retired. Bulky derived arrays and source copies were removed; frozen protocols, metrics, failure records and source snapshots remain. See the [current cleanup boundary](../../research/README.md#试验归档与清理). Historical commands below are not active runtime entrypoints.

> Status: archived; offline joint reconstruction completed; production qualification remains open.
> Remaining work: [consolidated route and qualification gates](../1791963159_pointcloud-densification-route.md).

## Requested outcome

Run every keyframe and all six cameras of one annotation scene through VGGT-Omega together. Provide an additional interactive preview resembling the upstream point-cloud demo, including a camera trajectory, RGB frame navigation, and an honest comparison with LiDAR calibration.

## Implementation

- Preserve the existing frozen short-window experiments and their source manifests.
- Freeze the complete scene-0061 keyframe chain, camera exposures, original hashes, upstream commit, 512 application checkpoint, and preprocessing before inference.
- Attempt one joint forward pass on physical GPU 1, using optimized SDPA only. Record failure before any fallback; never silently replace joint inference with independent windows.
- Separate predicted geometry, calibrated static geometry, measured returns, and frame-local actors. Use oracle actor masks explicitly; do not claim motion compensation.
- Globally exclude keyframe LiDAR scans 8, 19, and 30 from calibration, fusion, and filtering. RGB inputs remain available. Evaluate geometry against these acquisition-heldout scans with a fixed point footprint and report sparse-target limitations.
- Preserve full-resolution model predictions and source provenance. Any viewer level of detail changes display density only.
- Add a standalone full-sequence preview under the existing offline review server, with rotation, timeline, six cameras, original RGB, rendered RGB, and trajectory controls.

## Verification

Check that all scene keyframes and six cameras appear exactly once in the joint input; inspect actual GPU memory and runtime. Audit saved point provenance and heldout separation, exercise the viewer in an isolated browser, review changed sources, and run git diff --check. Remove this run's temporary environment, checkpoint copy, browser session, and test caches after retaining the reproducible evidence.

## Boundaries

This remains an offline experiment. Camera calibration and oracle actor boxes are dataset metadata. Rendered point colors come from the input RGB images. Neither point rendering nor sparse LiDAR agreement proves unseen-view photorealism or dense geometric correctness.

## Outcome

All 234 keyframe RGB exposures completed one joint forward with the 512 application checkpoint, balanced input resolution 480 (640 by 352), on physical GPU 1. The two preceding 688 by 384 attempts ran out of memory; their protocols, source snapshots and failure records remain intact. Forward time was 59.993 seconds and postprocessing plus six-camera RGB rendering took 430.1 seconds.

The calibrated static asset contains 4,168,067 points and the measured-priority fused asset contains 4,333,428. All 234 exposures contribute retained static or frame-local actor geometry. Geometry from three actor-filled exposures is intentionally absent from the static layer.

Three globally excluded scans supply 38,461 unique static validation targets. Fused 0.5m correct support is 45.35% versus 36.35% for the measured baseline, with 7,162 newly correct and 3,698 lost correct targets. Long error tails and reduced tight-tolerance hits on the common subset remain documented; this does not promote generated geometry to annotation truth or establish a single-variable advantage over the earlier 416 short-window trial.

The standalone preview is served at http://localhost:8780/full-sequence/. Source/provenance auditing, 13 CPU contracts, 23 browser behavior checks, six-camera projection checks and documentation build passed. Actual browser rendering used Canvas2D; WebGL hardware qualification is still unavailable. The existing short-window and mesh evidence remains unchanged.

- Maintained results: [research report](../../research/69-vggt-omega-full-sequence.md).
- Developer documentation: [point-cloud route](../../../docs-site/dev/reference/point-cloud-data-model.md#vggt-ω-点云增密主路线); CHANGELOG: Unreleased experiment entry.
- Remaining qualification and integration work: [consolidated plan](../1791963159_pointcloud-densification-route.md).

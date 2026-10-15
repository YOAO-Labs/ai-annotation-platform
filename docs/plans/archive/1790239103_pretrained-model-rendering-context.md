# VGGT reconstruction route for the 3D annotation workbench

> Status: archived; route-selection and cleanup session completed; the three-frame default is historical.
> Remaining work: [consolidated route and qualification gates](../1791963159_pointcloud-densification-route.md).

## User decision and current scope

The user selected the VGGT family after reviewing the local experiments. This
turn consolidates and cleans the experiment files, retaining the complete VGGT
route. The user explicitly chose cleanup and route selection before production
workbench integration.

The primary objective is useful point-cloud densification. The selected route is
continuous multi-frame, six-camera VGGT-Omega depth prediction, followed by real
LiDAR scale/local-depth correction, visibility and temporal filtering, and
provenance-preserving dense points plus an original-RGB textured mesh. Three
frames are the current default; five frames remain a recorded comparison.

## Retained implementation and evidence

- [Pilot protocol](1791530790_vggt-omega-lidar-densification.md).
- [Measured results and route decision](../../research/68-vggt-omega-lidar-densification.md).
- [Maintained developer entry](../../../docs-site/dev/reference/point-cloud-data-model.md#vggt-ω-点云增密主路线).
- `qualify_vggt_omega.py`, `omega_densification.py` and `vggt_common.py` own the
  input protocol, inference, shared geometry and source verification.
- Point and mesh builders, independent artifact auditors, CPU contracts and
  `apps/web/scripts/vggt-omega-review-ui/` remain executable.
- `serve_vggt_review.py` serves the retained review assets on port 8780.
- Historical source snapshots retain their original bytes and dependency names.
  The separate consolidation manifest identifies the current runtime and checks
  the unchanged extracted functions. Cleanup does not imply new GPU inference.

Other model trial source, tests, UIs, reports, endpoint artifacts and preview
services are removed. Shared clones, global model caches, original datasets and
production import tools remain outside the cleanup scope.

## Evidence boundary

Twelve fixed windows completed whole-scan LiDAR holdouts. Three-frame point
fusion increases 0.5m correct depth support from 25.76% to 49.05%, with common
support MAE changing from 1.067m to 1.237m. Coverage gain does not validate all
new surfaces. Static actor separation currently uses oracle boxes; actor
samples use centre exposures with timestamp offsets and no motion compensation.

Texture meshes reuse exact saved positions and retain unsupported holes. Their
actual pixel coverage differs from the point-cloud 1.5px query footprint.
Calibrated-source RGB projection and the displaced visual probe provide display
evidence; the probe has no independent RGB truth. The current browser evidence
uses a CPU fallback because this host could not create a WebGL context.

## Follow-up integration sequence

1. Define scene-level asynchronous generation and bounded per-frame point/mesh
   assets. Preserve acquisition, calibration, model revision and input digests;
   publish immutable generations through an atomic active-generation owner.
2. Reuse platform GPU admission, cancellation and retry controls. Keep generation
   outside task-opening requests, with a usable original-point fallback.
3. Add an opt-in VGGT auxiliary layer to the existing renderer/cache owner, with
   independent disposal and task/frame/calibration invalidation. Preserve original
   LiDAR point identities for picking, segmentation, measurement and annotation
   persistence. Show generated provenance and dynamic limitations in the review.
4. Verify real hardware rendering, active-frame budgets, generation races,
   annotation-payload equivalence and annotator benefit on representative scenes.
   Replace oracle masking with deployable inputs before claiming automatic use.

## Outcome

- Retained route: VGGT-Omega, LiDAR correction, filtered dense points and textured meshes.
- Developer documentation: `docs-site/dev/reference/point-cloud-data-model.md`.
- Research record: `docs/research/68-vggt-omega-lidar-densification.md`.
- Changelog: Unreleased route consolidation and cleanup entry.
- Remaining work: production asset lifecycle and annotation workbench integration.

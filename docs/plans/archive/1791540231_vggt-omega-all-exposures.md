# VGGT-Omega all-exposure comparison

> Retention update: offline trial executables and previews are retired. Bulky derived arrays and source copies were removed; frozen protocols, metrics, failure records and source snapshots remain. See the [current cleanup boundary](../../research/README.md#试验归档与清理). Historical commands below are not active runtime entrypoints.

> Status: archived; offline comparison completed; retain the negative static result and oracle-motion limits.
> Remaining work: [consolidated route and qualification gates](../1791963159_pointcloud-densification-route.md).

## Objective

Reconstruct every one of scene-0061's 1,318 distinct RGB exposures with overlapping windows. Fuse metric static geometry using known sensor calibration and compare key-only versus all permitted LiDAR anchors. Keep dynamic instances at a common real timestamp and compare single exposure, uncompensated short-neighbourhood accumulation, and rigid motion compensation.

## Frozen design

- Use the pinned official 512 checkpoint, balanced 480 preprocessing (640 by 352), physical GPU 1 only, BF16 and Flash/efficient SDPA without math fallback.
- Windows contain 144 chronologically sorted distinct exposures and advance by 72. Add a full final window ending at the last exposure. Select one output owner per exposure by greatest index distance from the window edges, breaking ties toward the earlier window. Duplicate context is never counted as independent temporal support.
- Preserve all 382 LiDAR identities. Exclude scans within 100 ms of any of the three previously held-out key scans globally. Reuse the previous 38,461 static targets unchanged.
- Both calibration branches use up to four nearest permitted scans within one second of each exposure. Per-window scale uses unique raw-return identities. Bounded local correction, actor ray masks, model consistency, measured free-space checks, 0.1 m sample-preserving voxels, and measured-first fusion follow the existing pilot rules.
- Actor association uses instance tokens and unique box membership. Motion requires bracketed annotation timestamps with gaps no greater than 0.75 s; do not extrapolate or normalize by box size. Rigid vehicles may use a symmetric 100 ms neighbourhood. People and articulated vehicles use one exposure per instance.
- Freeze source IDs before the uncompensated/compensated comparison. Apply the same selection, reference camera visibility rule, voxel size and colour policy. Never average RGB or coordinates across object boundaries.
- Dynamic evaluation uses fixed held-out raw LiDAR surface returns at the scan acquisition timestamps, grouped by instance and scan. Box-centre agreement is not a surface-quality metric. Report sparse proximity proxies with their limitations.

## Deliverables and checks

- Separate offline preview, with full-scene static geometry, actual asynchronous camera times, switchable dynamic modes, timeline and model-generated RGB point rendering.
- Immutable protocol, input/metadata/weight/source hashes, window predictions, calibration provenance, clouds, target scores, browser evidence and concise research documentation.
- Meaningful CPU contracts for ownership, identity, strict interpolation, rigid transforms, voxel selection and exclusion. Independently audit sampled output provenance and leave-out separation. Validate actual browser interaction and record the real graphics backend.
- Keep the earlier 234-image result unchanged. Remove this run's private virtual environment and temporary test products after preserving evidence; retain experiment assets and shared caches.

## Interpretation boundaries

This is an offline oracle-box study, not a production tracker qualification. Temporal selection cannot remove motion blur already present in RGB, guarantee nonrigid correspondence, fix occlusion, or recover per-point LiDAR acquisition times absent from this dataset. Changing window context and calibration scan count prevents attributing every difference to extra RGB alone.

## Outcome

- Completed all 18 window forwards for 1,318 distinct exposures, then both static calibration branches, three dynamic modes, 224 actual front-camera reference timestamps and 672 RGB point renders. The separate preview is available at `http://localhost:8780/sweeps-sequence/`.
- All permitted LiDAR provides 1,677,518 static measured points. The calibrated model and fused cloud contain 14,745,908 and 15,279,859 points. Fixed static 0.5 m correct support declines from 50.65% measured to 38.56% fused. Preserve this negative result prominently; increased density is not increased reliable coverage.
- Rigid motion compensation improves moving-return support at 0.1 m from 87.43% to 95.90%, but 695 of 756 returns belong to one vehicle and instance-scan macro 0.2 m support declines. Nonrigid actors use one exposure in all modes. This does not establish sharpness or general motion accuracy.
- Passed 22 CPU contracts, Ruff, CSS token lint and the documentation build. Passed 24 browser state checks and 10 native interaction checks, including all-reference playback to the end, with zero final unexpected errors. The actual backend is Canvas2D CPU in HeadlessChrome 150, not a qualified production GPU renderer.
- Independent audit passed 98,000 static provenance samples, 94,920 dynamic samples, 1,680 fresh static projections and 825 dynamic group-mode scores. Verified the global 11-scan exclusion, exact preservation of measured attributes, the previous key-only baseline and the previous runtime source fence.
- Retain the frozen protocols, executed source snapshots, predictions, provenance, sparse targets, audit reports and browser evidence. The final artifact manifest and cleanup record are stored in the experiment directory. Remote CI was not run.

- Maintained results: [research report](../../research/70-vggt-omega-all-exposures.md).
- Developer documentation: [point-cloud route](../../../docs-site/dev/reference/point-cloud-data-model.md#vggt-ω-点云增密主路线); CHANGELOG: Unreleased experiment entry.
- Remaining qualification and integration work: [consolidated plan](../1791963159_pointcloud-densification-route.md).

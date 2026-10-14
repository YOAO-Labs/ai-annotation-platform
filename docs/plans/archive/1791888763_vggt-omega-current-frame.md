# VGGT-Omega current-frame annotation comparison

> Retention update: offline trial executables and previews are retired. Bulky derived arrays and source copies were removed; frozen protocols, metrics, failure records and source snapshots remain. See the [current cleanup boundary](../../research/README.md#试验归档与清理). Historical commands below are not active runtime entrypoints.

> Status: archived; offline comparison completed; retain the selected-cloud versus preselection-depth distinction.
> Remaining work: [consolidated route and qualification gates](../1791963159_pointcloud-densification-route.md).

## Objective

Evaluate current-frame densification after the all-exposure reconstruction increased annotation ambiguity through missing actors and ghosts. Keep the same scene, 39 annotation times, 234 camera images, three diagnostic times and three previously frozen actor identities.

## Frozen comparison

- Current measured LiDAR is the baseline and remains a separate, complete display layer.
- Traditional comparison adds the nearest previous and next annotation scans using ego poses, with no model or oracle object compensation.
- True single-image inference uses six independent sequences (`B=6, S=1`), with fixed-batch input-independence checks. Separate `B=1, S=1` calls remain a numerical diagnostic, not an equivalence claim. The six-camera comparison uses `B=1, S=6` at each annotation time. Neither uses another time's images or accumulates model output across time.
- Use the previous pinned 512 checkpoint, 640 by 352 balanced preprocessing, and physical GPU1 exclusively.
- Calibrate against a deterministic approximately 80% partition of the current scan. Reserve the complementary approximately 20% by scan and raw-return identity before inference. Every input-dependent processing step uses only the training partition. Display keeps all measured returns, while evaluation excludes heldout returns from every current measurement layer.
- Preserve real measurements and provenance. Model candidates use known calibration, bounded local depth correction, confidence/range gates, sample-preserving voxels and current-measurement ray protection. No annotation boxes, actor-specific minimum anchor count or true actor trajectories participate in candidate selection.

## Verification and interpretation

Freeze code and protocol before inference and construction. Evaluate the same withheld returns across methods at old times 8, 19 and 30, report static/actor/diagnostic subsets, new and lost correct support, outliers and instance boundary proxies. Ground-truth boxes are evaluation labels only. The within-scan holdout measures completion and must not be numerically conflated with previous whole-scan holdout results. Sparse supports and box extents do not certify dense surface accuracy or annotation productivity.

Use identical per-camera scale and local-correction freedom for both model groups. Define target visibility from the full scan on the evaluation side before intersecting frozen withheld rows. Report training-ray distance strata up to 1.5 pixels, 1.5 to 3 pixels and beyond 3 pixels to make the protection rule's effect explicit. A read-only protocol audit confirmed the independent batch geometry in upstream code.

The first inference attempt stopped before scoring because numerical differences between `B=6,S=1` and separate `B=1,S=1` BF16 kernels exceeded an initial 2% P99 relative tolerance (observed 3.445%). Its frozen inputs, code and failure log remain in `current-frame-attempts/batch-numerics`. The replacement check keeps the exact batch shape and perturbs every other input image; the selected singleton output must remain bit-identical. Single-call numerical differences are retained as a diagnostic. This change precedes all geometry scoring and does not tune heldout results.

Create a separate preview with per-time four-way comparison, source-camera projection, measurement-only toggle, auxiliary source isolation, diagnostic-object focus and immutable evidence links. Verify switching, asynchronous ownership, source isolation, failure recovery and desktop/mobile rendering using the project agent-browser workflow; record the actual graphics backend. Keep the production workbench, data and old experiments intact. Clean this run's temporary virtual environment and checks after preserving the formal artifacts.

## Outcome

All 39 annotation times and 234 images completed in both input modes, with 936 RGB point renders. Current raw measurements remain separate and complete (1,354,112 rows with byte-identical sensor sidecars). All 11,514 fixed visible heldout returns were rescored by an independent artifact audit, along with every saved model point's source pixel, depth and RGB.

The final selected-cloud moving subset favors mono (52.78% versus joint 47.22% at .5m), while preselection corrected-depth results favor joint slightly. Thus the experiment does not establish intrinsically better monocular depth. Overall selected-cloud support is mono45.69%, joint46.39%, traditional28.37%; these within-scan completion percentages must not be compared with older acquisition-heldout scores. Missing small actors, surface spread and intentional measurement-protection gaps remain visible.

Six CPU regressions and 34 real-browser behavioral checks passed. Final copy clarification and visual review preserve the original-measurement accuracy distinction. The actual browser renderer is WebGL2 on Vulkan SwiftShader, with no WebGPU adapter; this is functional preview evidence rather than production GPU qualification. The maintained Chinese report is `docs/research/71-vggt-omega-current-frame.md`, and the independent preview is `/current-frame/` on the existing offline review server.

- Maintained results: [research report](../../research/71-vggt-omega-current-frame.md).
- Developer documentation: [point-cloud route](../../../docs-site/dev/reference/point-cloud-data-model.md#vggt-ω-点云增密主路线); CHANGELOG: Unreleased experiment entry.
- Remaining qualification and integration work: [consolidated plan](../1791963159_pointcloud-densification-route.md).

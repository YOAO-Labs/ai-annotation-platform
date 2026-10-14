# 0076 — Current-frame model geometry as optional auxiliary assets

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Project maintainer, authorized implementation
- **Supersedes:** —

## Context

Annotation needs useful density while retaining the complete measured scan and its
stable point identities. Overlapping temporal reconstructions produced missing
moving objects and ghosts. Existing neighbor LiDAR overlays and point surface
styles must remain usable alongside current-frame experiments.

## Decision

- Use one augmentation source preference: none, measured neighbors, or experimental
  current-frame VGGT-Omega. Surface styling remains independent. A nullable legacy
  preference preserves existing behavior until the user explicitly chooses.
- Keep raw geometry as the owner of picking, masks, measurements, fitting, quality
  checks and persistence. Show one camera's auxiliary RGB point layer in the main
  view, with separate counts and a temporary measured-only comparison.
- Produce fixed B6S1 depth/confidence in an optional, separately pinned container.
  The platform's existing service-pool routing and physical GPU admission own
  dispatch. CPU workers perform calibration and filtering; a durable AsyncJob owns
  progress, cancellation and retries. Opening the workbench never starts inference.
- Bind immutable generations to source objects, calibration, camera roles, axes,
  profile, model commit and weight hash. Reserve a lease, upload to immutable keys,
  recheck sources and actor/cancellation authority, then commit the active pointer.
- Use independent deployment and project gates, default off. Distinguish offline
  holdout calibration from full-current-LiDAR generation profiles. Never describe
  calibration residuals as an independent accuracy measurement.
- Import per-exposure nuScenes calibration. Repair legacy calibration only through
  an explicit compare-and-swap operation that preserves manual edits.

## Consequences

Raw annotations remain valid across model changes. Prepared results load without a
live model service, and failed builds do not replace valid generations. Storage,
browser cache and auxiliary point budgets are bounded separately from measured data.

The model can still produce incorrect depth or omit thin objects. Hardware rendering
qualification, annotation benefit and model licensing are separate deployment gates;
download access does not change the supplied FAIR noncommercial license. Temporal
model fusion and automatic model-derived annotation geometry remain outside this
experimental route.

## Notes

- Implementation: `apps/api/app/services/point_cloud_auxiliary/`,
  `apps/api/app/services/point_cloud_densification/`, `apps/vggt-omega-backend/`.
- Plan: `docs/plans/1791963159_pointcloud-densification-route.md`.

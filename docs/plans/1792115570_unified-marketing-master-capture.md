# Unified marketing-master capture

> Status: implementing

## Objective

Use qualified marketing masters as the single production capture source for project videos, GIFs, and posters. Preserve existing standard provenance honestly; standard capture is rehearsal only. Static screenshots remain independent.

## Scope

- Update `aap-doc-media` discovery description, instructions, and capture reference.
- Make selected recording default to `marketing`; require validation-only for `docs`, including the legacy flows entry. Retain explicit historical standard-archive derivation compatibility.
- Rerecord the 34 ML assets refreshed at commit `8347512fa03ac76afbed7a59c59681d6ced0793d` as qualified marketing masters. The previous 83 public derivatives remain until verified replacements are ready. The 18 static screenshots do not require rerecording.
- Derive selected registered public outputs from the new masters, review real content, and preserve unrelated assets and human review records.
- Incrementally deliver the 34 masters, raw sources, and original source manifests to `AAP资产:营销素材/高清母版/current/`. Merge the current manifest and preserve all unrelated qualified masters; do not mirror a partial batch with destructive sync.

## Implementation stages

1. Policy and entrypoints: update skills, production defaults, rehearsal guard, maintained developer guide, and regression tests. Commit the stable implementation before recording so generation provenance is clean.
2. Hardware and runtime preflight: verify an actual hardware-composited 60Hz X11 capture surface, NVENC, measured geometry and calibration, live model capability scope, and an exclusively owned disposable database/Redis/storage scope. Use a pilot before the full batch. No quality-gate downgrade.
3. Capture and derive: record bounded image/inference and video/tracker batches, keeping the serving checkout immutable during capture. Trace the mapping from asset IDs to flow names. Reuse successful masters after failures; fix only demonstrated capture defects, then retry incomplete assets.
4. Acceptance and delivery: verify file facts, cadence and hashes; inspect beginnings/core actions/endings/posters; derive through the shared pipeline; audit provenance and remote checksums; update this outcome and clean task-owned test resources. Continue authorized PR validation after push.

## Quality contract

Retain the existing output contract of 3840×2160 at 60fps, hardware compositing, rAF p95 ≤20ms, calibration ≥58 samples, effective unique fps ≥55 and unique-frame ratio ≥90%. Report actual capture geometry and resampling; these outputs must not be described as native pixel 4K. Do not pad/repeat frames to pass cadence or relabel the earlier 1440×810/25fps standard sources.

## Acceptance

- Default recording plans choose marketing; explicit docs recording without validation fails before runtime mutation; legacy flows produce no production archive.
- All 34 assets have new qualified source entries with actual source hashes, clean capture revision, preserved behavior/model evidence and valid GIF windows.
- Selected public derivatives trace to those masters; untouched files and unrelated cloud manifest entries remain intact.
- Local capture/derivation checks, media integrity, diff check and requested PR checks pass. Google Drive content checks match the delivered files.
- No synthetic human approval, leaked credentials, or deletion of unowned runtime/storage resources.

## Progress

- Confirmed the previous ML update used standard sources and does not satisfy the unified-master requirement.
- Implemented master-default selection, rehearsal-only standard entrypoints, read-only hardware preflight before seed, and marketing inference/behavior evidence retention. Updated the skill and maintained developer references.
- Validation: skill validator PASS; recording suite 43/43 PASS, followed by updated recording-plan regression suite 8/8 PASS; marketing archive suite 25/25 PASS; targeted ESLint, TypeScript, Markdown formatting and diff check PASS. Test fixtures were cleaned by their finally blocks.
- Independent review found and verified fixes for legacy rejection occurring after catalog initialization and hardware checks occurring after seed.
- The host is headless. An isolated current-user, non-seat0 NVIDIA Xorg display was started without modifying gdm, system ACLs or existing desktop sessions. Actual Chromium GPU compositing reports RTX 3090 enabled. The full existing calibration pilot is diagnosing the installed FFmpeg window-grab compatibility; no new qualified ML master has been produced yet.

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
- The host is headless. An isolated current-user, non-seat0 NVIDIA Xorg display runs without modifying gdm, system ACLs or existing desktop sessions. Actual Chromium GPU compositing reports RTX 3090 enabled; the full calibration pilot and product captures passed after selecting the compatible NVENC FFmpeg and a sufficiently large virtual framebuffer.

- Headless entry implemented and integrated: `--headless-display` creates an owned non-seat0 NVIDIA Xorg session with 3840×2160 framebuffer, private temporary authentication, Metacity and NVENC encoder selection; normal/failure/signal cleanup removes only owned resources. Actual wrapper calibration on an independent display passed at 66/66 unique frames and 59.99fps; cleanup verified.
- Integrated validation found a Unicode-checkout pathname issue in a read-only CLI test; corrected URL-to-path conversion and all 11 headless tests passed. The other 55 integrated tests had passed; type checking and regenerated 189-variable environment reference passed.
- First product pilot completed: `sam-tools/smart-point`, run `20261016T021321Z-12136e94b53a`, clean source revision `12136e94b53a002553e05456909c962efd82e30e`, MKV and MP4 both 3840×2160/60fps/16.017s, cadence 66/66 unique at 59.98fps. Root verified source hashes and inspected beginning/core/saved Mask result. This is agent sampling, not a fabricated human review.
- Added single-asset `--poster-at` derivation with exact selected frame provenance because the old fixed poster timestamp landed during real inference. Existing masters remain immutable; public pilot files will be derived after capture.
- Execution uses three clean worktrees with independently owned databases, Redis instances, eight storage buckets each, workers and hardware displays. The disjoint final assignment is 11 image/inference assets, 13 video assets and 10 pipeline/management assets. Static screenshots remain unchanged. Stateful ML flows retain per-flow fixture repair, real-model and save/readback assertions; only the five proven management demonstrations reuse their freshly reconciled fixture. Management observation snapshots are explicitly identified as snapshots.
- Real batch failures exposed body-level timeouts overriding isolated fixture preparation. Selected captures now preserve a minimum 15-minute preparation/encoding budget and any longer flow budget, while ordinary E2E timeouts and product assertions remain unchanged. Recording-plan regression tests passed 10/10; TypeScript declaration contract checks passed. Exemplar now includes the actual persisted bbox after reload in its content window without lowering the duration gate.
- Vitest erroneously collected the new Node test suite. The existing precise exclusion pattern now covers it; the Vitest filtered run collects zero tests and the independently invoked Node suite passes 11/11.
- Owned Xorg sessions now start with screen saver timeout zero and DPMS disabled. An actual additional display confirmed those settings and complete socket/lock/reservation cleanup. The older display was observed powered off, but real OCR Off/On diagnostics both allowed rendering and normal clicks, so DPMS is not a proven cause of the earlier OCR timeout. No product hover/actionability workaround was introduced.
- Subsequent bounded controls reproduced native rAF stopping in natural-animation workbenches despite a visible/focused page, enabled controls and successful media requests. Neither stopping the recorder, disabling trace, activating/resizing the window nor explicitly restoring monitor power recovered it. Removing only `--run-all-compositor-stages-before-draw` from Linux headed capture restored 116–117 callbacks per two seconds, nontransparent video canvas pixels and normal clicks in two runs, while measured hardware cadence remained about 59 unique fps. The Linux-only fix preserves macOS behavior, product controls and all quality gates; natural-animation production pilots are rerunning against its clean revision.

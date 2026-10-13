---
name: aap-renderer-validation
description: Measure or validate this project's point-cloud/WebGPU, raster-mask acceleration, or precise-video rendering on a real browser and device.
---

# Renderer qualification

Use the existing harness for the requested subsystem. Record actual hardware, browser, graphics backend, viewport/DPR, fixture, and source revision. Host hardware hints are a starting point; verify the browser's real adapter and fallback status.

## Point clouds

Performance entry: `apps/web/scripts/benchmark-pointcloud-renderer.mjs`; command: `pnpm --dir apps/web pointcloud:renderer-bench`. Supply the intended `POINTCLOUD_BENCH_BASE_URL`, project ID and task ID from the current fixture. Read its environment options before running; it temporarily changes preferences and may create a temporary box.

Strict functional qualification is a separate path: run the current point-cloud edit/playback specs with `PLAYWRIGHT_POINTCLOUD_WEBGPU=1` in the dedicated Playwright `pointcloud` project, preferably through `pnpm dev:worktree -- exec --mode e2e`. The strict gate must assert the production renderer reports WebGPU and exercise the real fixture; a benchmark sample alone is not this qualification.

Use current harness gates, not copied historical thresholds. Renderer timing begins at the history/navigation update; retain click-to-navigation separately because timeline settling is intentional. Missing navigation evidence invalidates the sample.

Separate cold, adjacent, and fully warm samples. A warm label alone is insufficient: all relevant cache stages must report hits. Report sample counts, absolute latency and qualified relative gains. Compare tri-view first-open and reopen as separate cases.

For pipeline preparation, inspect `PointCloudTriViewPass.ts`, `PointCloudScene.ts`, and `usePointCloudScene.ts`. Compilation must use real geometry and the six-plane clipping topology, deduplicate per generation, suppress pending tri-view draws, and invalidate after completion. Preserve the single renderer owner.

The historical [macOS report](../../../docs/research/25-macos-webgpu-performance.md) contains prior evidence, not current certification. In particular, short Safari success did not establish long-sequence reliability. For hangs, trace fetch, decode Worker, abort and geometry submission before attributing a renderer failure.

Probe `navigator.gpu` from the same secure application origin and launch configuration as the qualification run; `about:blank` is not valid adapter evidence. Record the browser-reported vendor/architecture and `isFallbackAdapter` when exposed. A missing/null fallback field or a capable feature set does not prove hardware rendering by itself.

## Raster masks and precise video

Read [mask and video checks](references/mask-video.md) only for those subsystems.

## Reporting

Report measured results and unresolved boundaries separately. Scope any device-loss test to a dedicated test browser; do not kill the user's browser or exhaust physical memory. Verify fallback restores data and releases the old renderer/resources.

Keep the maintained report under `docs/research/` when requested, with its index entry. Keep raw JSON, traces, recordings and temporary harnesses outside tracked output. Clean only artifacts created by this run when cleanup is requested, after preserving the required report/evidence.

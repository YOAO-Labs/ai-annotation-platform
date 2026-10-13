# Raster-mask and precise-video evidence

## Raster masks

Trace the production sparse-mask tile store, Worker, WebGPU operation, XOR patch/history, save and reload. Compare CPU oracle output with GPU output, including non-word-aligned tails, offsets, overlapping/disjoint ROIs, and empty/full cases relevant to the change.

Measure the actual production policy: small ROIs may remain on CPU. Check fallback/device-loss handling, bounded buffer reuse, and disposal of worker/session/GPU owners. Shader output alone does not verify undo or persisted annotations.

## Precise video

Start at `apps/web/scripts/video-bench/run-video-bench.mjs`, `precise-frame-runner.mjs`, and their tests. Inspect current options and fixture requirements instead of substituting a generic playback benchmark.

Verify the actual decode/render path and native hardware evidence for the target platform. Unsupported probes or disconnected CDP sessions must not retain stale evidence that allows a strict gate to pass. The CDP session lifecycle uses `close`; inspect the installed library and adjacent regression when changing probe teardown.

For the repository's strict application-path check, run `e2e/tests/video-webcodecs-precise-frame.spec.ts` with `PLAYWRIGHT_REQUIRE_WEBCODECS=1` in the Chromium project, preferably through the isolated e2e worktree mode. Passing proves the WebCodecs path and decoded-pixel/annotation alignment exercised by that spec; it does not prove hardware video decoding. Hardware qualification requires separate browser/decoder evidence.

Browser checks should verify the intended frame remains aligned with annotation geometry, including pause, seek, resize and task changes when affected. Distinguish true decoded frame evidence from UI timestamps and software fallbacks.

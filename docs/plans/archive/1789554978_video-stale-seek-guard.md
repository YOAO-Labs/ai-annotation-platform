# Video stale-seek frame-write guard (Issue #114)

> Status: implemented — see [Outcome](#outcome).
> Date: 2026-09-16
> Requested outcome: close the application-level race behind Issue [#114](https://github.com/yyq19990828/ai-annotation-platform/issues/114) — a late async precise-frame fetch started by a timeline click can drag the selected frame index back to the clicked (quantized) frame after keyboard stepping has already advanced it — so the E2E serialization introduced by PR #113 (`expectPaintSettled`) becomes redundant protection instead of the only line of defense.
> Motivation: CI `default-two` run `34952411169` (2026-09-15) failed `video-issue-context` G2-1 twice-on-retry: after clicking the frame timeline (quantized to F105) and stepping with `Shift+ArrowRight` to F120 (all intermediate assertions passing), the final assertion found `data-video-frame-index` rewritten back to **F105** and `data-video-painted-frame-index` jumping `-1 → 105`. PR #113 serialized the test interaction, but real users doing "click timeline, then immediately step/scrub" still hit the race.
> Non-goals: redesigning the precise-frame pipeline, changing the WebCodecs cache layers, changing timeline click quantization, removing the E2E paint-settle helper entirely (keep as belt-and-braces unless maintainer decides otherwise).

## Findings

### How the frame number is written today

`data-video-frame-index` renders `controller.frameIndex` (`VideoKonvaStage.tsx:2409`). In the Workbench the value is **controlled**: `WorkbenchStageHost` passes `videoFrameIndex` state from `useWorkbenchState` and `onFrameIndexChange` (`useWorkbenchShellModel.tsx:8168-8181` wraps it with segment clamping and calls `s.setVideoFrameIndex`). The only runtime writer is `setFrameIndex` inside `useVideoPlaybackController` (`useVideoPlaybackController.ts:287-294`), reached solely through `handleFrameClockChange` — the `onFrameChange` handed to `useFrameClock`.

`useFrameClock` calls `onFrameChange` from exactly two places:

1. `startSeek` — an **optimistic synchronous write** of the seek target (`useFrameClock.ts:267`), and
2. `updateFrameFromTime` — a media-derived write, allowed only when a **pending seek matches the reported frame** (`pending.frameIndex === mediaFrame`, `useFrameClock.ts:219-225`) or while playing (`useFrameClock.ts:241`).

### Guards that already exist (audited at HEAD)

| Path                                                             | Guard                                                                                               | Location                                                                            |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `seekFrameAsync` continuation after `await seekNativeFrameAsync` | mounted + source epoch + navigation generation                                                      | `useVideoPlaybackController.ts:812-818`                                             |
| `seekToFrameReady` presentation lifecycle                        | pending-presentation identity + epoch + generation; newer navigation cancels                        | `useVideoPlaybackController.ts:242-263, 516-543, 994-1042`                          |
| `updateFrameFromTime` media reports                              | pending-seek identity match or `isPlaying`; mismatches counted as `staleCallbacks`                  | `useFrameClock.ts:219-241`                                                          |
| Precise decode completion                                        | effect cleanup `cancelled` flag + `latestRef` latest-request-wins (`taskId`/`frameIndex`/`enabled`) | `useVideoPreciseFrame.ts:551-566`                                                   |
| Precise decode activation / display                              | strict `frameIndex === current` on `precise.bitmap`, `displayBitmap`, `framePresentation`           | `useVideoPreciseFrame.ts:600-604`, `useVideoPlaybackController.ts:386-401, 430-470` |
| Paint receipt                                                    | `markFramePainted` compares timing + **closure** `frameIndex`                                       | `useVideoPreciseFrame.ts:606-624`                                                   |

None of the audited app files changed between the failing CI run (2026-09-15) and HEAD, so the race exists in current code.

### Why the regression still slips through

Every guard above is a **snapshot check at write time**; there is no monotonic barrier at the sink (`handleFrameClockChange`) keyed to the _current navigation_. Under the CI conditions — the same run family documented a ~28 s main-thread freeze with coalesced input and a backlogged media pipeline (`docs-site/dev/troubleshooting/ci-flaky-services.md`, symptom 4) — at least one interleaving lands a stale `onFrameChange(105)`:

- `startSeek(N)` writes `onFrameChange(N)` optimistically; the seek pipeline (native `<video>` + WebCodecs precise decode for the clicked frame) settles **after** newer steps bumped the navigation generation.
- The paused re-seek effect (`useFrameClock.ts:385-391`) re-fires `startSeek(frameIndex)` whenever the controlled prop and `video.currentTime` disagree beyond tolerance; with the host's controlled state lagging a commit behind rapid stepping, a re-seek to a superseded frame re-publishes it through the optimistic write.
- The 300 ms `SEEK_TIMEOUT_MS` (`useFrameClock.ts:61`) clears `pendingSeekRef` while the element is still backlogged; the eventual `seeked`/rVFC for the clicked frame then re-records `nativeFrameRef` evidence (`useFrameClock.ts:205-217`) with no pending left to reject it.

The observed `painted -1 → 105` transition is a downstream symptom consistent with this: once `frameIndex` regresses, the already-cached F105 bitmap re-activates and paints, because both the cache-show effect (`useVideoPreciseFrame.ts:296-312`) and `markFramePainted` only compare against the _current_ (regressed) frame index.

The exact interleaving cannot be pinned conclusively by static reading — the repro harness below is therefore **phase 0**, not an afterthought. It is also exactly the acceptance test Issue #114 asks for.

### Related weak points found during the audit (fix alongside)

- `markFramePainted` validates against the **closure** `frameIndex` (`useVideoPreciseFrame.ts:613`); `VideoKonvaMediaLayer`'s draw-notify closure captures `frameIndex` at effect registration (`VideoKonvaMediaLayer.tsx:98-127`). A late Konva `draw.mediaFramePaint` event can therefore report a paint for a frame that was current at registration. Harden both to live values.
- The timeline `onSeek` handler calls `pausePlayback()` with default `snapToGrid: true` before `seekToFrame` (`VideoKonvaStage.tsx:2923-2927`); with a sampling grid > 1 this fires two competing seeks per click (snap + target). Not the root cause (the second cancels the first), but worth normalizing to `snapToGrid: false` while touching this area.

## Plan

### Phase 0 — deterministic reproduction (blocks everything else)

1. Add an integration test that runs the **real** `useFrameClock` against the real `useVideoPlaybackController` (the existing controller suite mocks `useFrameClock` wholesale, so these interleavings are untestable there). Use the fake media element pattern from `useFrameClock.test.ts` (`nativeVideo()` with rVFC handles) plus a controller-level fake video.
2. Model the CI interleaving: timeline click `seekToFrame(105)`; a mocked slow precise decode for F105 stays in flight; `microStep(1)` advances to 106…120; the fake media pipeline then delivers **late** `seeked`/rVFC for t105 — including the variants (a) while `pendingSeekRef` is already null after the 300 ms timeout, and (b) after a controlled-prop lag that re-triggers the paused re-seek effect.
3. Assert: `frameIndex` stays at the stepped value; `onFrameChange` never receives a superseded frame outside playback. Record which variant reproduces the regression — this decides where the sink barrier must hook.

### Phase 1 — sink-level stale-assignment guard (the fix)

Single ownership at the frame-number write convergence point; no new owners:

1. In `useVideoPlaybackController`, gate `handleFrameClockChange` media-derived writes by navigation identity: each `seekFrameAsync` already bumps `navigationGenerationRef`; expose the initiating seek's identity from `useFrameClock` (e.g. extend `onFrameChange` to receive the pending seek's `{ id, frameIndex }` for pending-scoped reports, or export a `latestSeekIdRef` comparator) and drop reports that do not belong to the current generation — except reports arriving while `isPlaybackActive` (legitimate playback advances).
2. Close the optimistic-write regression window: `startSeek` (and the paused re-seek effect at `useFrameClock.ts:385-391`) must not (re-)publish a frame older than the controller's latest navigation — compare against the live `frameIndexRef`/latest published seek before calling `onFrameChange`, and skip the write when the target is superseded (still perform the media seek itself so pixels can catch up).
3. `markFramePainted`: compare `paintedFrame` against a live frame-index ref inside `useVideoPreciseFrame` (keep one `frameIndexRef`-style ref in sync during render), and have `VideoKonvaMediaLayer`'s notify re-check the live current frame before reporting.

Late results remain cache-writable (per the issue's suggested pattern: "过期结果直接丢弃或只更新缓存不更新展示帧") — decodes still land in the bitmap caches for instant display when the user returns to that frame.

### Phase 2 — E2E proof without the serialization

1. Temporarily remove the `expectPaintSettled` call inside `seek()` (`apps/web/e2e/tests/video-issue-context.spec.ts:407`) and re-run the G2-1 family locally; keep the reload-site calls.
2. After green: keep or drop the helper per maintainer preference (recommendation: keep it — it is cheap and documents the invariants; update its comment to say the app-level guard is the primary defense).

### Phase 3 — documentation

| Item                                                                                    | Path                                                         |
| --------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| Changelog entry (Fixed: stale seek overwriting keyboard stepping)                       | `CHANGELOG.md` Unreleased                                    |
| Troubleshooting symptom 4 note: app-level guard landed, E2E serialization now redundant | `docs-site/dev/troubleshooting/ci-flaky-services.md`         |
| E2E README "WebCodecs 精确帧 E2E" section wording                                       | `apps/web/e2e/README.md`                                     |
| Workbench state-ownership skill lifecycle case (optional, one paragraph)                | `.agents/skills/aap-workbench-state/references/lifecycle.md` |

## Acceptance criteria

1. New integration test reproduces the F105-regression interleaving against pre-fix code (validated by seeing it fail on the unpatched controller) and passes after the fix.
2. Existing suites stay green: `useFrameClock.test.ts`, `useVideoPlaybackController.test.ts`, `VideoKonvaMediaLayer.konva.test.tsx`, `VideoKonvaStage.konva.test.tsx`.
3. `video-issue-context.spec.ts` G2-1 passes locally **without** the in-`seek()` `expectPaintSettled`; CI `default-two` shard recovers its historical stability.
4. `pnpm --filter @anno/web typecheck` / `lint` / `lint:css-tokens` pass; `git diff --check` clean.

## Risks and notes

- The generation barrier must not break legitimate flows: reverse seeks (jump history, bookmarks, keyframe jumps), loop-region wraparound, jog playback, and issue navigation (`useVideoIssueNavigation.seekFrame` → `seekToFrameReady`) all rewrite `frameIndex` to older values by design — the guard keys on _navigation recency_, never on frame magnitude.
- `SEEK_TIMEOUT_MS = 300` is aggressive under CI load; raising it is tempting but changes seek-failure semantics beyond this issue — out of scope here, note for a separate tuning discussion.
- Browser-level coalescing of rapid `currentTime` assignments cannot be fully simulated in jsdom; the phase-0 fake models the observable contract (late/out-of-order completion events) rather than browser internals, which is the same level the existing `useFrameClock` tests operate at.

## Outcome

- Landed changes:
  - `useVideoPlaybackController.ts`：`handleFrameClockChange` 增加暂停态单调栅栏（只接受最新导航目标 `frameNavigationTargetRef` 或当前帧回声，播放态豁免）；`seekFrameAsync` 在发起媒体 seek 前登记导航目标。
  - PR #118 评审跟进：导航目标改为渲染期收敛到已提交帧——原实现里目标仅由 `seekFrameAsync` 更新，「seek A → 播放推进到 B → 暂停」或宿主外部改写受控帧（章节/段落/任务恢复）后，A 的迟到回报仍能过栅栏拽回旧帧；现随 `frameIndexRef` 一同镜像最新帧，仅 `seekFrameAsync` 赋值与其乐观写之间的同步栈窗口内保留在途目标。配套对抗测试（播放后暂停、受控帧外部跳转）均已在未修复代码上验证为红。
  - `useVideoPreciseFrame.ts`：`markFramePainted` 改用 `liveFrameIndexRef` 校验最新帧，迟到的 Konva 绘制回执不再发布过期 painted 帧。
  - `VideoKonvaStage.tsx`：时间轴 `onSeek` 的 `pausePlayback` 改为 `snapToGrid: false`，消除点击时的双重 seek 竞态窗口。
  - 新增 `useVideoPlaybackController.stale-seek.test.ts`（真实 `useFrameClock` × 真实控制器集成 harness，三个迟到媒体事件交错场景）；`useVideoPlaybackController.test.ts` 新增汇聚点栅栏对抗测试（未修复代码上验证为红，修复后绿）。
- 验证：
  - 单测：`apps/web` 全量 5448/5448 通过；typecheck / eslint / lint:css-tokens / prettier 通过。
  - E2E（本地 `dev:worktree --mode e2e` 隔离栈）：临时移除 `seek()` 内 `expectPaintSettled` 串行化后，`video-issue-context.spec.ts` 全部 11 个用例通过（含 G2-1）；spec 恢复保留该 helper 作为冗余防护。
  - Phase 0 复现结论：jsdom 可模拟的三类迟到交错（pending 超时后的迟到 seeked/rVFC、pending 存活期的不匹配回报、点击帧迟于首步呈现）在 HEAD 上均被既有 pending 身份校验拦下；CI 观测到的帧号回退依赖浏览器内部 seek 中止/回退语义与受控态提交交错的组合，无法在 jsdom 复刻。因此修复落在帧号写入汇聚点的单调栅栏——对任意来源的迟到旧帧回报一律生效，并以注入式对抗测试锁定该合同。
- User documentation: `docs-site/user-guide/` 无需更新（无可感知 UI 变化；行为修复即「不再跳回旧帧」）。
- Developer documentation: `docs-site/dev/troubleshooting/ci-flaky-services.md`（症状 4 补充应用层修复与 `expectPaintSettled` 定位）、`apps/web/e2e/README.md`（WebCodecs 精确帧 E2E 一节）。
- ADR: 无新增（未引入架构决策，沿用单一 owner 的播放控制器状态）。
- CHANGELOG: Unreleased `Fixed` 新增条目（Issue #114）。
- Release milestone: Not yet determined
- Remaining work: 无；`SEEK_TIMEOUT_MS=300` 的负载调优另行讨论（见 Risks）。

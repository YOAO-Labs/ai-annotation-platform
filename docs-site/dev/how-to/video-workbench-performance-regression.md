---
title: 视频工作台性能回归
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# How-to：视频工作台性能回归

视频工作台提供固定 bench 矩阵、BugReport 自动诊断，以及 PR 附件路径约定。它不上传 trace，也不要求仓库提交真实视频 fixture。

## 快速运行

```bash
pnpm --filter @anno/web video:bench -- --dry-run
pnpm --filter @anno/web video:bench
```

默认产物写到 `test-results/video-bench/<run-id>/`：

- `manifest.json`：本次矩阵、预算、fixture 描述和 trace 目标路径。
- `README.md`：PR 附件说明。

可选参数：

```bash
pnpm --filter @anno/web video:bench -- --base-url http://localhost:3000
pnpm --filter @anno/web video:bench -- --out /tmp/video-bench
```

## Bench 矩阵

fixture 描述在 `apps/web/scripts/video-bench/fixtures.json`：

| 视频          | 目标                     |
| ------------- | ------------------------ |
| 720p / 3 min  | 常规标注回归             |
| 1080p / 5 min | 主流长视频场景           |
| 4K / 30 sec   | 高分辨率拖拽和 seek 压力 |

每组视频覆盖 10 / 100 / 500 tracks 三档密度，场景包含打开工作台、时间轴 scrub、拖拽选中轨迹、J/K/L 播放和 hover preview。

## WebCodecs 精确帧基准

```bash
pnpm --filter @anno/web video:bench -- --scenario precise-frame --dry-run

VIDEO_BENCH_CHROMIUM_CHANNEL=chrome \
pnpm --filter @anno/web video:bench -- \
  --scenario precise-frame --seed --headed --strict \
  --base-url http://127.0.0.1:3001
```

`--seed` 是隔离 E2E 栈的推荐入口。先启动 PostgreSQL、Redis、MinIO，以及使用
`annotation_e2e`、`E2E_SEED_ENABLED=true` 的 API（8010）和代理该 API 的 Web（3001）。runner 会在
隔离数据库中创建 1080p/30、1080p/60、4K/30 三个 61 秒 H.264 task，把登录态放进内存
`storageState`，结束或失败时调用 cleanup；不会写 auth-state 文件，也不会修改开发数据。三个 task URL
只要有一个显式存在，`--seed` 就会拒绝运行，避免自动素材与外部素材混测。该隔离栈是单写者夹具，
不要并行运行多个 `--seed` benchmark 或其他会调用全局 reset / cleanup 的 E2E。

已有固定素材时仍可手工传入 URL：

```bash
VIDEO_BENCH_STORAGE_STATE=/abs/path/auth-state.json \
VIDEO_BENCH_CHROMIUM_CHANNEL=chrome \
VIDEO_BENCH_TASK_1080P_30_URL=/projects/.../annotate?task=... \
VIDEO_BENCH_TASK_1080P_60_URL=/projects/.../annotate?task=... \
VIDEO_BENCH_TASK_4K_30_URL=/projects/.../annotate?task=... \
pnpm --filter @anno/web video:bench -- --scenario precise-frame --headed --strict
```

`--dry-run` 只校验矩阵，不启动浏览器、不写报告，也不产生 Worker 结论。不使用 `--seed` 时必须提供
三条已登录用户可访问的任务 URL，以及 Playwright `storageState`。任务媒体须分别匹配声明的分辨率 /
fps，并至少覆盖两个 GOP。runner 会读取真实 manifest 与 sample 响应，核对分辨率、fps、H.264 codec、
chunk size 和关键帧边界后再派生 same-GOP / cross-GOP 目标；缺少任一任务或素材合同不匹配时不能得到
完成结论。

固定 GPU runner 应通过 `VIDEO_BENCH_CHROMIUM_CHANNEL=chrome` 使用系统 Google Chrome，
并把本地 `DISPLAY` / `XAUTHORITY` 绑定到真实桌面会话。bundled Chromium 与 SSH 转发显示
只适合能力探测，不能代表用户 Chrome 的 codec / GPU 路径。

有 GPU 但没有物理显示输出的 X11 runner 可能把有头页的 BeginFrame 降到约 1 Hz。runner
在有头模式自动传入 `--disable-gpu-vsync` 恢复 Chrome 的正常帧调度，并把实际参数写入
`manifest.environment.chromiumArgs`。`gpuAdapter` 只证明 GPU 合成，不能证明 WebCodecs 使用硬件视频
解码。Apple Silicon macOS strict 使用 CDP Media domain 匹配 `WebCodecs::VideoDecoder` player，并要求
至少一个 initialized player 同时发布 `kVideoDecoderName=VideoToolboxVideoDecoder` 与
`kIsPlatformVideoDecoder=true`；只有 `kLoad`、没有走到初始化的瞬时 player 不参与正反判定，任何半属性
或相反属性仍 fail closed。`SystemInfo.getInfo` 的 `video_decode` 和 profiles 只保留为环境诊断，因为该
平台可能在实际命中 VideoToolbox 时仍返回空 profile。Intel macOS 不使用相同信号宣称硬解通过；其他
平台继续按自身可验证的硬解能力门执行。

`--strict` 用于固定 GPU runner 的资格门：报告仍会先落盘，但 `inconclusive` 或 Worker 门触发
会令命令以非零状态退出。开发机探索可省略该参数，保留三态报告而不把能力不足误当通过。
如果只是定位软件解码路径的阶段耗时，可在非 strict 运行中临时设置
`VIDEO_BENCH_ALLOW_SOFTWARE_DECODE=1`；该开关在 strict 运行中无效，不能生成发布资格证据。

产物写到 `test-results/video-bench/<run-id>/`：

- `manifest.json`：分辨率矩阵、逐操作原始 observation、环境、资源账本、退出门与三态 Worker 决策。
- `summary.md`：实测 p95、归因 long task、flag-on/off 交互 rAF、资源账本与 Worker 决策摘要。

字节预算三档（工作台设置 → 性能档位）：轻量 96/32 MiB、标准 256/96 MiB、激进 512/192 MiB（bitmap / chunk）。判读要点：

- **请求计数**：flag off 与连续播放的 precise 请求必须为 0（`data-video-precise-state=disabled`、播放态 `data-video-frame-source=video`）。
- **计数边界**：连续播放开始前先等待暂停态 precise 预取请求流静默，再从 play click 计数；
  否则前一次 seek 的尾请求会被误记为播放态新增请求。
- **播放真实性**：precise-on 与 flag-off 都从 frame 0 开始，除页面 rAF 外还要求媒体 `currentTime`
  持续前进到目标时长且 `ended=false`；页面在刷新但视频未前进不能通过。
- **session / 逐帧**：同 GOP 顺序逐帧的 `encodedChunksSubmitted` 只增量增长；后退 / 跨 GOP 触发 `sessionResets`。
- **long task / 主线程**：以 `buildGopPlan`（sample 校验、GOP 规划、description 解码、
  `EncodedVideoChunk` 构造）的同步耗时作为可迁移 slice，再与操作窗 Long Tasks API
  观测关联；归因 ≥50ms long task 必须为 0、同步 blocking p95 ≤16ms。
- **交互帧率**：同一组逐帧目标分别在 flag on / off 下采集 rAF，Worker 门只看相对回退；
  连续播放 rAF 仍是播放资格门，不冒充 precise pipeline 的 Worker 证据。
- **内存**：稳定性目标以互质步长跨越 GOP / chunk / 缓存预算，中点与结束各取一次 plateau；
  操作结束后 `liveVideoFrames` 归零、活动 decoder ≤1、bitmap / chunk 字节账本回预算且不持续增长。
- **Worker 触发门**：只有三组矩阵样本、Long Tasks API、资源计数与连续播放全部完成，才能得到 `triggered` 或 `not-triggered`；能力不足、fallback、样本不足或指标缺失一律为 `inconclusive`。触发后实现 worker 并重跑全部 gate。

headless Chromium 可能没有可用的 H.264 软解。warm seek / long task 等资格结果必须来自有头 Chrome
或带 GPU 的固定 runner；fallback 只能证明降级合同，不能参与“不需要 Worker”的结论。

## BUG 反馈诊断

视频工作台会维护当前 task 的诊断快照：

```js
window.__videoWorkbenchDiagnostics;
window.__videoFrameClockDiagnostics;
```

用户在视频工作台提交 BUG 反馈时，`BugReportDrawer` 会自动把快照追加到描述末尾，并在 `recent_console_errors` 中插入 `[video-workbench-diagnostics]` JSON payload。排查时优先看：

- `frameClock.recentSeeks`：最近显式 seek 的 frame、耗时和 ready source。
- `frameClock.longTasks`：当前会话捕获到的长任务计数。
- `framePreview.cacheHits / cacheMisses`：hover thumbnail 是否命中缓存。
- `timelineMode`：当前是 selected-track timeline 还是 global-density timeline。
- `playbackRate`：J/K/L 当前方向与速率。

## PR 检查建议

涉及视频渲染、timeline、frame preview 或 J/K/L 播放时：

1. 跑 `video:bench -- --dry-run` 确认矩阵没有被破坏。
2. 手动用至少一组 1080p fixture 录制 Performance trace。
3. 把 `test-results/video-bench/<run-id>/` 和 trace 放到 PR 附件或评论。
4. 若用户通过 BUG 反馈提交问题，直接引用反馈详情中的 `Video Workbench Diagnostics`。

Playwright trace 仍按问题定位需要手工开启；资格 runner 本身不提交大体积视频或 trace。

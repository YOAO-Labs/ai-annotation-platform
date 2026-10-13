# macOS WebGPU 性能实测

测试日期：2026-09-05；2026-09-15 复测与资格补齐见文末专节。

## 结论

- Raster Mask 在 Chrome 与 Safari 的生产路径上均通过正确性检查，并在 2K、4K ROI 上取得稳定收益。
- 3D 点云在 Chrome 的真实 Apple Metal WebGPU 后端上通过当前推广门。相机上色收益明显，三视图首次展开的管线编译停顿已经消除。
- Safari 点云 WebGPU 的短序列通过，但长序列曾停在点云 geometry 完成前。该路径仍应保持实验功能默认关闭，等待顶层页面复现和加载链诊断。
- 2026-09-15：当前源码（v0.25.3）上 Chrome/Metal 复测全部通过；macOS 缺口收敛为 Raster Mask one-pass 复测、超大图 tile 首次资格与 3D 多轮资格，详见
  [2026-09-15 复测与资格补齐](#2026-09-15-复测与资格补齐)。

## 环境与口径

- MacBook Pro，Apple M4 Pro，14 核 CPU、20 核 GPU、48 GiB 统一内存。
- macOS 26.6.2，Chrome 152.0.7977.76，Safari 26.6.2。
- Chrome adapter 为 Apple / Metal 3，`isFallbackAdapter=false`；没有启用 Vulkan、unsafe WebGPU 或 GPU 黑名单绕过。
- Vite 开发站点与本机 API、Docker 基础服务；测试期间接通电源，系统没有热或性能告警。
- p95 使用 nearest-rank。结果包含浏览器、React、资源加载和绘制边界，不代表纯 shader 时间或清空后台进程后的裸机上限。

## Raster Mask

生产链路为 SparseMaskTileStore → 持久 Worker → WebGPU → XOR patch/history。每个场景先预热 3 次，再测量 20 次；CPU 对照使用当前 packed-separable 路径。

| 浏览器 | ROI         |  CPU p50 / p95 | WebGPU p50 / p95 | p95 降低 |
| ------ | ----------- | -------------: | ---------------: | -------: |
| Chrome | 2048 × 2048 |   7.8 / 8.7 ms |     6.4 / 6.7 ms |    23.0% |
| Chrome | 3840 × 2160 | 13.6 / 14.2 ms |    9.9 / 10.5 ms |    26.1% |
| Safari | 2048 × 2048 | 10.0 / 11.0 ms |    9.0 / 10.0 ms |     9.1% |
| Safari | 3840 × 2160 | 21.0 / 22.0 ms |   17.0 / 18.0 ms |    18.2% |

两种浏览器均通过 1024²、2048²、4K、非字对齐、重叠 ROI 和分离 ROI 六类场景。patch checksum、合并后保存 checksum 与重新载入 checksum 一致。1024² 按生产策略继续走 CPU。

Chrome 另以 radius 31 对 2048² 与 4K 各执行 100 次测量，合计 412 次真实 GPU 作业。2048² p95 为 CPU 16.9 ms、WebGPU 15.0 ms；4K 为 29.3 ms、22.2 ms。没有 runtime fallback 或 device lost，固定 ROI 下缓冲容量保持稳定，dispose 后 Worker、session、GPU owner 和 buffer 均归零。

Chrome 与 Safari 还各完成 50 组真实 shader 和 CPU oracle 的逐 word 对比，覆盖空、全满、棋盘、边界、固定随机图案及多种 radius、尾字和 offset，结果均为 50/50 通过。

## 3D 点云 Chrome

使用 nuScenes mini scene-0061 的 39 帧、约 3.5 万点和六路相机。Legacy 与 WebGPU 各执行 20 次非相邻跳帧和 20 次相邻 warm 切帧，viewport 为 1440 × 900、DPR 1。

修复前，benchmark 从时间轴 click 开始计时，把产品有意设置的 160 ms 导航防抖计入 renderer；warm geometry 的 120 ms 门槛因此无法通过。三视图折叠时也没有准备 WebGPU 六面裁剪管线，首次展开会承担一次同步编译。

当前 benchmark 从 history 导航更新开始统计 renderer 时间，同时单独保留 click → history 指标。warm 相对收益只使用所有相关资源都报告 cache hit 的样本；全量 warm p95 仍受绝对 250 ms 上限约束。三视图要求首次展开不超过 50 ms，且相对重开额外开销不超过 10 ms。

最终使用仓库正式 benchmark 脚本在原生 Chrome/Metal 上重跑一轮：

| p95 指标             | Legacy WebGL2 |   WebGPU |             变化 |
| -------------------- | ------------: | -------: | ---------------: |
| cold geometry        |      157.8 ms | 123.5 ms |       改善 21.7% |
| warm geometry        |       83.9 ms |  35.2 ms |       改善 58.0% |
| warm RGB（全部样本） |      116.1 ms |  42.6 ms |       改善 63.3% |
| fully-warm RGB       |      123.9 ms |  42.6 ms |       改善 65.6% |
| 三视图首次展开       |       27.3 ms |  26.9 ms | WebGPU 快 0.4 ms |
| 三视图重开           |       25.0 ms |  24.1 ms | WebGPU 快 0.9 ms |

Legacy fully-warm 样本为 18/20，WebGPU 为 19/20，WebGPU depth cache 命中率为 95%。真实 backend、零实验路径 Canvas 全图回读、零运行时错误、样本数量、绝对延迟、warm 收益、三视图首开/重开、rAF 与最大帧隙检查全部通过，最终 `promotionGate.passed=true`。

实现位置：

- [benchmark-pointcloud-renderer.mjs](../../apps/web/scripts/benchmark-pointcloud-renderer.mjs) 负责原生 macOS Chrome 启动、虚拟时间轴导航、renderer 计时边界、fully-warm 汇总和推广门。
- [PointCloudTriViewPass.ts](../../apps/web/src/pages/Workbench/stages/three-d/PointCloudTriViewPass.ts) 使用真实六面裁剪拓扑异步编译三视图 WebGPU 管线；geometry 换代会重新准备，编译期间展开会在完成后补画。
- [PointCloudScene.ts](../../apps/web/src/pages/Workbench/stages/three-d/PointCloudScene.ts) 在点云 geometry 提交后触发预热，并继续保持单 renderer owner。

## Safari 点云限制

Safari 使用同源 iframe 驱动原生浏览器页面，DPR 2。Legacy 完成一次 20 cold + 20 warm 长序列；WebGPU 的 2 cold + 2 warm 短测通过，实际 backend 为 WebGPU，未捕获运行时错误。

WebGPU 长序列在第 21 次导航 F37 停止：路由与 UI 已切到目标帧，页面持续显示“加载点云…”，但没有 geometry/color 完成事件，也没有 console、page error 或 unhandled rejection。现有证据将范围收敛到 manifest 后、geometry 提交前的 PCD fetch、Worker decode 或 abort 生命周期；WebGPU 独有的六相机与相邻帧 depth 预取可能增加 Safari 资源压力，但尚不能定为根因。

## Device lost

在独立 Chrome 测试进程中触发 GPU 进程丢失后，工作台自动恢复为 Legacy WebGL2，旧 canvas 被移除，34,688 个点保持完整，renderer count 为 1，页面没有运行时错误。该测试没有操作用户现有浏览器，也没有制造物理 OOM。

## 2026-09-15 复测与资格补齐

同一台 MacBook Pro（Apple M4 Pro、48 GiB、macOS 26.6.2），Chrome 152.0.7977.84，仓库已演进到
v0.25.3：Scene 时间轴改为默认折叠的 compact 形态，面板显隐移入工作台设置对话框。全部测试使用默认
flag，无 unsafe WebGPU/Vulkan 或黑名单绕过。

### Raster Mask one-pass 复测（当前源码）

使用生产 harness `mask:webgpu-operation-bench`，native Chrome 有头运行：

- 六类 correctness case（1024²/2048²/4K/非字对齐/重叠/分离 ROI，radius 2，warmup 3 + 20 测量）
  全部通过 route parity：CPU/WebGPU patch checksum、合并保存与重载 checksum 一致；1024² 按生产
  策略留在 CPU（below-pixel-threshold）。
- p95 收益：2048² 25.4%、4K 30.0%、非字对齐 30.6%、重叠 30.6%、分离 31.2%。
- radius 31 长会话：2048²/4K 各 100 次测量，796 个 worker job 0 失败、0 运行时回退
  （backend 稳定 `webgpu`、fallback reason 为空）、Long Task 0、单一 GPU owner worker、
  capacity 全程 1,572,912 bytes，dispose 后 worker 数与 GPU 字节归零。

### 超大图 tile 浏览（首次 macOS 资格）

夹具为 `P-LARGE-IMG` 真实图片（123MP PNG、233MP JPEG、47MP 带 alpha 超高图），金字塔全部 ready。
DPR 1 与 DPR 2（Retina）各一轮，覆盖三张图的冷启动、连续 pan/zoom、六个交互块长会话与切题：

- 冷启动：stage 可见 352–605 ms，target coverage 1 settle ≤856 ms；可见 tile 4–18 个，
  首屏 level 3/4。
- 原图请求全程 0（131 MB PNG 源图从未被下载），tile 错误 0，HTML image fallback 0，
  URL 重签 0。
- 连续 pan/zoom p95 frame delta 9.7–10.2 ms（≈100 FPS，门槛 ≥30 FPS），Long Task 全程 0。
- 长会话 retained bytes 进入平坦 plateau（DPR 1：123MP 9.7 MB、233MP 3.7 MB；DPR 2：123MP
  38.2 MB、233MP 14.7 MB、47MP 14.9 MB），远低于 128 MiB task-scoped 预算；staleCommits 0，
  切题后 coverage 恢复 1，无跨任务旧 tile。

### 3D 点云多轮资格（Apple Metal 第二厂商）

正式 benchmark 先适配当前 UI：进入后展开默认折叠的 Scene 时间轴，三视图精修改经工作台设置对话框的
面板开关打开。随后以 `POINTCLOUD_BENCH_ROUNDS=3` 执行（每轮 Legacy WebGL2 与 WebGPU 各 20 cold +
20 warm）：

- 第 1、3 轮完整通过 promotionGate；第 2 轮仅 `warmReadyRgbP95ImprovementAtLeast60Percent`
  未达门（53.95%，该轮 legacy p95 异常低），绝对门全部通过。
- 三轮共享检查全部通过：真实 WebGPU backend、renderer owner 恒为 1、零实验路径 Canvas 全图回读、
  空闲零提交、depth raster cache hit 0.85–1.0、无运行时错误。
- p95：cold geometry WebGPU 120–145 ms vs Legacy 137–145 ms；warmReady RGB WebGPU 59–64 ms
  vs Legacy 155–161 ms（2.4–2.7×）；三视图首开 43–49 ms（≤50 门，另有一轮 51.9 ms 边界样本）。
- 相对 09-05 报告，两个 backend 的绝对 p95 都升高约四成（应用 UI 演进所致），相对收益与资源合同
  保持不变。

### 结论边界

macOS Chrome/Metal 上的 Raster Mask one-pass、超大图 tile 浏览与 3D WebGPU 多轮资格缺口已补齐。
Safari 长序列点云问题未复测、仍未解决；Linux Wayland 与 Windows D3D12 依旧没有机器，保持
`not tested`。

## 重跑

```bash
POINTCLOUD_BENCH_BASE_URL=http://localhost:3000 \
POINTCLOUD_BENCH_PROJECT_ID=<project-uuid> \
POINTCLOUD_BENCH_TASK_ID=<task-uuid> \
pnpm --dir apps/web pointcloud:renderer-bench
```

超大图 tile 夹具按 [DEV.md](../../DEV.md) 的 `image:seeds` + `seed_large_images.py` 流程准备后，
用 `__imageTileDiagnostics` 驱动 pan/zoom 与长会话采样（本报告的临时 runner 不入库）。

macOS 自动使用已安装的原生 Chrome 与 Metal；其他平台保留 Chromium Vulkan 实验启动参数。可用 `POINTCLOUD_BENCH_DPR=2` 验证 Retina，或用 `POINTCLOUD_BENCH_ROUNDS=3` 执行多轮资格测试。benchmark 会临时打开相机上色，并在结束后恢复用户偏好；任务没有可见 3D 框时会创建临时框并在 `finally` 删除。

仓库只保留本报告、正式 benchmark 和回归测试；原始 JSON、截图、日志及临时 runner 不纳入版本控制。

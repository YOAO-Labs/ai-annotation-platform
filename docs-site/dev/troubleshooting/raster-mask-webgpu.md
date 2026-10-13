---
audience: [dev, ops]
type: troubleshooting
status: experimental
last_reviewed: 2026-07-31
---

# Raster Mask WebGPU 未命中或回退 CPU

## 症状

- 开启实验环境变量后，大 ROI morphology 仍显示 `backend=cpu`；
- 诊断显示 `disabled`、`adapter-unavailable`、`initializing`、`budget-insufficient`、`device-lost` 或
  `gpu-runtime-failed`；
- Linux 部署机器有 NVIDIA GPU，但访问页面的浏览器没有命中 WebGPU。

这些状态都不代表 Mask 计算失败。只要 CPU Worker 成功，XOR patch、undo / redo 与保存语义保持一致；
符合条件的大 ROI 会继续使用 packed separable CPU，而不是重新 materialize dense alpha。

## 先确认资源边界

WebGPU 运行在访问工作台的用户浏览器中，不运行在 API、Celery、PostgreSQL、对象存储或 ML backend
容器中。服务器有 GPU 不会让远程浏览器取得 adapter；反过来，客户端 MacBook 的浏览器可以使用本机
GPU，而 Linux 服务端完全不参与该计算。

`VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` 是 Vite build-time 开关：

- 默认 `true`，仅在大 ROI morphology 请求时惰性加载 provider 并探测 adapter；
- 设为 `false` 后重建可紧急回滚；只重启 API 或 Celery 不会改变已生成的浏览器 bundle；
- 该开关不是用户设置，也不会启用 WebCodecs 视频硬件解码。

## 诊断状态

| 状态 / reason                | 含义                                                        | 处理                                                                   |
| ---------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------- |
| `disabled` / `gate-disabled` | 当前前端 bundle 显式关闭 WebGPU gate                        | 预期走 packed/dense CPU；需要恢复时以 `true` 重建前端                  |
| `initializing`               | adapter / device / pipeline 仍在后台初始化                  | 当次操作走 CPU；不要让 UI 等待初始化                                   |
| `adapter-unavailable`        | 浏览器无法取得兼容 adapter                                  | 保留 CPU；检查浏览器、OS、驱动与安全上下文，不把服务端 GPU 当作证据    |
| `below-pixel-threshold`      | input 小于 `2048²`                                          | 预期行为；小 ROI 使用 dense CPU                                        |
| `unsupported-operation`      | 不是 square dilation 或 radius 不在 `1..31`                 | 预期 dense CPU 路由，不扩展 kernel 前先跑端到端 A/B                    |
| `budget-insufficient`        | GPU capacity 或 CPU prospective bytes 超过各自独立 hard cap | 先区分 `cpuBudgetBytes` / `gpuBudgetBytes`；不能靠提高另一项绕过准入   |
| `device-lost`                | 浏览器报告 device 丢失                                      | 当前请求复用 packed CPU，并进入 cooldown；切 task 不会主动清除 circuit |
| `gpu-runtime-failed`         | buffer、queue、submit、map、readback 或 patch 阶段失败      | 当前请求复用 packed CPU；连续第二次失败后本页固定 CPU                  |

详细阶段读 `failureStage` / `webGpuFailureStage`。允许值包括 `adapter-request`、`device-request`、
`shader-compile`、`pipeline-create`、`buffer-create`、`queue-write`、`encode`、`submit`、`map`、
`readback-validate` 与 `patch-build`。诊断不得转存浏览器原始错误消息或 adapter/driver 字符串。

第一次失败后应看到 `webGpuCircuitState=cooldown` 和非零 `webGpuCooldownRemainingMs`。30 秒到期后只有新的
eligible foreground request 会触发一次重试；连续第二次失败变为 `page-fixed`。新 Mask session 不清除
该状态，完整 pool/provider lifecycle dispose 后才恢复资格。

## 验证默认开启与显式回滚

默认构建尚未执行 morphology 时允许保持 `webGpuState=idle` 且 `initAttempts=0`；第一次符合条件的请求才会
进入 ready 或稳定 fallback。以 `VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU=false` 重建后，检查：

```text
webGpuGateEnabled=false
webGpuState=disabled
counters.initAttempts=0
gpuOwnerWorkers=0
gpuAllocatedBytes=0
```

即使浏览器以可用的 Vulkan / Metal / D3D adapter 启动，显式回滚构建的这些字段也必须保持零。若出现
初始化次数，说明 false build-time gate 未被正确裁剪，应视为回归。`baseCacheRetainedBytes` 与
`sourceScratchCapacityBytes` 在符合条件的 packed CPU 操作后允许非零；它们已经与 GPU provider 生命周期
解耦，并受 `cpuComputeBudgetBytes` 约束。

## 研发 A/B

生产 runner 必须连接带对应 gate 的 Vite 页面：

```bash
pnpm --filter @anno/web mask:webgpu-operation-bench
pnpm --filter @anno/web mask:packed-cpu-bench
pnpm --filter @anno/web mask:webgpu-separable-bench
```

第三条只加载 benchmark-only 的横/纵可分离候选，用于复核已记录的 production no-go；它不会改变
Worker route。强制 Vulkan 只允许用于研发资格实验，不要写入生产 Chrome 启动参数、Docker 配置或默认 Playwright
项目。结果至少核对 XOR patch checksum、save checksum、Long Task、owner 数、GPU / cache / scratch bytes
plateau 和 dispose 后资源归零；只记录 shader 时间不能作为开启依据。第二条 runner 比较 direct oracle 与
production separable CPU kernel，默认不触发 adapter。

分段诊断中，成功 GPU 请求应满足：

```text
inputAlphaBytes=0
packedSourceBytes>0
xorReadbackBytes>0
cpuStrategy=not-run
prepareStrategy=packed-cache 或 direct-rle
gpuSourceCapacityBytes>0
gpuXorCapacityBytes>0
gpuReadbackCapacityBytes>0
```

`backendPrepareMs` 是构造 packed input 的总时间。进一步按策略读取：

| 字段                                       | 解释                                                                  |
| ------------------------------------------ | --------------------------------------------------------------------- |
| `prepareStrategy=packed-cache`             | 使用 immutable base tile cache 与 word-span assemble                  |
| `prepareStrategy=direct-rle`               | cache 预算不足或被清空，仍走 WebGPU，但直接扫描 RLE 构造 packed input |
| `prepareStrategy=dense-cpu`                | 小 ROI、不支持操作或 packed CPU 未准入，使用 dense CPU                |
| `directRleScanMs`                          | direct-RLE 策略扫描 canonical RLE 的时间                              |
| `baseCacheFillMs`                          | 本次 cache misses 从 RLE 填充 immutable tiles 的时间                  |
| `packedAssembleMs`                         | base tile spans 组装到 row-aligned source scratch 的时间              |
| `dirtyOverlayMs`                           | dirty overrides 以 masked overwrite 覆盖 scratch 的时间               |
| `baseCacheHitTiles` / `baseCacheMissTiles` | 本次 ROI 的 base cache 复用与填充数量                                 |
| `baseCacheEvictedTiles`                    | 本次准入为满足硬上限淘汰的 tile 数量                                  |
| `baseCacheRetainedBytes`                   | Worker 当前保留的 immutable cache 字节                                |
| `sourceScratchCapacityBytes`               | grow-only scratch 的实际 capacity；可大于本次 packed payload          |

同一 warm ROI 仍持续 miss，通常表示 session 被反复注册、Worker 被替换、cache 预算无法准入或工作集超过
LRU 上限。先对照 `workersReplaced`、session 数、`prepareStrategy` 和 retained bytes，不要通过提高全局内存
限制来掩盖生命周期问题。50% overlap pan 应只 miss 新进入 ROI 的 tiles；disjoint ROI 第一次全 miss 属于
预期，返回仍在 LRU 内的 ROI 应恢复全 hit。

`gpuUploadSubmitMs` 与 `gpuReadbackMs` 分别表示提交前工作和 map / clone 回读，`diffOrPatchMs` 是 non-zero
XOR words 生成 tile patch 的时间。若 `backend=cpu-fallback`，应看到
`cpuStrategy=packed-separable`、`denseTransientBytes=0`、`packedIntermediateBytes>0` 与
`fallbackMaterializeMs=null`；这证明 GPU 中途失败复用了 prior packed source，没有重建 dense alpha。
普通 gate / capability CPU 路由在大 ROI 上也允许使用同一 packed source。

CPU/GPU 双预算的默认设备档位是：

| `navigator.deviceMemory` | CPU compute budget | GPU buffer budget |
| -----------------------: | -----------------: | ----------------: |
|               `<= 2 GiB` |             32 MiB |             0 MiB |
|                 常规档位 |             64 MiB |            64 MiB |
|               `>= 8 GiB` |            128 MiB |           128 MiB |

`gpuBufferBudgetBytes=0` 不得伴随 `cpuComputeBudgetBytes=0`。若低内存客户端连 CPU 都报 budget error，应先
核对 `cpuTransientBytes`、`denseTransientBytes`、`packedIntermediateBytes` 与
`patchUpperBoundBytes`，不要把 GPU budget 调高当作修复。

成功 GPU 请求的 `xorOutputStrategy` 应为 `dense-word-scatter`。`xorTotalWords` / `xorNonZeroWords` /
`xorWordDensity` 用于判断 core XOR 稀疏度，`xorChangedPixels` / `xorTouchedTiles` 描述实际 patch 工作量；
`xorScanMs`、`xorPatchAllocateMs`、`xorPatchScatterMs` 与 `wordPatchBuildMs` 用于定位 dense plane 扫描、tile
payload 分配和 byte-span 写入。当前 production 不含 atomic sparse records、overflow 或 record buffer；不要
把缺少 sparse counters 误判为诊断未加载。

## 回滚

将 `VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU=false` 后重建前端。CPU Worker session、主线程 sparse packed
tile、XOR history 与 canonical COCO RLE 保存路径不依赖该 gate，不需要回滚 API 或数据库。Worker 内的
immutable base cache 同时服务 packed CPU 与 WebGPU；关闭 gate 后它仍可保留，但必须受 CPU hard budget
约束，并在最后 session release、Worker replacement 或 dispose 后归零。

最近最多 20 次 typed compute event 只保存在浏览器内存，并在任务切换或 pool dispose 后清空。提交 BUG
报告时会附带 backend、CPU strategy、稳定 reason/stage、分段耗时、像素数、预算/容量、cache 与 circuit；
不会附带完整 task id、Mask 内容、adapter/driver 或浏览器原始错误。

## 相关

- [标注模块](../concepts/annotation-module)
- [环境变量](../reference/env-vars)
- [ADR-0056](/dev/adr/archive/0056-raster-mask-persistent-client-compute-session)
- [ADR-0061](/dev/adr/archive/0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit)

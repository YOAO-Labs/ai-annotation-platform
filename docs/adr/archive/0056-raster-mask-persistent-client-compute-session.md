# 0056 — Raster Mask 大 ROI 使用持久客户端计算会话与可选 WebGPU 后端

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** core team
- **Supersedes:** —（扩展 [ADR-0054](./0054-raster-mask-large-canvas-memory-and-tiles.md) 的 sparse tile、固定 Worker pool 与 XOR history，不改变 canonical COCO RLE 持久格式）

> 默认关闭的 rollout 决策已由 [ADR-0060](./0060-default-enable-capability-gated-client-acceleration.md) 取代；持久会话、CPU fallback 与资源合同继续有效。
> dense CPU fallback 与联合 compute budget 随后由
> [ADR-0061](./0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit.md) 的 packed CPU fallback、双预算和
> 独立 circuit 取代。

## Context

大画布 Raster Mask 已以 immutable base RLE 加 materialized dirty tile 取代整图 alpha，但 tiled
morphology 仍在主线程重建连续 ROI、执行逐像素计算，再扫描 before / after 生成 history。4K ROI 会造成
明显同步工作，也让 Worker 已持有的 base RLE run index 没有被计算链路复用。

WebGPU 单核与 Dedicated Worker 原型证明大 ROI square dilation 有加速空间，但也给出三个限制：小 ROI
比 CPU 慢；adapter / device 初始化可能耗时数秒；Linux X11 默认 Chrome 可能取不到 adapter。WebGPU
使用的是访问页面的客户端浏览器 GPU，与部署 API、Celery 或 ML backend 的 Linux 主机 GPU 无关。

| 方案                                                  | 主要卖点                                                                    | 主要劣势                                                                    |
| ----------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **A. 持久 CPU Worker 会话 + default-off WebGPU 候选** | 默认路径移出主线程；复用 base session；GPU 可整体关闭；结果直接进入 history | 协议与资源状态机更复杂；需要维护客户端资格矩阵                              |
| B. 继续主线程 morphology                              | 修改最少                                                                    | 大 ROI Long Task 与重复 materialize 保留                                    |
| C. 每次向独立 GPU Worker 传整幅 alpha                 | 原型简单                                                                    | 重复 clone / pack / unpack；会产生第二份协议与 shader                       |
| D. 服务端 GPU 计算                                    | 可集中管理硬件                                                              | 交互增加网络 RTT、上传草稿和并发隔离；部署 GPU 与浏览器渲染资源不是同一边界 |

## Decision

### D1. CPU Worker session 是默认且完整的正确性路径

- `RasterMaskWorkerPool` 按 task 持有 base RLE session；Worker 内建立 run index。
- morphology 请求只携带 core、halo 后的 input、operation、source revision 与相交 dirty tile 的 packed
  overrides。Worker 从 immutable base 加 overrides 重建 ROI。
- CPU 路径复用 production `applyMaskMorphology()`，支持现有全部 morphology operation 与 kernel。
- ROI core tile 采用最多两个并发 decode 的有界物化，不用一次性请求填满 32 项 Worker queue。
- 取消、超时、Worker 替换和 session 释放继续服从固定 pool 生命周期；没有 Worker 时不回到主线程执行。

### D2. packed tile 是会话真值，canonical RLE 是持久真值

- 每个 materialized tile 同时持有显示 alpha、immutable `baseBits` 与可写 `currentBits`；三者都计入 tile
  admission budget。
- brush、lasso、undo / redo 只同步 touched bits。dirty 由 `currentBits` 与 `baseBits` 比较，不维护平行
  RLE。
- 保存时仍只把 dirty tile 交给 Worker，与未访问 base 区间合并成 canonical COCO RLE。刷新后重新从该
  RLE 建立会话，不持久化 GPU buffer 或 packed cache。

### D3. Worker 只回传 exact XOR tile patches

- Worker 只为 core 内发生变化的 tile 返回 non-empty XOR bitset、changed pixel count 与 bounds。
- store 在校验 session、sha、source revision、tile revision、patch 尺寸、尾位与 core 边界后一次性应用全部
  patches；任何校验失败都零部分写入。
- 同一 patch 同时用于 UI 更新与 `MaskHistoryCommand`，undo / redo 再次 XOR；不再重建 before checkpoint
  或扫描整幅 after。

### D4. WebGPU 是客户端、单 owner、默认关闭的可删除后端

- `VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` 是 build-time 运维 gate，默认 false。关闭时 Worker 不
  dynamic-import provider，也不请求 adapter。
- 只有 slot 0 可持有一个 device / pipeline 与 grow-only source、target、readback buffer；pool 诊断显式
  记录 owner、allocated bytes、backend 与 fallback reason。
- 首批只支持 `square dilate`、radius 1–31、ROI 至少 `2048²`。Standard 档最多准入已测 `2048²`，High
  档可准入 4K；Low 档不分配 GPU compute budget。
- 初始化不阻塞首次操作。warming、无 adapter、预算不足、不支持操作、device lost 或 runtime error 都在
  同一 Worker 使用已有 source 精确回退 CPU。
- provider 的 adapter、device、pipeline、buffer 与 shader 只存在于 production module；benchmark 直接调用
  production pool / store，不保留独立 WGSL 副本。

### D5. 默认开启需要新的跨平台资格决策

Linux RTX 3090 强制 Vulkan 的结果只作为工程性能证据。macOS、Wayland、Windows 无 flag 的 correctness、
p95、device lost 与长会话资源矩阵通过前，gate 保持 false，也不向用户展示开关。未来默认开启必须另作
显式决策；不能以服务端存在 GPU、`navigator.gpu` 存在或单个 shader benchmark 代替资格证明。

## Consequences

正向：

- default-off 构建也能获得 CPU Worker session、packed dirty override 与一次 XOR patch 的主线程减负。
- GPU 失败不改变 Mask 语义或保存格式，默认构建没有 adapter 请求和 GPU 资源所有者。
- history、tile cache、Worker session 与 GPU buffer 都有独立硬预算和显式释放证据。
- benchmark 覆盖生产 store、Worker、patch application、history retain、undo 与 save，不再把实验核结果当成
  产品延迟。

负向：

- materialized tile 同时保留 alpha 与两份 packed bitset，必须持续纳入 admission / eviction 测试。
- slot 0 承担 morphology affinity；长 GPU job 期间普通任务需要由其它 slot 继续处理。
- WebGPU 目前只加速一个 operation / kernel 组合；扩展 shader 前仍需独立端到端证据。
- build-time gate 的变更需要重建前端，不能用重启 API 或 Celery 代替。

## Alternatives Considered

**方案 B（主线程继续计算）**：不能消除已测大 ROI 同步成本，也继续重复 Worker 已拥有的 base session。
拒绝。

**方案 C（整幅 alpha GPU Worker）**：早期原型证明计算核可行，但 tile clone、materialize、pack、unpack 与
RLE 会吞掉大部分收益，并形成重复 shader / protocol。拒绝。

**方案 D（服务端 GPU）**：交互草稿必须跨网络传输，增加延迟、鉴权、并发和数据生命周期问题；也不能帮助
浏览器 Konva 渲染。拒绝用于该交互路径。

## Notes

- 生产代码：`apps/web/src/pages/Workbench/stage/shared/rasterMaskWorkerRuntime.ts`、
  `rasterMaskWorkerPool.ts`、`sparseMaskTileStore.ts`、`rasterMaskWebGpu.ts`。
- 研究证据：[`docs/research/21-webgpu-video-workbench.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/21-webgpu-video-workbench.md)。
- 实施计划：[`docs/plans/archive/2026-07-29-v0.23.16-raster-mask-persistent-compute-webgpu.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-29-v0.23.16-raster-mask-persistent-compute-webgpu.md)。
- 相关 ADR：[ADR-0054](./0054-raster-mask-large-canvas-memory-and-tiles.md)。

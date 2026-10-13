# 0061 — Raster Mask 大 ROI 使用 packed CPU fallback 与独立 WebGPU 熔断

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** core team
- **Supersedes:** ADR-0056、ADR-0057 与 ADR-0058 中“GPU 不可用时重新 materialize dense alpha”的 fallback 与联合 compute budget 决策

## Context

ADR-0057 与 ADR-0058 已让 WebGPU 成功路径复用 row-aligned packed source，但 gate 关闭、adapter 不可用、
初始化中、预算不足或运行时失败时仍回到 dense alpha morphology。Linux X11 默认 Chrome 的
`requestAdapter()` 可稳定返回 `null`，这会让不能使用 WebGPU 的客户端承担 2048² 约 250–292 ms、
4K 约 483–529 ms 的平均 p95。

旧路由还把 CPU scratch、packed base cache 与 GPU buffers 计入同一个 compute budget。低内存设备或
关闭 WebGPU gate 时把预算置零，会错误地把“不能使用客户端 GPU”扩展成“不能执行 CPU fallback”。
production Worker 协议同时保留了已经结束资格赛的 per-bit benchmark selector，增加了无实际产品价值的
状态面。

## Decision

### D1. 大 ROI square dilate 的 CPU fallback 使用 packed separable kernel

- `square dilate`、radius `1..31`、input 至少 `4,194,304` pixels 时，从 immutable base RLE 与 dirty
  packed overrides 构造一次 packed source。
- CPU 先按输出 words 做水平 expansion intermediate，再在垂直窗口内 OR，并与 source center XOR。
- 结果继续交给 ADR-0059 选定的 dense word-scatter builder；history patches、undo / redo、save 与
  canonical COCO RLE 合同不变。
- gate 关闭、无 `navigator.gpu`、adapter/device 初始化失败、GPU budget 不足和 GPU runtime failure
  都复用该 packed source。运行时失败不再重新 materialize input-sized dense alpha。
- 小于 crossover、不支持的 operation/kernel/radius 或 packed CPU prospective admission 失败时保留
  dense CPU；dense 路径也必须先通过自己的 hard budget。

### D2. CPU 与 GPU 使用独立预算

- `cpuComputeBudgetBytes` 只约束 CPU packed/dense transient、source scratch、XOR output、patch upper
  bound 与 retained packed base cache。
- `gpuBufferBudgetBytes` 只约束 prospective source、XOR target、readback 与 params GPU capacities。
- `deviceMemory <= 2 GiB` 的默认档位为 CPU 32 MiB、GPU 0；常规档位为 64 MiB，高内存档位为
  128 MiB。
- WebGPU gate 或 GPU budget 为零不会把 CPU budget 清零。两种 CPU candidate 都不能接纳时返回稳定的
  CPU budget error，不允许分配后再宣告超限。

### D3. WebGPU provider 使用一次冷却重试与 page-fixed circuit

- 第一次 adapter、device、shader、pipeline、buffer、queue、encode、submit、map、readback 或
  patch-build failure 后销毁 device/buffers，进入 30 秒 cooldown。
- cooldown 到期后的下一次 eligible foreground request 只重试一次；成功清零 failure count，连续第二次
  失败后当前 page/provider lifecycle 固定 CPU。
- 新 Mask session 不清除 cooldown 或 page-fixed circuit。最后 session release、Worker replacement 或
  page lifecycle dispose 会销毁完整 provider，新的 provider lifecycle 才重新取得资格。
- 对产品暴露稳定低基数 fallback reason；诊断另记 failure stage、cooldown、连续失败数和 circuit state，
  不记录浏览器原始错误、adapter 名称或 driver string。

### D4. 删除 production benchmark selector，保留有界本地诊断

- production `RasterMaskMorphologyRoiRequest` 不再接受 per-bit / word-scatter selector；Worker 永远使用
  ADR-0059 的 word-scatter winner。
- direct packed kernel只作为 test/benchmark oracle；production 只使用 separable winner。
- 浏览器内存保留当前任务最近 20 次 typed compute events，任务切换或 pool dispose 清空。BUG 报告可附带
  route、backend、CPU strategy、稳定 reason/stage、分段耗时、像素数、预算/容量、cache 与 circuit，
  但不附完整 task id、Mask 内容、adapter/driver 信息或浏览器实现错误消息。

## Consequences

正向：

- 两轮、三个 radius、2048²/4K 共 12 个 production case 的端到端 p95 相对 dense baseline 改善
  80.1%–91.3%，全部超过 35% 主门。
- direct → separable kernel 在 radius 8/31 的 p95 改善 88.4%–96.6%；radius 1 仍保持
  20.0%–36.9% 的非负收益。
- 12/12 patch、save、reload checksum 与冻结 baseline exact；CPU fallback 不依赖 GPU availability。
- RTX 3090 强制 Vulkan 的真实成功路径仍保持 WebGPU backend、单 owner、稳定 GPU capacity、零 Long
  Task，dispose 后资源归零。
- 连续失败不会产生 adapter/device 重试风暴，且 BUG 报告能定位稳定 failure stage。

负向：

- packed separable 仍需一份水平 intermediate；4K code-derived CPU transient 约 8.43 MiB，必须继续计入
  hard budget。
- radius 31 的垂直 OR 仍随 radius 增长；当前数据支持 crossover，但不等于已完成 WebGPU separable、
  deque 或 ring-buffer 优化。
- 30 秒 cooldown 会让偶发首次故障后的短时请求继续使用 CPU，这是避免重试风暴的明确取舍。

## Alternatives Considered

**始终保留 dense CPU fallback**：实现简单，但在常见无 adapter Linux 浏览器上留下数量级更慢的已知路径，
拒绝。

**GPU 失败立即无限重试**：可能从瞬时故障恢复更快，但会重复承担 adapter/device/pipeline 成本并制造
故障风暴，拒绝。

**按 adapter vendor 或 User-Agent 路由**：高熵、易漂移，且不能证明当前 device、budget 和 runtime
健康，拒绝。

**把 CPU 与 GPU budget 继续合并**：无法表达“无 GPU 但 CPU 可安全执行”，拒绝。

## Evidence

- 计划：[`docs/plans/archive/2026-07-31-v0.23.21-raster-mask-packed-cpu-fallback-routing.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-31-v0.23.21-raster-mask-packed-cpu-fallback-routing.md)
- 研究：[`docs/research/21-webgpu-video-workbench.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/21-webgpu-video-workbench.md)
- 数据：[`docs/research/data/21-mask-packed-cpu-fallback-ab.json`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/data/21-mask-packed-cpu-fallback-ab.json)

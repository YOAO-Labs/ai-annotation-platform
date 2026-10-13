# 0065 — Raster Mask WebGPU 保留 one-pass kernel，不采用可分离候选

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** core team
- **Supersedes:** —

## Context

现有 packed `square dilate` WGSL 在一个 compute pass 中同时完成水平扩张、纵向聚合、与 source XOR。
其理论操作数随 radius 接近二次增长，因此评估了 horizontal intermediate + vertical XOR 两阶段候选。
候选会新增一个与 `ceil(coreWidth / 32) × inputHeight × 4` 等大的 storage buffer、第二个 pipeline 和
第二次 dispatch；只有端到端 operation p95 的稳定收益能证明这些复杂度合理。

Linux X11、Chrome 150、RTX 3090、有头强制 Vulkan 的同页资格 runner 对 one-pass 与 separable 交错
执行。每个 bucket 两轮、每轮 3 次预热 + 10 次记录，覆盖 2048²、3840×2160、4096²，radius
`8/16/31` 与 contour/dense/checker/edge 四类输入。进入生产的门是每轮 p95 改善至少 10%，且两轮
算术平均至少 15%。

50 组 tail、非 32 对齐、边缘、稠密、checker 与确定性随机输入的 XOR 逐 word exact；但性能矩阵只有
9/36 bucket 过门，27/36 未过门。2048² 的 12 个 bucket 全部失败；4K 与 4096² 的局部 radius 31
收益也不能跨输入分布稳定成立。候选在 4096² 还把 provider buffer plateau 从 6,291,504 bytes
增加到 8,388,656 bytes。两条路径均为零 Long Task，dispose 后归零，因此结论由端到端收益门决定，
不是 correctness 或资源泄漏失败。

## Decision

1. production Raster Mask Worker 继续只使用现有 one-pass WebGPU kernel；不增加 radius/ROI crossover、
   adapter-name 分支、intermediate buffer、第二套 pipeline 或 protocol selector。
2. separable 实现只保留在显式运行的资格工具中，不被 production Worker 导入，也不进入普通前端 bundle。
   资格工具用于保存本轮可复现证据，不代表产品 route。
3. `VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` 继续默认 `true`。该决定不否定现有 one-pass 路径；gate、
   4 MP 准入、独立 CPU/GPU budget、单 owner、cooldown/page-fixed circuit 与 packed CPU fallback 均保持。
4. Linux X11 默认 Chrome 的 adapter 仍不可用并精确回退 packed CPU。Linux Wayland、macOS Metal、
   Windows D3D12 与 Safari/Edge 实机状态明确为 `not tested`，不得从强制 Vulkan结果外推。
5. 只有新的 production 诊断证明 one-pass GPU stage 再次成为主导瓶颈，且新候选能在数据分布无关、
   跨目标平台的同包矩阵中通过相同正向收益门，才重新立项；不直接复活本次 crossover。

## Consequences

正向：

- production 没有不可达 shader、额外 GPU allocation、故障分支或长期双 kernel 维护成本。
- 现有 one-pass 的 exact history/save、单 owner、资源 plateau 和 CPU fallback 合同不变。
- 局部 r31 快样本不会被误写成所有大图或所有客户端都更快。

负向：

- 部分 4K/4096²、radius 31 负载不会获得本次候选观测到的局部收益。
- 未测试平台仍依赖 capability-first fallback 和 kill switch，不能声明 one-pass 性能资格。
- benchmark-only 模块会保留少量研发代码，但生产构建必须持续证明不含其 shader/pipeline 标签。

## Alternatives Considered

**只在 radius 31 + 4K/4096² 启用 separable**：不同输入图案和轮次仍有 bucket 未达到单轮 floor，
无法得到与内容无关的静态 route，拒绝。

**按 adapter 或输入密度动态选择**：会引入高熵硬件策略或先扫描内容的额外成本，且当前数据没有给出
稳定门槛，拒绝。

**统一替换为 separable**：2048² 全部 bucket 与多组 r8/r16 明显不达门，同时增加 intermediate，拒绝。

## Notes

- 资格 runner：`apps/web/scripts/benchmark-mask-webgpu-separable.mjs`
- benchmark-only provider：
  `apps/web/src/pages/Workbench/stage/shared/rasterMaskWebGpuSeparableQualification.ts`
- 原始数据：`docs/research/data/21-mask-webgpu-separable-qualification.json`
- one-pass 长会话：`docs/research/data/21-mask-webgpu-one-pass-long-session.json`
- X11 默认回退：`docs/research/data/21-mask-webgpu-x11-default-fallback.json`

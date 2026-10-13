# 0059 — Raster Mask 保留 dense word-scatter，不采用 atomic sparse compaction

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** core team
- **Supersedes:** —（细化 [ADR-0057](./0057-raster-mask-webgpu-packed-xor-contract.md) 的 XOR result → history patch 数据面）

> D3 的默认关闭 rollout 决策已由 [ADR-0060](./0060-default-enable-capability-gated-client-acceleration.md) 取代；dense word-scatter 与 sparse no-go 决策继续有效。
> production benchmark selector 与旧 per-bit builder 随后由
> [ADR-0061](./0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit.md) 删除；word-scatter winner 不变。

## Context

ADR-0058 将 warm packed prepare p95 降到约 2–7 ms 后，dense core XOR words → 512² history tile patches
成为约 10–16 ms 的最大 CPU 阶段。现有实现逐个 set bit 计算像素、tile 和局部坐标；canonical workload
的 non-zero word density 只有 1.70%–3.06%，因此同时值得验证 CPU word scatter 与 GPU sparse records。

候选必须继续输出既有 `MaskHistoryPatch[]`，保持 patch、undo / redo、save / reload 与 canonical COCO RLE
exact；任何 sparse 输出还必须有固定容量、overflow dense recovery、无序 record 校验和完整 compute ledger。

## Decision

### D1. Production 使用 dense XOR + CPU word-scatter

- shader 与 provider 继续只生成和回读 dense core XOR words。
- Worker 以 popcount 和首尾 bit 计算 changed summary，再把每个 non-zero word 按 history tile 边界拆成
  byte spans 写入 patch；不再逐 set bit 重算坐标。
- 最终 patches 按 `(tileY, tileX)` 排序；edge tile、非 8 / 32 / 512 对齐和 tail bits 继续精确校验。
- 旧 per-set-bit builder 只由 benchmark selector 与 unit golden 单选，production 默认请求只运行 word-scatter。

### D2. Atomic sparse compaction no-go，prototype 全量删除

prototype 使用固定 `coreWords / 4` record capacity，同一 dispatch 始终写 dense recovery target，并对每个
non-zero word atomic append `(wordIndex, xorWord)`。canonical 样本 20/20 无 overflow，payload 约
32–35 KiB，correctness 与资源测试通过；但最终单遍 builder 的 `readback + patch` p95 在 2048² 退化
21.7%，4K 仅改善 1.4%，低于 10% 单轮下限。

因此 production 不保留 sparse shader binding、storage / readback buffer、record protocol、validator、
overflow recovery、circuit breaker、metrics 或 tests。不能以 default-off 为理由保留未达标的半套实现。

### D3. Default-enable 资格不变

最终 dense-only bundle 在 Linux RTX 3090 强制 Vulkan 下通过 2048² / 4K 两轮端到端门，但该证据不外推
到 macOS、Wayland 或 Windows。`VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` 继续默认 false，是否默认开启
仍需独立的无 flag 跨平台 correctness、长会话和性能矩阵。

## Consequences

正向：

- 最终两轮 2048² total p95 相对上一封版改善 15.8% / 33.6%，4K 改善 33.1% / 42.3%。
- 4K patch p95 从约 15–16 ms 降到 2.8 ms；patch、save、reload checksum exact，Long Task 为 0。
- 不增加 GPU buffer、atomic contention、record validation、overflow 或生命周期状态面。
- dense normal path 与既有 CPU fallback 合同不变，默认 bundle 继续隔离 provider 与 shader。

负向：

- 仍需 map 完整 dense core XOR plane；极稀疏 workload 没有按 record payload 比例减少 readback bytes。
- 2048² 第一轮保留调度尖峰后 patch p95 只改善 31.4%，说明 tail 仍受 JS 调度和 tile payload 分配影响。
- 若未来 patch allocation 再次成为首要瓶颈，必须用新的 end-to-end 数据重新立项，不能直接恢复本次
  atomic prototype 或升级为 prefix-sum compaction。

## Alternatives Considered

**保留 bounded atomic records**：payload 明显更小，但联合 p95 门失败，拒绝。

**GPU prefix-sum / radix compaction**：可能生成有序 records，但增加 pipeline、pass、scratch 和同步；当前
single-pass 已无增量收益，拒绝。

**GPU 直接生成 history tile payload**：动态 tile allocation 与现有 patch 协议耦合过深，拒绝。

**GPU-resident mutable source**：upload / submit 仍不是首要瓶颈，会引入 revision replay 与 device-lost 重建，
推迟。

## Evidence

- 计划：[`docs/plans/archive/2026-07-30-v0.23.19-raster-mask-sparse-xor-compaction.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-30-v0.23.19-raster-mask-sparse-xor-compaction.md)
- 研究：[`docs/research/21-webgpu-video-workbench.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/21-webgpu-video-workbench.md)
- 数据：[`docs/research/data/21-mask-webgpu-sparse-xor-compaction-ab.json`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/data/21-mask-webgpu-sparse-xor-compaction-ab.json)

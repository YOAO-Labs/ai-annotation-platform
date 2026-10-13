# 0058 — Raster Mask Worker 使用有界 immutable packed base cache

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** core team
- **Supersedes:** —（细化 [ADR-0057](./0057-raster-mask-webgpu-packed-xor-contract.md) 的 packed source prepare，不改变 GPU shader、CPU fallback、history 或保存格式）

> 默认关闭的 rollout 决策已由 [ADR-0060](./0060-default-enable-capability-gated-client-acceleration.md) 取代；immutable cache、预算与生命周期合同继续有效。
> cache 与 GPU buffers 共用单项 compute budget 的准入随后由
> [ADR-0061](./0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit.md) 的 CPU/GPU 双预算取代。

## Context

ADR-0057 让 WebGPU 成功路径直接从 immutable base RLE 与 dirty overrides 构造 packed ROI，并只回读
core XOR words。RTX 3090 production A/B 随后显示，GPU wall p95 已降到约 4–7 ms，而每次重复执行的
RLE scan 与 packed prepare p95 仍为约 17–29 ms，成为第一瓶颈。

主线程的 `SparseMaskTileStore` 已持有 mutable current truth、dirty tile revision、undo / redo 与保存合同。
若 Worker 再维护 mutable current shadow，需要为笔刷、撤销、切题、Worker replacement 和 stale response
增加 revision replay / ack 协议；当前 upload / submit p95 只有约 0.2–0.6 ms，不足以证明这种复杂度合理。

## Decision

### D1. 只缓存 canonical base 的 512² packed tile

- cache key 为 `sessionId + sha256 + tileX + tileY`，value 只从注册 session 的 immutable COCO RLE 生成。
- tile 使用 row-aligned `Uint32Array`，edge tile 按实际宽高计费，每行无效 tail bits 必须为 0。
- cache 不包含 `sourceRevision`、dirty revision 或 mutable current bits；dirty overrides 仍由每次请求显式声明。
- miss 时可从 RLE 重建，cache 丢失不影响正确性。

### D2. ROI 使用 word-span assemble，dirty 使用 masked overwrite

- Worker 将相交 base tile 的任意 bit span 复制到 grow-only、请求前清零的 packed source scratch。
- 非 32 对齐 source / destination offset 必须精确处理跨 word 读取与尾位清零。
- dirty packed override 以 masked overwrite 写入 scratch，既能 set 也能 clear base 前景；不得使用 OR 合并。
- scratch 只属于串行执行的 affinity Worker，不进入 cache，也不在请求间表达真值。

### D3. cache、scratch 与 GPU buffer 共用请求 compute ledger

- 全局 cache cap 为 `min(32 MiB, computeBudget / 4)`，多个 session 共用 owner Worker 内的确定性 LRU。
- provider 在分配前按 prospective GPU capacity、cache reservation 与 scratch 额外容量做精确准入。
- 不能接纳 cache 时清空该加速层并继续使用 ADR-0057 的 direct-RLE packed prepare；这不是 CPU fallback。
- session release 清理所属 entries，最后一个 session release、Worker replacement 或 dispose 后 cache 与 scratch
  必须归零。

### D4. cache 失败不扩大故障域

- cache invariant failure 会丢弃 cache 并对当次请求使用 direct-RLE packed prepare。
- GPU submit、map 或 device lost 后仍按 ADR-0057 惰性 materialize dense alpha，返回既有 exact CPU fallback。
- 主线程只在 session、sha256、revision、rect、tail 与 patch 全部校验后原子应用结果；cache 不改变取消或 stale
  response 语义。

### D5. 默认开启资格不变

Linux RTX 3090 强制 Vulkan 的两轮 warm A/B 通过 prepare 与端到端收益门；同 candidate bundle 通过预算
绕过 cache 的 direct-RLE paired control 也证明 cold wall 回归低于 3%。这些结果只证明该实现值得保留。
`VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` 继续默认 false；macOS、Wayland、Windows 无 flag 矩阵和独立
default-enable 决策仍是产品边界。

## Consequences

正向：

- 2048² warm prepare p95 相对 direct-RLE 改善 85.8%–86.4%，端到端 p95 改善 39.6%–41.4%。
- 4K warm prepare p95 改善 76.3%–78.8%，端到端 p95 改善 29.5%–36.0%。
- 50% overlap pan 只填充新进入视口的 tile；已 warm 的 overlap / disjoint ROI 都保持全命中。
- cache、scratch 与 GPU capacity 分别可观测，dispose 后全部归零。

负向：

- Worker bundle 增加 packed cache 与 bit-span 实现，即使 gate 关闭也会包含这些 CPU-side helper；默认 bundle
  仍不包含 provider、shader 或 adapter 请求。
- cold first-use 需要填充 cache，资格必须使用同 bundle direct-RLE paired control；跨 dev server 的首次编译
  与调度噪声过大，不能直接作为 cache 回归结论。
- cache 命中后 patch build 成为主要 CPU 阶段，下一优化必须重新权衡 atomic compaction、overflow 与 exact
  fallback，不能只比较 shader 时间。

## Alternatives Considered

**full-image packed base plane**：assemble 简单，但 8K / 16K 首次解码与常驻字节不可控，拒绝。

**mutable current packed shadow**：可减少 dirty overlay，但引入 revision replay、undo / redo 和 replacement
恢复协议，当前收益证据不足，拒绝。

**GPU-resident current source**：理论上消除 assemble 与 upload，但 upload / submit 不是当前瓶颈，推迟。

**SharedArrayBuffer / 多 Worker prepare**：需要 COOP / COEP 与新的调度、复制合同，当前单 Worker prepare 已
通过目标门，拒绝。

**立即实现 GPU sparse XOR compaction**：patch build 已成为下一候选瓶颈，但需要独立输出上限、overflow、
ordering 与 fallback 设计，留给后续测量驱动版本。

## Evidence

- 计划：[`docs/plans/archive/2026-07-30-v0.23.18-raster-mask-packed-base-cache.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-30-v0.23.18-raster-mask-packed-base-cache.md)
- 研究：[`docs/research/21-webgpu-video-workbench.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/21-webgpu-video-workbench.md)
- 数据：[`docs/research/data/21-mask-webgpu-packed-base-cache-ab.json`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/data/21-mask-webgpu-packed-base-cache-ab.json)

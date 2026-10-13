# 0057 — Raster Mask WebGPU 使用 packed source 与 core XOR result

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** core team
- **Supersedes:** —（细化 [ADR-0056](./0056-raster-mask-persistent-client-compute-session.md) 的 WebGPU provider 数据合同，不改变 CPU Worker、history 或保存格式）

> 默认关闭的 rollout 决策已由 [ADR-0060](./0060-default-enable-capability-gated-client-acceleration.md) 取代；packed source、core XOR 与惰性 CPU fallback 合同继续有效。
> GPU 失败后惰性 materialize dense alpha 的 fallback 随后由
> [ADR-0061](./0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit.md) 的 packed separable CPU 复用取代。

## Context

ADR-0056 已将大 ROI morphology 接入持久 Worker session，并以 default-off WebGPU provider 加速
`square dilate`。首版 provider 的输入仍是 dense alpha：Worker 从 base RLE 与 dirty overrides 重建
`Uint8Array`，provider 再逐像素 pack；GPU 回读 full after words 后，Worker 再逐像素比较 source / after
并生成 XOR tile patches。

RTX 3090 production A/B 证明 WebGPU provider 相对 CPU 有收益，但成功 GPU 路径仍承担三段与 ROI pixels
线性相关的 JavaScript 工作和一份 input-sized alpha。已有 packed dirty tile、XOR history 与 core-only 写回
合同允许缩短这条路径，而无需改变主线程 store 或 canonical COCO RLE。

## Decision

### D1. GPU-ready 请求直接从 immutable RLE 构造 packed input

- Worker 在静态 route 与 provider 无分配 preflight 通过后，才从 base RLE 构造 row-aligned
  `Uint32Array`。
- dirty packed overrides 对相交区域执行 exact set / clear，优先于 base RLE。
- 成功 GPU 请求不构造 dense alpha，也不保留 production alpha-to-packed helper。
- 不在 Worker 或 GPU 中维护另一份长期可写 current truth；每次请求仍由 immutable RLE + 当前 dirty
  overrides 确定输入，避免 brush、undo、Worker replacement 的增量同步协议。

### D2. shader 只输出 core XOR words

- shader 对含 halo 的 packed input 执行既有 `square dilate`，将 core after 与 core source 直接 XOR。
- output 以 core 原点 row-align；支持非 32 对齐 core offset，最后一 word 的无效位必须为 0。
- Worker 只扫描 non-zero words 与 set bits，按 512 tile 生成现有 `MaskHistoryPatch[]`，并同步计算 changed
  pixels 与 bounds。
- 不增加 GPU atomic summary：XOR words 本来就必须回读用于 history，CPU word / set-bit scan 已足够小。

### D3. CPU fallback 惰性 materialize dense alpha

- gate 关闭、provider 未 ready、adapter 不可用、预算不足和不支持操作在 packed prepare 前直接走 CPU。
- GPU submit、map、device lost 或 runtime error 后，丢弃全部 GPU output，再从同一 RLE + overrides 惰性
  materialize 一次 dense alpha，使用 production CPU morphology。
- 任一失败在 store 成功校验前都不得应用 partial patch 或提升 revision。

### D4. source、XOR target 与 readback 独立计费

- `MAP_READ` readback 继续与 storage output 分离。
- 三类 buffer 使用独立 grow-only capacity；扩容前按 prospective capacity + JS source / result 做准入。
- snapshot 报告每类 capacity 与实际 allocated sum，不再使用单 capacity × 3 近似。
- 当前 affinity Worker 的编辑 job 保持串行，不增加 readback ring；timestamp query 只允许作为可选
  benchmark 诊断，不是产品正确性依赖。

### D5. 默认开启资格不变

packed XOR 路径通过 Linux RTX 3090 强制 Vulkan 的 correctness、两轮 p95、非对齐 core、fallback 与资源
门，只证明实现值得保留。`VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` 继续默认 false；macOS、Wayland、
Windows 无 flag 矩阵与独立默认开启决策仍是产品门。

## Consequences

正向：

- 成功 GPU 请求的 `inputAlphaBytes=0`，删除重复 bit-pack 与 core-wide dense diff。
- 2048² / 4K production operation p95 相对 ADR-0056 首版 provider 再改善约 76.6%–78.8%。
- provider 回读 bytes 由 core 而非 input 决定；halo 不进入 history。
- CPU fallback、主线程 patch 校验、undo / redo 和 canonical 保存合同不变。

负向：

- RLE → row-aligned packed 与非对齐 bit extraction 增加低层位操作测试面。
- GPU runtime failure 会先付出 packed prepare，再付出惰性 dense materialize；该路径必须单独观测。
- packed prepare 与 patch build 现在高于 GPU wall，后续优化必须由新的阶段占比触发。

## Alternatives Considered

**继续 dense alpha provider**：实现稳定，但保留主要 CPU / allocation 开销，拒绝。

**GPU-resident 完整可写 session**：可进一步减少 upload，但需要 brush、lasso、history、task switch 与 Worker
replacement 的 revision / replay 协议，当前没有必要性证据，推迟。

**GPU atomic count / bounds + compact patches**：仍需 history payload，且引入额外 buffer / pass 与跨设备
atomic 差异；当前 word scan p95 足够低，推迟。

**readback ring / 并发 GPU jobs**：当前 editing job 必须串行服从 source revision；没有 overlap 收益证据，
拒绝提前实现。

## Evidence

- 计划：[`docs/plans/archive/2026-07-30-v0.23.17-raster-mask-webgpu-packed-xor-pipeline.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-30-v0.23.17-raster-mask-webgpu-packed-xor-pipeline.md)
- 研究：[`docs/research/21-webgpu-video-workbench.md`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/21-webgpu-video-workbench.md)
- 数据：[`docs/research/data/21-mask-webgpu-packed-xor-ab.json`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/research/data/21-mask-webgpu-packed-xor-ab.json)

# 0064 — 图片工作台采用任务级栅格资源协调器

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** core team
- **Supersedes:** N/A

## Context

超大图背景、已提交 Raster Mask、稀疏编辑 tile、撤销历史、Worker packed base cache、CPU
中间量与 WebGPU buffer 原本各自拥有局部预算。局部预算全部合规时，页面峰值仍可能超过单个浏览器页签能
稳定承受的范围；背景预取也可能在前台 Mask 运算开始后继续抢占资源。

浏览器没有可靠、跨平台的 JS heap 或显存总量接口。协调层因此不能宣称掌握物理内存，只能约束应用明确
拥有的逻辑 allocation，同时必须保证脏编辑、未保存 revision 和撤销历史不会因 pressure 被淘汰。

## Decision

### D1. 每个 Task 建立一个逻辑字节账本

`RasterResourceCoordinator` 随当前 Task 创建和销毁。背景、Mask render/edit/history、Worker cache/scratch、
CPU transient 与 GPU buffer 保留各自的数据结构和 dispose 逻辑，只向协调器提交 owner、category、priority、
logical bytes、reconstructible、pinned 与 generation。

设备档位的 soft/hard budget 固定为 144/192 MiB、288/384 MiB 和 576/768 MiB。未知
`navigator.deviceMemory` 使用标准档；该数值是保守的应用计账门，不是物理内存探测。

### D2. 大分配使用 reserve → commit → release

prospective reservation 与已提交资源共同计入 hard invariant：

```text
committedBytes + reservedBytes <= hardBudgetBytes
```

commit 在同一个 allocation 上原子转账。Worker operation 完成后，transient 与精确 cache/scratch/GPU
capacity 通过批量 replacement 原子交接，既不重复计费，也不留下未计账空窗。generation 不匹配、实际字节
增长无法准入、取消、崩溃和 dispose 都释放 candidate；release 幂等。

### D3. pressure 只请求 owner 释放可重建资源

协调器按 P0 到 P5 表达交互价值，但不持有 ImageBitmap、typed array、RLE 或 GPUBuffer。owner evictor
先停止背景预取，再释放非可见 detail、未选中 Mask render 和 idle Worker cache/scratch/GPU capacity；
foreground Mask operation 期间暂停 P4/P5。dirty tile、current revision 和 history 作为 P0 真值，不可被
pressure 静默删除。新的 operation 无法准入时保持现状并提示用户缩小可见区域或保存后重试。

### D4. 页面生命周期区分 BFCache 与真正卸载

hidden 短时只暂停预取，达到设备档位阈值后释放可重建资源。`pagehide.persisted=true` 会停止新 admission、
释放网络/bitmap/Worker compute，并提升 generation；只保留不可重建且 pinned 的编辑真值与历史。
`pageshow.persisted=true` 按新 generation 先恢复背景覆盖和当前 Mask。真正 pagehide 或组件卸载则清空全部
owner、Worker 和 coordinator。

多页签不共享虚构的“总显存”锁；每个页签独立满足 hard invariant，隐藏页签主动 shed。

### D5. 联合快照进入 BUG 报告

只读快照公开档位、global/owner/category committed/reserved/evictable/pinned、pressure、eviction、denial、
stale、generation 和 lifecycle 计数，并与背景 tile、Mask compute 诊断一同附加到 BUG 报告。快照不包含
签名 URL、对象 key、Mask 内容、task 内容或硬件身份。

## Consequences

正向：

- 背景与 Mask 的共同峰值受到一个可验证 hard invariant 约束，P4/P5 不会挤出已准入的编辑真值。
- Worker crash、BFCache、快速切题和 StrictMode 重放都有确定的资源所有权与清理路径。
- owner 仍可独立优化缓存和算法，不需要把不同数据结构合并成一个全局 Map。
- BUG 报告能够区分网络/解码失败与资源 pressure，并可核对 dispose 是否归零。

负向：

- logical bytes 是应用估算，浏览器内部纹理、解码器和 GC 缓存仍不可观测。
- owner 必须同时维护自身存量和 coordinator reservation，对新 allocation 漏记会破坏诊断可信度。
- pressure 后重新加载可重建资源可能短暂降低背景清晰度或延后非选中 Mask。

## Alternatives Considered

**把所有资源重写进一个全局 LRU**：不同 owner 的真值、取消和释放语义差异太大，也会让协调器持有内容强引用，
拒绝。

**直接相加现有局部预算**：不能限制联合峰值，也无法在前台操作时协调背景预取，拒绝。

**依赖 `performance.memory`、adapter info 或跨页签全局锁**：覆盖面不足且不能作为正确性依据，拒绝。

**pressure 时自动保存或清空 history**：改变用户数据与交互语义，拒绝。

## Notes

- 背景 tile 决策见 [ADR-0063](0063-konva-viewport-image-tiles.md)。
- Raster Mask 大画布真值边界见 [ADR-0054](0054-raster-mask-large-canvas-memory-and-tiles.md)。
- 实施计划见
  [`docs/plans/archive/2026-07-31-v0.23.24-raster-tile-resource-coordination.md`../../plans/archive/2026-07-31-v0.23.24-raster-tile-resource-coordination.md)。

---
title: 图片工作台栅格资源协调
audience: [developer, operator]
type: explanation
status: stable
last_reviewed: 2026-07-31
---

# 图片工作台栅格资源协调

图片工作台中的背景 tile、Raster Mask 渲染、稀疏编辑、撤销历史和 Worker 计算各自保留本地缓存策略，
同时通过当前 Task 的 `RasterResourceCoordinator` 共享一个逻辑字节账本。这个账本约束应用明确拥有的资源，
不代表浏览器 JS heap 或物理显存。

## 预算与优先级

设备档位由 `navigator.deviceMemory` 选择；字段缺失时使用标准档。

| 档位 | soft budget | hard budget | hidden freeze |
| ---- | ----------: | ----------: | ------------: |
| 低   |     144 MiB |     192 MiB |          10 s |
| 标准 |     288 MiB |     384 MiB |          15 s |
| 高   |     576 MiB |     768 MiB |          30 s |

soft budget 用于暂停预取和释放低价值缓存；hard budget 是同步准入不变量：

```text
committedBytes + reservedBytes <= hardBudgetBytes
```

P0 是 dirty edit、current revision 和 history 等不可重建真值；P1/P2 是最低背景覆盖、可见编辑 tile、
目标 LOD 与当前 Mask；P3 是 overview、compare 和空闲 scratch；P4/P5 是 overscan、预取和可重建缓存增长。
前台 Mask operation 开始后会暂停 P4/P5，完成后只按当前 generation 重建队列。

## Owner 接入合同

owner 在大分配前调用 `tryReserve()` 或 `reserve()`，分配成功后用同一 handle `commit(actualBytes)`，所有
取消、失败、淘汰和卸载路径都必须 `release()`。实际字节大于预留值时会重新做总账准入；无法接纳时 candidate
被释放，不能留下 outstanding reservation。

资源描述只包含：

- owner 与 category；
- priority 与 logical bytes；
- 是否可重建、是否 pinned；
- 当前 task/page generation。

协调器不持有 ImageBitmap、RLE、typed array 或 GPUBuffer。每个 owner 通过 evictor 自己关闭 bitmap、撤销
ObjectURL、abort 网络或清 Worker capacity，再由 reservation release 完成对账。

Worker operation 的 `cpu-transient` / `gpu-buffer` 与完成后留下的 base cache、scratch、GPU capacity 使用
`replaceCommittedResources()` 原子交接。任何 owner 都不得先释放 transient、再在下一次事件循环补记 retained，
也不得让同一 buffer 同时出现在两个 charge 中。

## Category 与所有权

| Category              | 典型 owner                      | 淘汰语义                         |
| --------------------- | ------------------------------- | -------------------------------- |
| `background-coverage` | `background`                    | BFCache/卸载可重建               |
| `background-detail`   | `background`                    | 非可见优先，必要时降低 LOD       |
| `background-prefetch` | `background`                    | 最先暂停、abort、淘汰            |
| `mask-render`         | annotation / interactive render | 未选中先释放；BFCache 全部可重建 |
| `mask-edit`           | dense / sparse editor           | dirty/history referenced 为 P0   |
| `mask-history`        | history store                   | 不可静默淘汰；新命令可被拒绝     |
| `mask-compare`        | operation preview               | 取消或确认后释放                 |
| `worker-base-cache`   | `worker-compute`                | Worker 空闲时可清                |
| `worker-scratch`      | `worker-compute`                | Worker 空闲时可清                |
| `cpu-transient`       | `worker-compute`                | job 完成、取消或崩溃立即释放     |
| `gpu-buffer`          | `worker-compute`                | device reset/lost/dispose 时释放 |

局部缓存预算仍然生效，它限制单个 owner 的工作集；共享 hard budget 再限制所有 owner 的联合峰值。两者中任一
拒绝都必须保持 annotation、dirty revision 与 history 不变。

## 页面生命周期

- hidden 短时只暂停预取；达到档位阈值后释放非可见 detail、未选中 render 和 idle compute。
- BFCache pagehide 停止新 admission，abort 网络与未提交 operation，释放所有可重建资源并提升 generation；
  只保留不可重建且 pinned 的 edit/history。
- BFCache pageshow 先恢复 overview/可见 tile，再加载 detail；旧 generation 的 promise 不能提交。
- 真正 pagehide、Task 切换或 React 卸载会 dispose coordinator、Worker、session 与全部 owner。
- 每个页签独立满足 hard budget；隐藏页签主动 shed，不建立跨页签伪精确显存锁。

## 诊断与不变量

当前路由的 `window.__rasterResourceDiagnostics` 提供只读快照，BUG 报告会自动附加同一份裁剪数据。重点核对：

```text
sum(owner.committedBytes) == committedBytes
sum(owner.reservedBytes) == reservedBytes
chargedBytes == committedBytes + reservedBytes
chargedBytes <= hardBudgetBytes
dispose 后 chargedBytes == 0
```

`deniedAdmissions` 表示准入失败，`staleCommits` 表示旧 generation 尝试提交，`evictionRounds` 和
`evictedBytes` 表示协调 pressure 的轮次与实际释放量。背景细节不清晰时同时查看 tile diagnostics 的
`targetCoverageRatio`：资源 pressure 会保留 coverage 并渐进恢复，网络/签名/解码错误则会增加 tile error。

测试可通过 hook 的显式 budget 覆盖档位；生产没有运行时“无限放大预算”开关。调整默认值必须同时重跑
randomized ledger、背景/Mask 联合 E2E、hidden/BFCache、Worker crash、两页签和 dispose 归零门。

相关决策见
[ADR-0064](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/adr/archive/0064-task-scoped-raster-resource-coordination.md)。

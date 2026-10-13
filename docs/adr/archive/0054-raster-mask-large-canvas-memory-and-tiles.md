# 0054 — Raster Mask 大画布采用稀疏 Tile 与固定资源预算

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** core team
- **Supersedes:** —（扩展 [ADR-0052](./0052-shared-raster-mask-and-image-geometry.md) 的共享 RLE 合同，不改变 canonical 持久格式）

## Context

图片与视频工作台已经共享内容寻址 COCO RLE，显示侧也会保留 crop alpha / bitmap；但编辑器仍为每个会话分配 `width × height` alpha 和整图 canvas，每次分析 / 操作创建新 Worker，stroke history 保存 before / after RLE。直接把 4096 常量改成 8192，会让单幅 8K Mask 的 dense alpha、RGBA 与编辑临时量保守达到约 576 MiB，并让全图扫描形成秒级或 OOM 风险。

两轮 2 次预热 + 20 次记录的基线显示：4K 显示 pipeline p50 / p95 为 235.2–238.8 / 254.5–267.2 ms；4K brush 加现有 history 编码 p50 / p95 为 161.1–161.6 / 164.9–170.7 ms；每轮创建 / 销毁 180 个 Worker，GC 后 heap 增量约 22.0 MiB。原始聚合数据见 `docs/research/data/19-mask-large-canvas-performance-baseline.json`。

候选方案：

| 选项                                             | 主要卖点                                                                   | 主要劣势                                                    |
| ------------------------------------------------ | -------------------------------------------------------------------------- | ----------------------------------------------------------- |
| **A. canonical RLE + 浏览器稀疏 tile（本 ADR）** | 不迁移持久格式；局部编辑成本与可见 / 修改 tile 相关；可继续精确 round-trip | 需要 run index、tile merge、分块 overlay 和更明确的降级合同 |
| B. 放宽尺寸后继续整图 alpha / canvas             | 改动最少                                                                   | 8K 约 576 MiB，无法满足低内存设备与交互门                   |
| C. tile 成为服务端持久格式                       | 局部读写自然                                                               | 破坏 AAP / COCO 互操作并引入迁移、对象一致性和 GC 复杂度    |
| D. WebGPU / WASM 重写                            | 有更高计算吞吐潜力                                                         | 不能消除整图真值内存；兼容、部署和测试成本超过当前证据      |

## Decision

### D1. 持久格式与媒体边界分离

- canonical 持久格式继续是 COCO RLE，runs 上限 1,000,000、JSON 上限 4 MiB。
- 图片 `raster_mask` 最大单边为 8192、最大像素为 67,108,864；视频 `video_track_mask` 与交互式 AI inline Mask 继续最大单边 4096、最大像素 16,777,216。
- codec 结构验证允许图片上限，task-aware validator 再按 geometry / 媒体收窄；AI proxy 使用独立常量。
- 8K noise、棋盘格等超过 runs / bytes 的内容稳定拒绝，不因尺寸允许而绕过熵预算。

### D2. 设备档位是硬预算

| 档位     | 判定                          | 只读缓存 | 下载 / decode 并发 | Worker pool | tile 缓存 | history |
| -------- | ----------------------------- | -------: | -----------------: | ----------: | --------: | ------: |
| Low      | `deviceMemory <= 2`           |   64 MiB |                  1 |           1 |    32 MiB |  16 MiB |
| Standard | 未知或 `2 < deviceMemory < 8` |  128 MiB |                  2 |           2 |    64 MiB |  32 MiB |
| High     | `deviceMemory >= 8`           |  192 MiB |                  4 |           2 |   128 MiB |  64 MiB |

admission 在插入前完成；只有当前编辑和 selected 对象可 pin。无法进入预算的对象保持轻量 bounds 与 `deferred` / `budget_exceeded`，不得先超额再等待 LRU。bitmap、Worker、queue、tile、history 和 retained bytes 必须有显式 created / closed / live 计数。

### D3. 统一固定 Worker pool 与 transferable 协议

- analyze、operation、instance operation、tile decode 与 tile merge 共用 `RasterMaskWorkerPool`。
- pool queue 最大 32；analyze / 普通操作 / 8K merge timeout 分别为 15 / 30 / 60 秒。
- COCO counts 在边界转换为 `Uint32Array` 并 transfer；输出 crop alpha / result buffer 继续 transfer。
- abort、timeout 或 crash 只替换执行该 job 的 slot；scope / route 卸载时 dispose 全 pool，过期 generation 结果禁止回写。

### D4. 大画布使用 512 稀疏 tile

- 图片任一边超过 4096 或总像素超过 16,777,216 时进入 tiled editor；tile 固定 512，morphology halo 最大 32。
- 真值由 immutable base RLE 与 materialized tile overrides 组成。base 通过 column-major run index 按 tile 懒解码，禁止先 materialize 全图。
- viewport 只 pin 可见 tile 与一圈预取；overlay 使用 tile canvas / bitmap。overview 只用于显示，精确 picking 读取 RLE / tile 真值。
- 保存时 Worker 按 COCO column-major 合并 base interval 与 overrides，再复核 pixels、runs 和 JSON bytes；tile 不成为 API 或对象存储格式。

### D5. History 使用 XOR tile patch

- 每条 command 保存触及 tile 的 1 bit / pixel XOR，undo 与 redo 对同一 patch 执行 XOR。
- history 最多 100 条，同时受设备档位字节预算；新命令清空 redo，超预算从最旧 undo 淘汰。
- stroke 只在首次触及 tile 时捕获局部基线；no-op 不入栈。保存、取消与 Worker 失败不能提前释放仍被 history 引用的 tile。

### D6. 全图操作必须显式降级

- 大画布保留 brush、erase、lasso add / subtract、save、undo / redo。
- morphology 只允许用户显式选择 viewport ROI，读取 `ROI + halo` 后只写回 core。
- component、hole、flood fill、split / join / overlap 与全图 morphology 若需扫描超过 16,777,216 像素，返回 `large_mask_full_scan_required`，不静默回主线程或伪装局部等价。
- `RASTER_MASK_CREATE_ENABLED` 是部署级写入 kill switch，默认开启；项目级 `raster_mask_native_editing_enabled` 默认开启并允许项目 opt-out。大画布能力只有在两层闸门都允许时可写。

## Consequences

正向：

- 8K 局部编辑的常驻成本与 materialized tile 数相关，不再固定分配 64 MiB alpha 和 256 MiB RGBA。
- 图片可以扩展到 8K，同时视频 / AI 4096、runs / bytes 与 canonical RLE 互操作边界不漂移。
- 固定 pool、硬 admission 与计数器让资源是否回稳可以直接测试，不再依赖 `usedJSHeapSize` 猜测 bitmap / Worker 生命周期。
- XOR patch 去除每个 stroke 的全图 RLE 扫描，undo / redo 仍保持逐像素可逆。

负向：

- tile merge、run index、边缘 tile 与跨 tile brush / polygon 会增加几何 golden fixture 和取消测试规模。
- 大画布不提供所有高级全图操作；用户必须选择 ROI 或回到更小媒体。
- canonical RLE 保存仍可能在高熵内容上触发 runs / bytes 拒绝，尺寸可达不代表任意内容可持久化。
- Low 档位可能只能显示受限 preview 或进入只读，功能覆盖随设备内存明确降级。

## Alternatives Considered（详）

**方案 B（dense 8K）**：即使只保留 alpha + RGBA 已需 320 MiB，叠加 MaskBuffer clone、analysis 与 canvas 后约 576 MiB；不能靠更积极 GC 解决峰值。拒绝。

**方案 C（持久 tile）**：会把浏览器性能实现泄漏到 API / AAP / COCO，对既有内容寻址对象引入 manifest、部分失败与 GC 新状态机。当前 canonical merge 能在不迁移格式的前提下满足需求。拒绝。

**方案 D（WebGPU / WASM）**：它们可能降低 morphology 计算时间，但不自动解决 history、bitmap、RLE clone 与 8K 真值内存；也会增加浏览器兼容矩阵。只有 CPU / Worker tile 路径仍不达门时才重新评估。

**后台 operation job**：能处理全图扫描，但会新增异步草稿、锁、版本与取消状态机。本阶段产品要求是本地交互，超预算操作采用显式 ROI / 禁用。延后。

## Notes

- 基准 runner：`apps/web/scripts/benchmark-mask-large-canvas.mjs`。
- 基线报告：`docs/research/19-mask-large-canvas-performance-baseline.md`。
- 实施后两轮复测通过 4K pointer / pointer-up、8K sparse 交互 / merge、16 MiB GC heap 增量和全部显式资源归零退出门；聚合数据为 `docs/research/data/19-mask-large-canvas-performance-final.json`。
- 当前显示缓存：`apps/web/src/pages/Workbench/stage/shared/useRasterMaskRecords.ts`。
- 当前 dense editor：`apps/web/src/pages/Workbench/stage/shared/geometry/maskBuffer.ts`、`apps/web/src/pages/Workbench/stage/MaskOverlayLayer.tsx`。
- 实施计划：`docs/plans/archive/2026-07-21-v0.23.10-mask-performance-large-canvas.md`。

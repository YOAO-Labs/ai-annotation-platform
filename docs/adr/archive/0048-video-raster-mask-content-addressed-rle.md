# 0048 — 视频栅格 mask 使用内容寻址 RLE 对象，不内联进 annotation JSONB

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** core team
- **Supersedes:** —
- **Extended by:** [ADR-0052](0052-shared-raster-mask-and-image-geometry.md)

## Context

视频 mask track 需要逐关键帧保存像素级二值 mask。候选编码是 COCO uncompressed RLE；需要决定 RLE `counts` 是直接内联到
`annotations.geometry` / `video_tracker_jobs.staged_result`，还是把每帧 RLE 放对象存储、JSONB 只留引用。

Phase A 基准使用仓库真实 SAM fixtures、720p / 1080p / 4K、可复现的 synthetic adversarial masks，并实际序列化
30 frames × 10 targets。结果见 `docs/research/16-raster-mask-rle-benchmark.md`：真实 SAM 1080p p95 单帧 RLE JSON 为
3,727 bytes，但 3000 帧单轨迹投影为 11,431,900 bytes，超过冻结的 8 MiB annotation geometry 预算。即使 HTTP gzip 很小，
PostgreSQL JSONB 全量重写、Pydantic materialization、worker staged candidate 双份占用仍按未压缩结构付费。

| 选项 | 主要卖点 | 主要劣势 |
|---|---|---|
| 内联 COCO RLE | 单文档自包含、实现最短 | 长轨迹超过 JSONB 重写预算；candidate / API / 浏览器重复 materialize |
| **内容寻址 `coco_rle_ref`** | annotation/staged JSON 体积稳定；mask 可按当前帧懒取与复用 | 增加对象生命周期、鉴权读取与 GC |
| 每 track 单一外部 blob | 对象数少 | 任一关键帧修改仍重写整轨 blob，无法按帧缓存 / 去重 |

ADR-0013 和 ADR-0022 冻结的是图片工作台与既有 SAM 路径的 `mask -> polygon` 行为；本决策最初只扩展视频持久化 mask，不改变图片侧
现状，也不把所有几何统一成通用 blob。ADR-0052 后续在保持本 ADR 的 canonical bytes、内容寻址和生命周期合同不变的前提下，将同一内容层共享给图片 `raster_mask`。

## Decision

采用**逐关键帧、不可变、内容寻址的 COCO uncompressed RLE 对象**。`video_track_mask.keyframes[].mask` 只保存：

```json
{
  "encoding": "coco_rle_ref",
  "size": [1080, 1920],
  "object_key": "raster-masks/sha256/ab/cd/<sha256>.json",
  "sha256": "<64 lowercase hex>",
  "runs": 4096,
  "bytes": 16384
}
```

### Canonical bytes and limits

- object 内容是 `{encoding:"coco_rle",size:[h,w],counts:[...]}` 的 compact UTF-8 JSON；键顺序固定为 encoding / size /
  counts，分隔符固定为逗号 / 冒号，不写空白；SHA-256 对这组 canonical bytes 计算；
- width / height 均不超过 4096，总 pixels 不超过 16,777,216；counts 为 strict non-negative integer，run 数不超过
  1,000,000，总和必须等于 pixels；单对象 canonical JSON 不超过 4 MiB；
- annotation geometry compact JSON 不超过 8 MiB；tracker staged JSON 不超过 64 MiB。超限在 append / WebSocket publish /
  DB write 前拒绝，不能先积累全量再事后检查；
- 空 mask 合法且可往返，但视为不可见；outside 仍是独立显式状态。

### Write and read lifecycle

1. API / tracker 对 inline RLE 做严格校验并计算 canonical bytes / SHA-256；
2. 以 `put-if-absent` 上传 immutable object；相同内容复用同一个 key；
3. 上传成功后才在数据库事务中写 `coco_rle_ref`；数据库失败不回滚对象上传；
4. 客户端通过 task / annotation / tracker-job 作用域的受鉴权 content endpoint 读取 RLE。geometry 不保存永久 signed URL；
5. 删除 / 替换 keyframe 不同步删除对象，避免共享引用和事务回滚竞态。

对象 GC 每日扫描 `raster-masks/sha256/`：对象创建满 24 小时且在 active annotations、predictions、可审阅 tracker staged_result
中均无引用时才删除。扫描失败只保留垃圾，不得误删在线引用；实现必须有 dry-run 与删除计数日志。

### Format boundaries

- AAP JSON / Video JSON 保留 `coco_rle_ref` 元数据；AAP export 为可移植包时把 referenced RLE 作为包内资源并在 import 时重建
  content-addressed object，不能导出仅在源 bucket 有意义的悬空 key；
- DAVIS / COCO export 在 worker 内按需读取并解码；MOT / KITTI / YOLO det 用 RLE AABB；YOLO seg 跳过；
- 前端只解码当前帧可见 mask，并按 committed / staged 两类 source-aware cache key 管理 bitmap 生命周期。

## Consequences

正向：

- 3000 帧轨迹只在线性增加小引用，不把 RLE counts 反复写进 JSONB；
- 相同 mask 跨 candidate / annotation / export 自动去重；
- 当前帧懒取与 bitmap LRU 有稳定 cache identity（SHA-256）；
- tracker 可以在累计引用 payload 很小时仍用单 mask bytes / runs 做提前拒绝。

负向：

- annotation 创建 / tracker accept 从单次 DB 写变成“对象先写 + DB 后写”的 saga；
- AAP 可移植导入导出必须携带资源，不能只复制 geometry JSON；
- 需要受鉴权读取 endpoint、MinIO / OSS 行为一致性测试和保守 GC；
- 离线数据库 dump 不再自包含 mask bytes，备份策略必须同时覆盖对象存储。

## Alternatives Considered

**内联 RLE**：1080p 短轨迹体积很好，gzip 也显著，但 3000 帧单轨已超过 8 MiB gate。问题发生在 JSONB / Python / worker
内存，不可用 HTTP gzip 掩盖。

**每 track 单 blob**：减少对象数，但编辑一个关键帧仍重写整条轨迹；staged preview 也无法按帧懒取，故不选。

**palette PNG per frame**：DAVIS 导出适合 PNG，但在线合同还需要 runs、尺寸、AABB 与标准 COCO RLE；PNG 作为权威对象会让每次
COCO export / tracker continuation 多一层解码与再编码，故不选。

## Notes

- Phase A plan：`docs/plans/archive/2026-07-12-v0.22.0-raster-mask-track-davis.md`
- Benchmark：`scripts/benchmark_raster_mask_rle.py`、`docs/research/16-raster-mask-rle-benchmark.md`
- Codec：`apps/_shared/mask_utils/src/mask_utils/rle.py`、`apps/api/app/utils/raster_mask_rle.py`、
  `apps/web/src/pages/Workbench/stage/shared/geometry/maskRle.ts`
- 相关决策：ADR-0013、ADR-0022、ADR-0024
- Alembic：annotation geometry 与 tracker staged_result 已是 JSONB，本决策不新增关系列；对象 GC 状态不落引用计数表。

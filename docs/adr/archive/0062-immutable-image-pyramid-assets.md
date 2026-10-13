# 0062 — 超大图采用不可变代次金字塔与批量鉴权交付

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** core team
- **Supersedes:** N/A

## Context

普通图片缩略图通过对象存储整对象读取与 Pillow 解码生成。这个路径用于 50MP–300MP 图片时，会让压缩源、
完整解码像素和中间结果同时占用 Worker 内存，也无法为客户端按视口加载提供稳定合同。

超大图还带来三个独立问题：

- DatasetItem 可被多个 Task 复用，派生资产不能按 Task 重复生成；
- 源对象可能在路径不变时被替换，旧像素不能继续和新标注坐标叠加；
- 私有对象存储不能把数千个长期 URL 写进 Task 或 manifest，也不能允许客户端提交任意对象 key 请求签名。

## Decision

### D1. 资产身份与不可变 generation 分层

- `image_pyramid_assets` 以 DatasetItem 或 legacy direct Task 为 owner，二者恰有一个非空，并与
  `profile_version` 组成唯一身份。
- `image_pyramid_generations` 保存一次生成尝试；同一 asset 同时最多存在一个
  `pending`/`building` generation。
- 对象写入 `image-pyramids/{asset_id}/g{generation}/`。tile、overview 和 manifest 在该前缀内不可变。
- Worker 完成网格、对象大小和摘要校验后，才在同一数据库事务中把 generation 标记为 `ready` 并切换
  `active_generation`。
- ETag、对象大小和可用的版本 ID 组成快速 `source_identity`；流式 SHA-256 组成内容
  `source_fingerprint`。API 和发布点均用 source identity 阻止旧源 generation 继续生效。

### D2. 统一规范化与服务端生成

- 生产生成器使用 pyvips/libvips 的 lazy/sequential pipeline，不以 Pillow 作为大图 fallback。
- 像素按 EXIF autorotate 后统一到 sRGB；灰度转 sRGB，alpha 保留，多页输入拒绝。
- manifest 使用平台自有 `aap-image-pyramid/v1`，采用 full-resolution-first 的二次幂层级、512 core
  tile、1px overlap 和 WebP 输出。
- manifest 只描述逻辑网格、规范化版本和摘要，不包含 bucket、对象 key 或签名 URL。

### D3. 独立资源域与原子失败语义

- 金字塔任务进入独立 `image-pyramid` 队列，单 Worker 并发为 1；下载、派生、临时空间、像素、维度、
  tile 数和 wall time 都有硬上限。
- 源文件流式落到私有临时目录；任何失败都会删除未发布前缀、释放 lease，并写入低基数稳定错误码。
- 每日 reconciliation 处理过期 lease、旧 generation 和无数据库 owner 的孤儿对象。
- 金字塔对象与 source 同寿命，不进入普通媒体缓存的定时过期 lifecycle。

### D4. Manifest 与批量签名分离

- Task 列表和详情只返回轻量状态摘要。
- manifest API 复用 Task 可见性和 annotation read scope，支持 private cache 与 ETag。
- overview/tile URL 通过最多 128 项的批量端点签发；服务端只接受 generation 和逻辑坐标，再自行派生
  对象 key。
- 服务端在签名前验证 source fence、generation、网格坐标和对象存在性。缺失对象会使 ready generation
  进入不一致状态，不签发一个注定失败的 URL。

## Consequences

正向：

- 大图源不再被 Python 聚合为单个 bytes，生成内存与临时盘可以独立限额和观测。
- DatasetItem 的多个 Task 共享一份派生资产；source replacement 不会复用旧像素。
- 客户端可按视口批量刷新短期 URL，Task payload 不随 tile 数增长。
- 半成品不会被发布成 ready，Worker crash 和重复消息可由 lease、单飞约束与 GC 收敛。

负向：

- API 镜像引入 libvips 与 ICC profile 包，镜像体积增加。
- 服务端需要维护额外数据库状态、对象对账和专用 Worker。
- WebP 有损输出尚不能替代对像素级无损有硬要求的项目；这类策略需要单独 profile 和视觉资格门。
- arm64 容器、真实高熵缺陷图 golden 和对象存储大规模并发仍需部署环境持续验收。

## Alternatives Considered

**Pillow 整图解码后切 tile**：实现简单，但内存随 decoded pixels 增长且现有整对象读取会叠加一份压缩源，
拒绝。

**直接采用 Deep Zoom manifest 并暴露对象前缀**：减少一层协议，但会把存储布局变成客户端合同并扩大
任意 key 签发风险，拒绝。

**Task 级完整 URL 列表**：首次加载简单，但 payload 为 O(tile count)，URL 过期后也难以一致刷新，拒绝。

**让 pyramid 进入普通 30 天缓存 lifecycle**：可自动回收空间，但会制造 ready 行指向缺失对象，且缺少
同步惰性重建合同，拒绝。

## Evidence

- 计划：[`docs/plans/archive/2026-07-31-v0.23.22-large-image-pyramid-delivery-foundation.md`../../plans/archive/2026-07-31-v0.23.22-large-image-pyramid-delivery-foundation.md)
- 实现：`apps/api/app/workers/image_pyramid.py`
- API：`apps/api/app/api/v1/tasks/image_pyramid.py`
- 数据模型：`apps/api/app/db/models/image_pyramid.py`

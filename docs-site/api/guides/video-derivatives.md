---
title: 视频派生媒体
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# 视频派生媒体

Task 的 `/tasks/{task_id}/video/...` 和可见 Task 支撑的 `/videos/{dataset_item_id}/...` 共用派生合同。当前支持直接关联 DatasetItem 的托管视频；目录、原地引用和独立 TaskOriginal 派生不在此合同内。整段原件播放继续使用[认证准备与范围读取](./verified-content#视频原件准备与范围读取)。

读取沿用现有 Task 可见性，不新增 `annotations:read` scope；Task 的帧重试仍要求原有写权限，facade 保持自己的原有检查。后台工作冻结具体 Task、原请求凭据和准确来源，执行中重新核对当前权限。Dataset 回填与 Storage 维护保留各自管理权限及生命周期检查。

## 元数据与时序

存储管理的 `POST /api/v1/storage/video-assets/retry` 返回 `202`，包含 `job_id`、`asset_type`、`dataset_item_id` 和 `status`。正常受理为 `queued`；队列投递无法确认时为 `dispatch_pending`，仍返回可查询的任务，不能据此断定执行失败。可由原重试入口重新派发尚未认领的输出；本人对同一输出和资产类型的活动请求复用任务，其他发起者获得各自可见的任务。详情使用 `GET /api/v1/async-jobs/{job_id}`，仅本人或超级管理员可见；该任务不提供通用取消或失败预测重试动作。

任务状态跟随既有输出的认领、发布、失败和过期收尾，`kind=video_asset_retry`。只返回安全 ID、目标类型及成功／失败计数，不公开原件定位或凭据。metadata ready 不代表所有派生资产成功，海报和时间表分别按本次请求结果判定；普通 Worker 返回也不等于资产成功。执行端状态未知且仍有未收尾访问时，任务保留等待状态，沿用现有输出维护机制，不承诺立即判定崩溃失败。chunk／frame 缺可信元数据时，仍先准备元数据并返回原有 `503 video_metadata_preparing`，此次不登记目标资产重试任务。

manifest 返回 `metadata_generation_id`，整段 `video_url` 和 `fallback_video_url` 为空。旧未登记派生结果触发新 metadata output，提交后排队，返回 `503 {"detail":{"code":"video_metadata_preparing"}}`；客户端有限次轮询。未知原件、撤权、维护门禁和关闭不确定不降级为旧路径。

元数据、分块和 samples 读取遇到明确的短锁错误 `409 content_busy`、`storage_original_busy` 或 `storage_maintenance_busy` 时，浏览器最多重试三次，间隔为 200、400、600 毫秒。其他 409、权限或来源错误不自动重试；元数据准备中的 503 保留独立的有限等待策略。

`GET .../frame-timetable?expected_metadata_generation_id=<uuid>` 返回该冻结 generation 的完整时间表及 ID；当前 generation 不符返回 409。未提供参数仍读取当前代。元数据、活动指针与数据库时序索引同次提交，封面生成期间不提前发布。

## 帧、块与样本

ready frame/chunk DTO 包含 `output_id`、小写 `sha256`、`byte_size` 和 `metadata_generation_id`。pending 仍有 output 与 generation 身份，通常返回 202 和重试提示。服务端完整核验保存位置的结果及相应对象后才签发地址。

`GET .../chunks/{chunk_id}/samples?expected_output_id=<uuid>` 要求该块仍为指定输出，否则 409；响应携带相同 output、SHA 和 generation。样本仅来自不可变结果，不能被逻辑行中的 diagnostics 覆盖。字节偏移必须落在该块内，帧集合必须完整覆盖冻结范围。没有可用 samples 时返回 404，客户端可以按现有能力回退。

浏览器应同时比较清单、样本与下载字节的 output／generation／SHA，完整核验块哈希后解码。服务端缓存、浏览器 decoder 和帧缓存不能仅以 Item 与帧号识别内容。签名刷新仍须保持输出身份，变化时重新获取清单。

## 生成、预算与回收

每个尝试保存独立 UUID 前缀、原件／授权收据和 media-cache 位置。输出使用独占 PUT，完整回读 SHA／大小，再以短事务复核当前来源、授权、generation、槽位、固定租约及源／目标 Gate 后发布。元数据转码副本不作为抽帧输入，也不自动替代原生播放器来源。

原件上限 512MiB，同时两份暂存；复制合作式期限 300 秒。单派生对象最多 2GiB，结果 JSON 最多 64MiB，时间表／packet 最多一百万条。工作租约固定 1800 秒，不续约。软时限后等待真实原生进程、线程、管道、文件和客户端关闭，不使用硬杀把尚未收尾的工作当作已结束；实际排空可能更长，超过租约不能发布。

业务删除和缓存 TTL 先解除逻辑引用，独立输出保留待回收。已发布输出从退役起重新等待至少一小时，保护已签地址；仅在没有在用操作时，按保存位置删除准确前缀。新对象位于 `video-derivatives/objects/{output_id}/`，不受旧 `videos/` 自动过期规则支配。旧无 owner 的对象不猜测、签名、采纳或盲删。

未知原生进程／关闭保留 StorageOperation 和 `anno-video-derived-{output_id}-...` 临时目录。应按输出 UUID 与操作账核对实际进程收尾，不能用目录年龄或直接清账代替证明。运维步骤见[视频帧服务 Runbook](../../ops/runbooks/video-frame-service)。完整用途切换仍关闭。

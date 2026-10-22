---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-22
---

# 内容核验与严格图片读取

这些接口提供完整图片的内容核验和请求内快照读取。服务器先读取全部字节、计算 SHA-256 并解码验证图片，再从同一临时快照发送响应。原路径或对象在此后被覆盖，不会改变正在发送的快照。全部路径以下列 `/api/v1` 前缀调用。

## 目录观测条目

| 方法 | 路径                                                               | 授权与行为                                                          |
| ---- | ------------------------------------------------------------------ | ------------------------------------------------------------------- |
| POST | `/dataset-scans/{run_id}/entries/{path_digest}/verify`             | 当前超管和 `datasets:write`；为完整扫描中的匹配文件核验当前图片内容 |
| GET  | `/dataset-scans/{run_id}/entries/{path_digest}/content?sha256=...` | 当前超管和 `datasets:read`；按已登记摘要严格读取                    |

从[观测条目列表](./dataset-sources)取得 `path_digest`。verify 返回 `sha256 / size_bytes / media_type / width / height`（尺寸按浏览器 EXIF 旋转后的逻辑方向）；条目列表增加 `verified_content` 与 `content_verified`。核验结果与原始 stat 观测分开保存，不包含绝对路径、节点指纹或文件内容。

要求 Run 已完整完成、数据集和来源仍存在且启用、来源与位置修订未变。必须先在**实际处理请求的 API 节点**完成当前 read 探测：调用位置 probe，`node_id=api` 并提供相对位置的 `sample_file`。worker 的 list 探测不能替代它。API 未挂载目录、探测过期或根身份变化时拒绝读取；不会自动把文件经 Celery/Redis 转发。多 API 实例应分别探测并暴露同一授权目录。首次核验要求扫描与 API 位于同一内核主机，并核对扫描固定的来源子目录和文件身份；跨主机、未知或重启前的主机证据返回 initial_verification_node_mismatch，需在可比较节点重新扫描。已建立的 SHA-256 可供其他已验证 API 节点读取同内容。

首次核验建立的是本次读取的内容身份，不能证明扫描时或旧标注曾读取的字节。设备号和 inode 属于各节点本地，不跨节点当内容证明。重复核验和后续读取必须符合已经保存的摘要，不能覆盖旧身份。文件与原始大小/时间观测不符时，需重新扫描。

该流程不创建 DatasetItem、Asset 或任务，不开放原地样本标注。已核验内容也不意味着原图以后一直在线；预览状态不由此次核验推导。

## 托管图片任务

| 方法 | 路径                                        | 行为                                                                                          |
| ---- | ------------------------------------------- | --------------------------------------------------------------------------------------------- |
| GET  | `/files/tasks/{task_id}/content-identity`   | 返回当前 verified Asset 的摘要、大小和 Asset ID；原图可用性仍为 `unchecked`，预览为 `unknown` |
| GET  | `/files/tasks/{task_id}/content?sha256=...` | 按固定托管位置和该摘要读取完整图片快照                                                        |

两个入口均要求 `tasks:read`，并复用当前项目权限、批次状态和任务分派规则。知道 Task/Asset ID 不代表有权限；任务之外的数据集或目录不会因此开放。读取后、响应前再次检查权限和绑定；API Key 的吊销、轮换、过期或 scope 收回同样使在途读取失败。未核验历史 Asset 返回 `content_unverified`，不把旧 hash hint 或 ETag 当 SHA-256。

当前仅支持固定平台 datasets 桶的 `managed_original / present` 副本，拒绝禁用或配置偏离的存储位置。不会用缩略图、其他桶或当前全局配置猜测原图。可覆盖 S3 key 也必须完整核对摘要，不能只因键名含 UUID 就直接相信其内容。

既有 `/files/tasks/{task_id}/file-url` 保留签名响应格式，并执行相同任务可见性检查；该旧地址仍有签名有效期窗口，不具备新快照接口的严格保证。工作台、ML、导出和训练当前仍沿用已有链路；本接口没有自动切换这些消费者，也未开放外部路径。

## 响应、预算与清理

成功返回完整 `image/png`、`image/jpeg` 或 `image/webp`，含精确 Content-Length、`X-Content-SHA256`、基于摘要的 ETag、`Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。仅支持单帧图片。请求必须带64位小写 SHA-256；不提供任意 locator 输入，Range 返回416。

固定上限：每张64MiB、3200万像素、读取循环10秒、请求等待15秒、响应30秒。每个 API 进程最多4个快照，名额持续到响应结束，临时文件字节上限合计256MiB；图片解码另有内存占用，多进程上限需相应累加。满额返回429。没有持久原图缓存、预览派生或缓存淘汰。

超时或取消会设置停止标记；阻塞的内核 I/O 不能由线程强制终止，后台读取仍占名额，直到实际退出并清理。成功、失败、取消、发送异常和客户端断开都会关闭请求拥有的临时文件；清理不触碰源目录或托管原件。

| 状态      | 典型 code                                                                                                                                               |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 409       | `scan_incomplete`、`content_unverified`、`api_read_not_verified`、`source_changed`、`content_mismatch`、`configuration_changed`、`no_readable_original` |
| 413       | `content_too_large`、`image_pixel_budget_exceeded`                                                                                                      |
| 415       | `image_format_unsupported`、`invalid_image`                                                                                                             |
| 429       | `snapshot_capacity_exceeded`                                                                                                                            |
| 503 / 504 | `content_source_unavailable` / `content_read_timeout`                                                                                                   |

认证使用 Bearer。浏览器应通过带认证头的请求取得 Blob，再管理 object URL 的取消与释放；不能直接把这些 API 路径赋给普通图片 src。现有数据集/工作台 UI 尚未接入这条新读取链路。

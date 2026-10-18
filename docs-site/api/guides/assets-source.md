---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 独立素材来源控制

`apps/assets-source` 持有单文件上传初始化、发布完成及 Dataset/Item 删除的实际 SQL/S3 执行。公开 API 继续认证 JWT/API key、检查 `datasets:write` 并保持原响应；显式远程配置失败会报错，不回退到本地。未配置远程地址时使用同一公开工作流与原生能力。

## 接口与身份

| 公开 REST                                                     | 内部 POST                    | 结果                                                     |
| ------------------------------------------------------------- | ---------------------------- | -------------------------------------------------------- |
| `POST /datasets/{dataset_id}/items/upload-init`               | `/v1/source/upload-init`     | `item_id,upload_url,expires_in`                          |
| `POST /datasets/{dataset_id}/items/upload-complete/{item_id}` | `/v1/source/upload-complete` | 成功为 `status,item_id,linked_tasks`；缺失 400、重复 409 |
| `DELETE /datasets/{dataset_id}`                               | `/v1/source/delete`          | 204 空响应                                               |
| `DELETE /datasets/{dataset_id}/items/{item_id}`               | `/v1/source/delete`          | 204 空响应                                               |

内部请求使用可信网关 bearer，账号 UUID 从公开 API 的当前认证结果取得；原生能力再次检查当前活跃账号、上传归属或全局素材库管理权。来源服务不读取 API 的 JWT、Redis 或 Celery 配置。普通上传仍允许当前支持的活跃账号角色；来源删除仍仅允许全局素材库管理员。引用中的来源返回 409，须先在对应项目或功能中解除引用。

内部信封固定为 `aap-assets-source/v1`：成功仅含 `schema,ok:true,data`，失败仅含 `schema,ok:false,error:{code}`。完整 JSON Schema 随纯合同 wheel 分发，位于 `apps/assets-source/src/aap_assets_source/protocol.schema.json`。调用方检查 actor、父子与操作 UUID，未知版本、字段、错误或错误 scope 明确失败，不接受部分结果。请求/响应默认上限为 1/16 MiB，超限在事务提交前拒绝。

## 上传与重放

初始化的 UUID `Idempotency-Key` 是未来 Item UUID；省略时 API 生成一次。初始化仅登记 intent，返回有效期 900 秒的 staging PUT URL，不创建 Item 或 Task。尚未完成时，复用相同键和文件参数可重取同一 staging 地址；相同键不能换账号、数据集或文件参数。已完成的初始化不再签发 PUT URL。

完成阶段可使用自己的 UUID `Idempotency-Key`，成功回传该键。现代上传以原 Item/intent 的终态结果重放；旧条目的完成操作还使用这个 UUID 保存自己的不可变回执。SDK 提供 `client.datasets.upload_init(..., idempotency_key=init_key)` 与 `upload_complete(..., idempotency_key=complete_key)`，便利方法 `upload_files()` 为各阶段生成 UUID。Web 上传队列的同一文件 worker 保留两个不同的阶段键。没有新增通用自动重试；结果不确定时保留原条目、请求与阶段键确认。

服务锁住当前身份、Dataset 和 intent，读取 staging HEAD，以同一 ETag 条件读取最多 256 KiB 图像头，再条件复制到本次 publication UUID 的来源路径。EXIF 旋转后的尺寸、Item、Labeling 任务追加、计数和回执共同提交；完成重放绕过复制。读取或复制时来源变化返回 `source_changed`，回滚当前 SQL，不能把权限或网络错误当作文件缺失。图像格式无法解析时仍可保留无尺寸记录。

旧无 intent 条目没有可证明的历史上传者；当前活跃账号按原规则完成它，不补造归属。服务只读旧字节，使用原生旧上传锁和完成能力保存元数据、任务及 Assets 自己的回执。缺失/重复不清除旧行或对象。重放还检查父子存在及当前来源路径/类型，不能将历史结果应用到替换后的来源。

## 事务与存储权限

服务使用专用 execute-only LOGIN，仅执行五项现代上传、两项删除和两项旧上传能力；不能直接读写业务表、序列、其他 owner schema 或继承角色。原生 SQL 安装与 owner 授权见[同库数据接口](/api/guides/module-data)。来源删除的 Audit、回执和派生清理 outbox 与业务删除在同一事务；原始字节始终保留。

S3 身份只拥有 import 桶 `dataset-uploads/*` 的 Get/Put、datasets 桶历史来源的 Get，以及 `dataset-sources/*` 的 Put。它不能删除对象、列桶或管理桶，也不写 media-cache。内部 I/O endpoint 与浏览器签名 endpoint 分别显式配置，签名后不改 Host。服务不读取请求提供的任意 URL、bucket 或对象 key。

单次调用在验证完整结果及响应字节预算后才提交。复制与 PostgreSQL 不能共同回滚：复制后进程退出或 SQL 失败可能保留未登记的原始拷贝，重试使用新的 publication 代次，旧拷贝不自动删除。原始拷贝和未完成 intent 暂无自动回收，需要计入存储容量。

成功发布后仍由 API 派发缩略图或视频元信息；发布成功不等于媒体已准备完成。现有派发、导入/扫描、媒体派生及完整 Blob 协议继续按各自边界推进。本工件不代表整个 Assets 或其他业务模块已能独立部署；共享 PostgreSQL 仍是前提。

## 运行与回退

先应用 0213、运行 module-data owner 授权与 `infra/docker/assets-source-grants.sql`，再为非 root 用户 10003 准备专用 secret、固定桶和 S3 origin。独立启动配置见仓库 `DEV.md`、`docker-compose.assets-source.yml` 及服务 README。先确认服务 `/health` 与 `/ready`，再设置网关 `ASSETS_SOURCE_URL`、`ASSETS_SOURCE_TOKEN` 并刷新 API。网关 I/O 超时默认 300 秒，上限 3600 秒；独立服务的池、SQL、S3 连接/读取和消息预算均可独立限制。

回退到本地模式可清空网关 URL 并刷新 API，保留兼容的 SQL 与完成回执，再停止指定独立 Compose 项目。回退迁移前须停止来源写入并处理仍需确认的操作；空库迁移往返不证明删除在途回执安全。Go 仍是隔离实验，不进入本服务或真实环境。

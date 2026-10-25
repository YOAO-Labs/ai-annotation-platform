---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-21
---

# 独立素材来源控制

`apps/assets-source` 是 Assets 来源控制运行时，持有单文件上传初始化、发布完成、Dataset/Item 删除、同步 ZIP 入库、旧对象桶扫描，以及存储连接器列表、读取、创建、更新、删除和连通性测试。公开 API 继续认证 JWT/API key、检查各入口既有 scope 与角色并保持原响应；显式远程配置失败会报错，不回退到本地。未配置远程地址时使用同一工作流与原生能力。

## 接口与身份

| 公开 REST                                                     | 内部 POST                    | 结果                                                                           |
| ------------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------ |
| `POST /datasets/{dataset_id}/items/upload-init`               | `/v1/source/upload-init`     | `item_id,upload_url,expires_in`                                                |
| `POST /datasets/{dataset_id}/items/upload-complete/{item_id}` | `/v1/source/upload-complete` | 成功为 `status,item_id,linked_tasks`；缺失 400、重复 409                       |
| `DELETE /datasets/{dataset_id}`                               | `/v1/source/delete`          | 成功 204；引用保留、操作繁忙或幂等冲突 409                                     |
| `DELETE /datasets/{dataset_id}/items/{item_id}`               | `/v1/source/delete`          | 成功 204；引用保留、操作繁忙或幂等冲突 409                                     |
| `POST /datasets/{dataset_id}/items/upload-zip`                | `/v1/source/upload-zip`      | `added,deduped,skipped,errors,total_in_zip,linked_tasks,scene_inference_notes` |
| `POST /datasets/{dataset_id}/items/scan`                      | `/v1/source/scan`            | `status,new_items,linked_tasks`                                                |

内部请求使用可信网关 bearer，账号 UUID 从公开 API 的当前认证结果取得；原生能力再次检查当前活跃账号、上传归属或全局素材库管理权。来源服务不读取 API 的 JWT、Redis 或 Celery 配置。普通上传和 ZIP 入库允许符合现有策略的活跃账号；ZIP 只要求 `datasets:write`，不要求素材库管理员。来源删除仍仅允许全局素材库管理员。扫描保留现有 `super_admin`、`project_admin` 管理员集合并要求 `datasets:write`。引用中的来源返回 409，须先在对应项目或功能中解除引用。

来源删除同时检查单条素材所在场景的轨迹、轨迹操作和质量历史，以及视频章节、活跃视频媒体作业、未完成清单和仍被保留的图片／视频输入。检查通过前不删除素材、不取消视频作业，也不写入成功审计、删除回执或存储清理任务。场景锁先于素材锁取得，竞争返回可重试冲突。终态且已过保留期的无引用输入不会因旧回执永久阻止删除。

409 的响应体保留具体原因：`detail.code=source_in_use` 表示仍有保留引用，`detail.reason` 可表示 `source_busy` 或 `operation_conflict`。前者须由对应项目或功能处理；忙锁可稍后重试，幂等键冲突须核对原请求。取消 tracker、终止轨迹或软删除相机标注会保留历史记录，仍有引用时继续拒绝来源删除。部分历史引用没有单独的解除入口，不能将取消执行或项目解绑视为保留原任务与标注的通用解除方式。

普通来源操作使用 `aap-assets-source/v1` 信封：成功仅含 `schema,ok:true,data`，失败仅含 `schema,ok:false,error:{code}`。批量导入使用独立的 `aap-dataset-source-ingest/v1` 数据合同。完整 JSON Schema 随纯合同 wheel 分发，位于 `apps/assets-source/src/aap_assets_source/protocol.schema.json`。调用方检查 actor、父子与操作 UUID，未知版本、字段、错误或错误 scope 明确失败，不接受部分结果。JSON 请求/响应默认上限为 1/16 MiB；ZIP 使用单独的归档预算。

## 可选指南对象能力

同一二进制支持 `ASSETS_SOURCE_MODE=guide-only` 和 `source-and-guide`，默认仍为 `source`。
`guide-only` 无需数据库或原来源凭据，仅开放健康检查及带 bearer 认证的
`POST /v1/guide-objects/execute`；其 readiness 不探测 S3 或 IAM。
组合模式为指南对象使用独立存储身份，不扩大原来源凭据的权限。

`aap-assets-guide-object/v1` 请求固定包含 `schema,request_id,operation,project_id,key,content_type`。
`presign_put` 的 `content_type` 必须为字符串，其他操作必须传 null；业务 MIME 校验由 Workspace 持有。
`key` 必须属于请求项目的 `projects/{project_id}/guide/`，桶由部署配置固定；
不接受版本、凭据、actor 或权限布尔值。回复绑定请求 UUID、操作、项目和 key。
Schema 随合同包分发于 `aap_assets_source/guide_object_contract.schema.json`。

| 操作          | `result`                                       |
| ------------- | ---------------------------------------------- |
| `presign_put` | `url,expires_in=900`                           |
| `head`        | `content_length`；缺失对象为 null              |
| `delete`      | `deleted=true`；普通删除，不指定版本           |
| `presign_get` | `url,expires_in=3600`；签名保留 600 秒对齐窗口 |

失败返回绑定作用域的 `ok=false,error:{code}`，code 仅为 `storage_error`、
`invalid_request`、`unavailable` 或 `busy`。无效请求返回通用 400，未认证为 401，
请求超限为 413；未预期的存储异常为通用 500，不透传 SDK 信息。
客户端与 SDK 均不自动重试。`ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT` 默认 4，超额立即拒绝。

指南身份只允许配置标注桶 `projects/*/guide/*` 的 Get/Put/普通 Delete，
无需 ListBucket 或桶管理；本地 MinIO 验证使用显式 Deny `s3:DeleteObjectVersion` 保留历史版本。
内部普通 I/O 使用 SigV4；预签名保留旧 SigV2 内部签名后重写 `/minio` 前缀的行为。
配置使用 `ASSETS_SOURCE_GUIDE_S3_*`，不从 API/MinIO/AWS 环境回退；完整变量见
[环境变量参考](/dev/reference/env-vars)和服务 README。

本接口只执行对象操作，Workspace 仍负责项目权限与元数据。公共指南工作流已接入 Workspace，命令的三路径与恢复验收仍在推进。
网络超时或取消后的对象效果可能不确定；S3 与 SQL 没有原子提交，也不提供可自动重放的删除回执。
实现边界、当前本地证据与后续命令验收见 [ADR-0144](/dev/adr/0144-workspace-guide-assets-owner)。

## 存储连接器管理

六个公开连接器端点保留原路径、权限、状态码和响应模型。它们调用 Assets Source 的内部 `POST /v1/connections/{list,read,create,update,delete,test}` 工作流。本地模式复用同一 Python 工作流并在调用方数据库事务中执行；配置 `ASSETS_SOURCE_URL` 与 `ASSETS_SOURCE_TOKEN` 后，API 调用独立 Source，远程错误会直接返回，不自动切到本地。`GET /storage-connections` 仍返回全部可见连接器，网关逐页读取并校验完整快照与游标，不把内部分页暴露给公开响应。

内部协议为 `aap-assets-connections/v1`，完整请求、结果和原生参数合同见 `apps/assets-source/src/aap_assets_source/connections_contract.schema.json`。本地与远程连接器请求/响应都受 1/16 MiB 单次预算约束；完整列表由多个有界私有页面组成。

创建、更新、删除和测试接受可选 UUID `Idempotency-Key`。省略时 API 生成新 UUID，成功响应回传实际使用的键，创建的连接器 ID 等于该键。结果不确定时，调用方应保存并复用首次请求的键；同键同请求可取回原结果，改变请求会冲突。SDK/CLI 不会自动重试这些操作。测试连通失败只返回安全异常类型，不回传 SDK 异常文本、目标 URL 或凭据。

新连接器的历史兼容字段 `project_id` 会被忽略，所有新连接器均保存 `project_id=null`。全体活跃用户可使用 global 连接器；owner 连接器由创建者和超级管理员使用。owner 创建者即使平台角色后来降为 employee 或 viewer，仍可管理该连接器；global 连接器只由超级管理员管理。

## 上传与重放

初始化的 UUID `Idempotency-Key` 是未来 Item UUID；省略时 API 生成一次。初始化仅登记 intent，返回有效期 900 秒的 staging PUT URL，不创建 Item 或 Task。尚未完成时，复用相同键和文件参数可重取同一 staging 地址；相同键不能换账号、数据集或文件参数。已完成的初始化不再签发 PUT URL。

完成阶段可使用自己的 UUID `Idempotency-Key`，成功回传该键。现代上传以原 Item/intent 的终态结果重放；旧条目的完成操作还使用这个 UUID 保存自己的不可变回执。SDK 提供 `client.datasets.upload_init(..., idempotency_key=init_key)` 与 `upload_complete(..., idempotency_key=complete_key)`，便利方法 `upload_files()` 为各阶段生成 UUID。Web 上传队列的同一文件 worker 保留两个不同的阶段键。没有新增通用自动重试；结果不确定时保留原条目、请求与阶段键确认。

服务锁住当前身份、Dataset 和 intent，读取 staging HEAD，以同一 ETag 条件读取最多 256 KiB 图像头，再条件复制到本次 publication UUID 的来源路径。EXIF 旋转后的尺寸、Item、Labeling 任务追加、计数和回执共同提交；完成重放绕过复制。读取或复制时来源变化返回 `source_changed`，回滚当前 SQL，不能把权限或网络错误当作文件缺失。图像格式无法解析时仍可保留无尺寸记录。

旧无 intent 条目没有可证明的历史上传者；当前活跃账号按原规则完成它，不补造归属。服务只读旧字节，使用原生旧上传锁和完成能力保存元数据、任务及 Assets 自己的回执。缺失/重复不清除旧行或对象。重放还检查父子存在及当前来源路径/类型，不能将历史结果应用到替换后的来源。

## 同步 ZIP 与桶扫描

现有公开路由 `POST /datasets/{dataset_id}/items/upload-zip` 和 `POST /datasets/{dataset_id}/items/scan` 仍同步执行，响应字段保持兼容。网关接受可选 UUID `Idempotency-Key`，并在成功响应中回传该请求头。ZIP 的操作身份绑定原始归档 SHA-256；扫描绑定固定的扫描命令指纹。以同一键重试不确定响应时返回第一次完成的收据。复用键但更换 actor、Dataset、操作类型或 ZIP 字节会冲突。调用方不要对 POST 进行隐式自动重试，应保存原键后按需重放。语言无关数据、错误和归档预算冻结在包内 `ingest_contract.schema.json`，与原生 SQL 的同名 schema 工件保持一致。

ZIP 要求当前活跃账号和 `datasets:write`，不要求管理员角色。压缩包和展开数据各不超过 200 MiB，最多 5,000 个文件条目、每个条目最多 100 MiB、压缩比不超过 100:1，中央目录最多 10,000 条。隐藏/macOS 文件和不安全路径沿用旧规则；普通文件保留 MIME 类型推断、内容哈希去重、逐文件错误、basename `file_name` 及逻辑子目录布局。

ZIP 将新字节写入不可变 key：`dataset-sources/{dataset_id}/{item_id}/{publication_id}/{logical_relpath}`。Item 元数据在 `metadata.source_ingest` 保存操作 UUID 和逻辑相对路径。重复请求和失败回滚都不会覆盖或删除旧来源。ZIP 场景推断只在已有文件新增且数据集尚无 Scene、无既有场景分配时执行，并基于所有历史与新增 Item 规划。若 `is_temporal` 为真但没有可接受的 Scene，整个 SQL 事务回滚，已复制的原始字节仍保留。

每个 Source 进程只接收一个 ZIP 请求并在数据库事务开始前将已认证的请求流 spool 到临时文件。公开网关按实际流量限制 multipart 包体，为边界和头部另留 64 KiB；断连或异常会关闭解析器的临时文件。独立 Compose 资源上限为 512 MiB 内存和 256 MiB `/tmp`。保留的可信 nuScenes namespace 仍受写入保护；扫描还会拒绝以 `dataset-sources` 或 `dataset-uploads` 为首段的 legacy prefix，不改变 Dataset 名称目录的通用规则。

扫描保留管理员权限，只枚举固定 datasets 桶中当前 Dataset 的精确 `{Dataset.name}/` legacy 前缀，按 S3 页流式读取，不设新增的文件数/字节截断上限。它跳过已登记路径，只为缺失路径登记 Item，保留旧扩展名类型判断与子目录文件名；只有单段 ETag 是有效 MD5 时才将其作为内容哈希提示。扫描不复制或删除对象，也不推断 Scene，不会把 `dataset-sources/` 下的现代 publication 当成 legacy 对象或越过当前 Dataset 前缀。

每次操作的 Item 写入、Dataset 计数、Labeling append 回执、ZIP Scene/frame 变更、最终收据和媒体派发意图在同一个外层 PostgreSQL 事务中提交。Scene 名称或 UUID 冲突会使整个操作失败回滚，不能把回滚前的正向计数作为成功响应返回。对象存储无法参加数据库回滚，因此事务失败后已写入的 ZIP publication 字节会保留，不做原始来源清理。媒体 outbox 由 API maintenance relay 领取和确认；其两个 SQL 能力不授予 Source LOGIN，也不代表媒体计算已完成。

## 事务与存储权限

Source LOGIN 当前执行 26 项公开能力：来源/入库、连接器以及三个图片来源/作业受理函数。连接导入器使用独立的 9 项 LOGIN 能力；两者均不能直接读写业务表、序列、其他 owner schema 或继承角色。两个媒体派发 claim/ack 能力只由 API maintenance relay 使用。原生 SQL 安装与 owner 授权见[同库数据接口](/api/guides/module-data)。来源删除的 Audit、回执和派生清理 outbox 与业务删除在同一事务；原始字节始终保留。

S3 身份只拥有 import 桶 `dataset-uploads/*` 的 Get/Put、datasets 桶历史来源读取与该桶自身的 ListBucket，以及 `dataset-sources/*` 的 Put 和 multipart Abort。扫描只对原生能力返回的 Dataset 前缀执行条件 HEAD/GET。它不能删除对象、列其它桶或管理桶，也不写 media-cache。内部 I/O endpoint 与浏览器签名 endpoint 分别显式配置，签名后不改 Host。服务不读取请求提供的任意 URL、bucket 或对象 key。

单次调用在验证完整结果及响应字节预算后才提交。复制与 PostgreSQL 不能共同回滚：复制后进程退出或 SQL 失败可能保留未登记的原始拷贝，重试使用新的 publication 代次，旧拷贝不自动删除。原始拷贝和未完成 intent 暂无自动回收，需要计入存储容量。

图像 publication、DatasetItem 注册、来源身份绑定和缩略图/金字塔作业受理在同一个数据库事务内完成；导入器在锁定业务行前验证已发布对象身份，worker 随后按已绑定的身份执行。上传和导入成功表示作业已持久受理，不表示媒体已生成。视频元信息继续由视频媒体 owner 处理。本工件不代表整个 Assets 或其他业务模块已能独立部署；共享 PostgreSQL 仍是前提。

## 运行与回退

先应用当前迁移、module-data owner 授权与 `infra/docker/assets-source-grants.sql`，再为非 root 用户 10003 准备专用 secret、固定桶和 S3 origin。独立启动配置见仓库 `DEV.md`、`docker-compose.assets-source.yml` 及服务 README。先确认服务 `/health` 与 `/ready`，再设置网关 `ASSETS_SOURCE_URL`、`ASSETS_SOURCE_TOKEN` 并刷新 API。网关 I/O 超时默认 300 秒，上限 3600 秒；独立服务的池、SQL、S3 连接/读取和消息预算均可独立限制。0215 的 SQL 工件仍按 core 后 delivery 的顺序安装；降级会移除来源和连接器回执并丢失同键重放结果，故应先停止来源写入并排空活动操作和待派发媒体记录。

回退到本地模式可清空网关 URL 并刷新 API，保留兼容的 SQL 与完成回执，再停止指定独立 Compose 项目。回退迁移前须停止来源写入并处理仍需确认的操作；空库迁移往返不证明删除在途回执安全。Go 仍是隔离实验，不进入本服务或真实环境。

## 持久连接器导入

`POST /api/v1/datasets/{dataset_id}/import-from-connection` 仍返回 202 与 `job_id`，并保留 `datasets:write`、全局管理员角色、连接器可见性和出网白名单检查。可传 UUID `Idempotency-Key`，成功时回显；相同键与请求找回原作业，改变请求复用旧键返回 409。当前账户与权限在重放时仍需有效。

`aap-dataset-import/v1` 将接受、租约、冻结清单、逐文件完成和终态分开；每次调用在完整回复验证后提交。`python -m aap_assets_source_import` 使用独立数据库身份轮询已接受作业，不导入 API、ORM、Celery 或 Redis。七项执行能力只允许处理已接受的导入；接受和取消能力由 API 使用，不授予执行器。

每个作业固定预算与连接器配置/加密凭据快照。已接受作业继续使用这份快照；删除连接器不会删除快照或撤销已受理的执行授权。执行时再次读取有效白名单，显式空数据库覆盖保持拒绝。已接受执行不因原发起者被停用而丢弃；显式取消、Jobs 终态、父数据集和当前租约仍逐项围栏。

清单在复制前完整持久保存。每次尝试写入独立 `dataset-sources/{dataset}/{item}/{publication}/` 路径，不覆盖按文件名共用的 key，也不在判重时删除原始字节。条目、文件计数、Labeling append、导入游标/计数和后续派发记录在一个事务内提交。提交结果不确定或进程被终止后，接替执行器从已提交游标继续；数据库锁竞争保留当前文件重试，不计为文件失败。

图片对象身份取自成功上传回执的 ETag、可选 VersionId 和实际字节数，与登记同一份输出相匹配。目标身份继续只有写入与 multipart Abort 权限；缺少有效写入回执时登记明确的文件错误。心跳遇到 `dataset_busy` 时复用当前租约重试，全部重试与续租调用受最后一次确认的到期时间约束；真正失租、取消或无法确认续租时停止 I/O。

S3 源读取核对枚举的 ETag；SFTP 使用已知主机指纹、目录围栏和读取前后身份校验。取消可能无法立即中止外部阻塞调用，但旧结果不能越过 SQL 围栏登记。已提交条目保留；未知或重复的未引用原始拷贝暂不回收。

媒体和通知沿用既有执行方，持久 outbox 由 API maintenance relay 领取并在派发成功后确认。队列投递可能重复；确认代表 broker 接受，不代表媒体计算完成。此能力不迁移连接器管理和媒体执行本身，也不等同完整 Assets 可独立部署。部署及旧作业排空规则见 `DEV.md`、[ADR-0100](/dev/adr/0100-durable-connector-import) 与 [ADR-0101](/dev/adr/0101-assets-synchronous-zip-and-scan)。

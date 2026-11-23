---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-11-16
---

# 素材来源与独立核验

公开单文件上传、Dataset/Item 删除、同步 ZIP 入库和旧桶扫描在 API 内复用 `apps/assets-source` 的工作流，使用调用方事务。API 继续认证 JWT/API key、检查既有 scope 与角色并保持原响应；旧 HTTP 信封保留为可移植合同，不作为这些公共操作的后端选择。连接器导入的准确版本回读、持久恢复和 Native 金字塔签名仍由持有专用凭据的 Source 进程完成。

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

普通来源操作使用 `aap-assets-source/v1` 信封：成功仅含 `schema,ok:true,data`，失败仅含 `schema,ok:false,error:{code}`。批量导入使用独立的 `aap-dataset-source-ingest/v1` 数据合同。完整 JSON Schema 随纯合同 wheel 分发，位于 `apps/assets-source/src/yoao_assets_source/protocol.schema.json`。调用方检查 actor、父子与操作 UUID，未知版本、字段、错误或错误 scope 明确失败，不接受部分结果。JSON 请求/响应默认上限为 1/16 MiB；ZIP 使用单独的归档预算。

## 指南素材边界

指南素材不使用 Assets Source 的独立字节 RPC。公开上传、内容读取与删除入口使用受管上传、
不可变存储引用和维护门；Workspace Configuration 持有发布、解除引用、列表 CAS 与复制命令。
删除只退休最后一个引用，不在项目配置事务中直接删除对象。Assets Source 继续提供数据集、
来源与连接器操作；完整指南流程见用户指南及 Workspace 部署说明。

## 存储连接器管理

列表和读取始终在 API 中使用同一原生能力和有界分页，从不读取密文或依赖 Source HTTP 就绪。仅创建、更新、删除与测试在配置 `ASSETS_SOURCE_URL` 和 `ASSETS_SOURCE_TOKEN` 时走独立 Source；这些路径的捕获、解封或 I/O 不转给 API，远程失败不会回退。未配置 Source URL 的开发装配可按已有本地连接器权限和 Fernet 设置调用同一工作流。

内部协议为 `aap-assets-connections/v1`，完整请求、结果和原生参数合同见 `apps/assets-source/src/yoao_assets_source/connections_contract.schema.json`。本地与远程连接器请求/响应都受 1/16 MiB 单次预算约束；完整列表由多个有界私有页面组成。

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

公开 API 先按实际流量限制 multipart 包体，为边界和头部另留 64 KiB，并把归档保存在有界临时文件。断连或异常会关闭解析器的临时文件。可移植 Source HTTP 制品仍按其资源预算接收 ZIP。可信 nuScenes 命名空间继续受写入保护；扫描拒绝 `dataset-sources` 或 `dataset-uploads` 为首段的前缀。

扫描保留管理员权限，只枚举固定 datasets 桶中当前 Dataset 的精确 `{Dataset.name}/` legacy 前缀，按 S3 页流式读取，不设新增的文件数/字节截断上限。它跳过已登记路径，只为缺失路径登记 Item，保留旧扩展名类型判断与子目录文件名；只有单段 ETag 是有效 MD5 时才将其作为内容哈希提示。扫描不复制或删除对象，也不推断 Scene，不会把 `dataset-sources/` 下的现代 publication 当成 legacy 对象或越过当前 Dataset 前缀。

每次操作的 Item 写入、Dataset 计数、Labeling append 回执、ZIP Scene/frame 变更、最终收据和媒体派发意图在同一个外层 PostgreSQL 事务中提交。Scene 名称或 UUID 冲突会使整个操作失败回滚，不能把回滚前的正向计数作为成功响应返回。对象存储无法参加数据库回滚，因此事务失败后已写入的 ZIP publication 字节会保留，不做原始来源清理。媒体 outbox 由 API maintenance relay 领取和确认；其两个 SQL 能力不授予 Source LOGIN，也不代表媒体计算已完成。

## 事务与存储权限

来源控制、入库和核验用 Source 控制 LOGIN；已登记连接器的物理读写另用保存 I/O LOGIN。Importer 仅能领取已接受的导入、推进托管原件并消费核验回执，不能获取连接器密文。各 LOGIN 不能直接读写业务表、序列或继承角色。两个媒体派发 claim/ack 能力只由 API maintenance relay 使用。见[同库数据接口](/api/guides/module-data)。来源删除的 Audit、回执和派生清理 outbox 与业务删除在同一事务；原始字节始终保留。

S3 身份只拥有 import 桶 `dataset-uploads/*` 的 Get/Put、datasets 桶历史来源读取与该桶自身的 ListBucket，以及 `dataset-sources/*` 的 Put、multipart Abort 和准确版本核验所需的 `GetObjectVersion`。扫描只对原生能力返回的 Dataset 前缀执行条件 HEAD/GET。它不能删除对象、列其它桶或管理桶，普通身份也不写 media-cache。可选 Native 金字塔签名使用单独 Native S3 身份，只为已保存输出签发有限请求，不执行对象 I/O；配置、调用方 token 与策略模板见服务 README 和[图片金字塔运行手册](/ops/runbooks/image-pyramid)。内部 I/O endpoint 与浏览器签名 endpoint 分别显式配置，签名后不改 Host。服务不读取请求提供的任意 URL、bucket 或对象 key。

单次调用在验证完整结果及响应字节预算后才提交。复制与 PostgreSQL 不能共同回滚：复制后进程退出或 SQL 失败可能保留未登记的原始拷贝，重试使用新的 publication 代次，旧拷贝不自动删除。原始拷贝和未完成 intent 暂无自动回收，需要计入存储容量。

图像 publication、DatasetItem 注册、来源身份绑定和缩略图/金字塔作业受理在同一个数据库事务内完成；导入器在锁定业务行前验证已发布对象身份，worker 随后按已绑定的身份执行。上传和导入成功表示作业已持久受理，不表示媒体已生成。视频元信息继续由视频媒体 owner 处理。本工件不代表整个 Assets 或其他业务模块已能独立部署；共享 PostgreSQL 仍是前提。

## 运行与回退

先迁移并安装 `infra/docker/assets-source-grants.sql` 的控制 LOGIN 和[保存 I/O 的独立授权与私网 token](/ops/deploy/docker-compose#source-registered-io)，再启动 `docker-compose.assets-source.yml`；需要 Native 签名时叠加 `docker-compose.assets-native.yml`。API 的连接管理令牌、普通读写令牌、导入租约令牌、准确版本核验令牌及 Native 签名分别配置。`/ready` 只证明 Source 宿主就绪。

控制 LOGIN 的 psql 参数由 `assets-source-grants.sql` 声明。准备 overlay 所需私有文件后，按目标部署的 Compose 项目和文件链启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.assets-source.yml \
  up -d --build --wait assets-source
```

已登记原件、保存产物、在途导入或历史 Native 输出仍依赖 Source 时不能停它。清空 URL 不会迁移责任或把已保存目标改向内建存储。回退前须保留兼容 SQL 和操作回执，按原身份处置在途 I/O。

## 持久连接器导入

`POST /api/v1/datasets/{dataset_id}/import-from-connection` 仍返回 202 与 `job_id`，并保留 `datasets:write`、全局管理员角色、连接器可见性和出网白名单检查。可传 UUID `Idempotency-Key`，成功时回显；相同键与请求找回原作业，改变请求复用旧键返回 409。当前账户与权限在重放时仍需有效。

`aap-dataset-import/v1` 将接受、租约、冻结清单、逐文件完成和终态分开；每次调用在完整回复验证后提交。`python -m yoao_assets_source_import` 使用独立数据库身份轮询已接受作业，不导入 API、ORM、Celery 或 Redis。它用当前作业及租约调用 Source，仅能枚举、读取已观察条目或复制到本次保存目标；请求不能指定任意 endpoint、bucket 或 key。受理和取消仍由 API 执行。

每个作业固定预算与连接器配置/加密凭据快照。已接受作业继续使用这份快照；删除连接器不会删除快照或撤销已受理的执行授权。执行时再次读取有效白名单，显式空数据库覆盖保持拒绝。已接受执行不因原发起者被停用而丢弃；显式取消、Jobs 终态、父数据集和当前租约仍逐项围栏。

清单在复制前完整持久保存。每次尝试写入独立 `dataset-sources/{dataset}/{item}/{publication}/` 路径，不覆盖按文件名共用的 key，也不在判重时删除原始字节。条目、文件计数、Labeling append、导入游标/计数和后续派发记录在一个事务内提交。提交结果不确定或进程被终止后，接替执行器从已提交游标继续；数据库锁竞争保留当前文件重试，不计为文件失败。

图片对象身份取自成功上传回执的 ETag、可选 VersionId 和实际字节数，与登记的同一份输出相匹配。内建目标的 Importer 身份继续只有写入与 multipart Abort 权限，不用于 HEAD／GET；缺少有效写回执或完整核验回执时不能发布。心跳遇到 `dataset_busy` 时复用当前租约重试，全部重试与续租调用受最后一次确认的到期时间约束；真正失租、取消或无法确认续租时停止 I/O。

Source 按观察到的 S3 VersionId 读取并核对 ETag；SFTP 核验主机公钥、目录边界和读取前后身份。取消不能保证已经发出的网络调用立即停止，旧结果不能越过租约围栏登记。已提交条目保留；未知或重复的未引用原始拷贝不自动回收。

### Source 所属的导入核验

Importer 将准确 Job、租约、条目索引、Item、Creation、操作 ID 和读／写模式交给内部
`POST /v1/import/verify`；`POST /v1/import/verify-status` 只查询同一规范操作。
两者只接受 `job_id`、`lease_token`、`entry_index`、`item_id`、`creation_id`、
`operation_id` 和 `mode` 七个字段。Source 通过原生能力解析冻结内容、保存位置和实际版本，
并执行既有完整 SHA／长度回读；请求不接收 endpoint、bucket、key 或调用方指定的读预算，
响应不返回原件字节。

Source 配置 `ASSETS_SOURCE_IMPORT_VERIFY_TOKEN`，Importer 配置相同内容的
`ASSETS_IMPORT_VERIFY_TOKEN` 以及实际可达的 `ASSETS_IMPORT_VERIFY_URL`。
秘密值可使用各自 `_FILE` 入口，不能同时提供值与文件；该独立核验 secret 必须与
API 网关的 `ASSETS_SOURCE_TOKEN` 不同，网关 token 不能调用核验端点。

Source 的 `ASSETS_SOURCE_IMPORT_VERIFY_TIMEOUT_SECONDS` 限制接纳与整段回读，默认 120 秒、
上限 600 秒，不复用单次 SQL 或 S3 socket 超时。短暂锁争用或连接超时仍在原预算和有效
租约内继续接纳；Source 私有 nonce 只恢复本请求尚未启动物理读取的接纳，不增加 HTTP 字段。
Importer 保留自身有限等待期限。初始错误或内容不可信的回复只恢复原操作状态，不重投核验；
本地冻结的 SHA256、大小、ETag、版本，以及新写入的 MD5 均在此阶段比对，状态回执不符则拒绝。
接纳取消／连接丢失时，Source 以准确 nonce 结算未启动责任并保留原失败；数据库仍不可达时
记录失败、保留未证实的许可，不假装已排空。

每次核验保留准确操作／租约回执；读模式 reservation 使用调用方已知的稳定操作身份，
可按同一身份恢复，未启动的 reservation 才能原子取消。已启动的读必须先实际排空，
不能因调用方退出、超时或失租便释放许可。已保存的排空证据由 Source owner 继续结算，
不要求原调用方持续轮询，也不因此获得旧租约发布权。

条目发布及重复跳过均须提供 `verification_operation_id`，与当前租约、条目、Creation
和准确核验完成回执一致；不能把 Creation 首次 verified 状态当作另一次读模式已完成。
当前完成文档位于打包的 `0453_import_original_completion.schema.json`，冻结的历史 schema
保持不变。已接受作业不重新授权原发起者；取消、删除与当前 Job／租约围栏仍生效。

内建目标由 Importer 的受限身份写入，Source 在原 `dataset-sources/*` 范围用 `GetObjectVersion` 验证。已登记 S3 目标的连接凭据仅由 Source 打开，用于保存前缀内的条件复制与准确版本回读；Importer 只持当前租约的窄令牌。缺少准确版本、回执或 `GetObjectVersion` 权限即失败，不自动授权，也不改读当前版本。

媒体和通知沿用既有执行方，持久 outbox 由 API maintenance relay 领取并在派发成功后确认。队列投递可能重复；确认代表 broker 接受，不代表媒体计算完成。先按[同库迁移约束](./module-data#持久连接器导入)排空或取消旧导入，应用迁移、owner grants、`assets-import-grants.sql` 和 Source 保存 I/O 授权。Source 就绪后，复用目标部署项目与环境文件，追加 Importer overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.assets-source.yml \
  -f docker-compose.assets-import.yml up -d --build --wait assets-import
docker compose -f docker-compose.yml -f docker-compose.assets-source.yml \
  -f docker-compose.assets-import.yml exec assets-import \
  python -m yoao_assets_source_import --healthcheck
```

刷新实际 maintenance worker 和 Beat，核对 `app.workers.dataset_import_delivery.relay_dataset_import_deliveries` 已注册并被调度。`--once` 会领取并推进业务，不是健康探针。退役时先停止受理，结清领取和待派发，再停进程；不自动删除未登记对象，也不切回旧 Celery 导入。

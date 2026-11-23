---
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 素材目录及可移植合同

公开目录、关联及空间素材由 API 在调用方数据库会话内执行 `yoao_assets_v1` 能力。创建、更新与回执使用同一事务，文件引用按当前认证主体交给媒体 owner 校验。`apps/assets-catalog` 保留独立依赖、锁文件、镜像、受限 PostgreSQL LOGIN、只读 S3 签名身份和冻结 HTTP 合同，用于制品及互操作验收，不再是产品目录配置。

该工件覆盖五项目录操作和两项关联读取。关联写入、建题、导入、上传、来源删除与派生处理继续使用各自已有的事务或执行边界；完整 Assets 模块尚未独立部署。目录 SQL 仍依赖共享 PostgreSQL。

## 公开 REST

| 路由                              | 权限                               | 成功结果                               |
| --------------------------------- | ---------------------------------- | -------------------------------------- |
| `GET /api/v1/datasets`            | 活跃账号，`datasets:read`          | 数据集分页及统计                       |
| `GET /api/v1/datasets/{id}`       | 活跃账号，`datasets:read`          | 数据集详情，缺失为 404                 |
| `GET /api/v1/datasets/{id}/items` | 活跃账号，`datasets:read`          | 文件分页与签名 URL，父数据集缺失为 404 |
| `POST /api/v1/datasets`           | 当前素材库管理员，`datasets:write` | 201，已创建数据集                      |
| `PUT /api/v1/datasets/{id}`       | 当前素材库管理员，`datasets:write` | 200，已更新数据集                      |

创建和更新接受可选 UUID `Idempotency-Key`，成功时回传实际使用的键。响应中断后使用相同请求、账号和键重试；原结果和资源 UUID 不变。同键换内容、换操作或换目标返回 409。回执保留原快照，后续更新或删除不修改该回执；旧键不会复建已删除的数据集。公开更新仍检查当前目录写入权限和 Dataset 生命周期。

SDK 的 `client.datasets.create(..., idempotency_key=key)` 与 `update(..., idempotency_key=key)`、CLI 的 `datasets create/update --idempotency-key UUID` 和 Web 包装器支持传入保存的键。省略键时每次新调用都会选择新 UUID；要跨调用确认不确定结果，应事先保存并复用自己的键。

`name` 最多 255 字符，`data_type` 最多 30 字符。更新时 `name`、`description` 的 null 值忽略；省略 `axis_convention` 保留原值，显式 null 仅移除该元数据字段，其他元数据保留。重命名不会移动对象。普通创建不写空目录对象，桶由部署流程预先配置。

## 内部协议

内部操作全部为带专用 Bearer secret 的 POST，使用 JSON 请求和 `aap-assets-catalog/v1` 响应。请求不得包含额外字段；`actor_id` 只能由网关从已认证主体取得，不能接受浏览器传入的身份或角色。服务 token 本身不授予调用者随意选择业务身份的权限，不应暴露给最终用户。

| 内部路由             | 请求                                                                                   | 成功 `data`                           |
| -------------------- | -------------------------------------------------------------------------------------- | ------------------------------------- |
| `/v1/catalog/list`   | `actor_id`；可选 `search,data_type,has_scenes,limit,offset`                            | `items,total,limit,offset`            |
| `/v1/catalog/get`    | `actor_id,dataset_id`                                                                  | `dataset`                             |
| `/v1/catalog/items`  | `actor_id,dataset_id`；可选 `limit,offset`                                             | `dataset_id,items,total,limit,offset` |
| `/v1/catalog/create` | `operation_id,actor_id,name`；可选 `description,data_type,is_temporal,axis_convention` | `operation_id,actor_id,dataset`       |
| `/v1/catalog/update` | `operation_id,actor_id,dataset_id,patch`                                               | `operation_id,actor_id,dataset`       |

成功分支仅含 `schema,ok:true,data`；业务失败仅含 `schema,ok:false,error:{code}`。未知 schema、额外字段、错误分支或范围不符均拒绝。业务错误为 `invalid_request`、`account_not_active`、`account_busy`、`invalid_account_role`、`catalog_manager_required`、`dataset_missing`、`source_busy`、`operation_conflict` 和 `reserved_namespace`。网关将其映射为公开 401/403/404/409/422；不返回 SQL 错误或存储凭据。

分页 `limit` 为 1–200，`offset` 为非负 PostgreSQL bigint。列表按 `created_at DESC,id DESC` 排序；计数、分页及统计来自同一 SQL 语句快照。名称查询保留 ILIKE 通配符语义，三种点云名称 `lidar/point_cloud/pointcloud` 使用同一筛选族。文件元数据保留未知 JSON 字段；内部 `thumbnail_path` 只用于签名，公开响应不返回它。图片原图和缩略图地址由公开网关向 Images owner 核验来源身份后签发；来源缺失、漂移或核验失败时两种 URL 都为 null，不回退到目录快照中的对象 key。尺寸保留当前 owner 投影事实；缩略图和 blurhash 仅在派生代次匹配当前来源时返回。视频原始文件与封面仍由媒体 owner 核验，目录服务本身不签发视频 URL；元信息尚未准备时可返回已核验的原始地址，封面留空，来源漂移或核验失败时不沿用旧链接。

`POST /api/v1/datasets/{dataset_id}/backfill-dimensions` 和 `backfill-media` 接受持久 Image backfill，并返回 `202`、`dataset_id`、`job_id`、`operation_id`、`purpose`、当前作业 `status` 与 owner 确认的 `accepted_before`。可选 UUID `Idempotency-Key` 用于重放同一受理结果。前者只补缺失尺寸；后者接受缩略图回填，并保留独立视频元信息回填。旧 `batch` 查询参数为兼容保留且已弃用；owner 自行按有界批次扫描。

补算受理复用数据集写入许可和同一事务内的生命周期锁，不要求受限 API 身份再次执行目录私有查询。不存在或已归档的数据集不会进入作业；原件读取仍受媒体 owner 的当前许可与准确来源约束，不能用目录读取权限代替。

内部请求上限 1 MiB，独立 HTTP 响应上限 16 MiB；超出时明确失败，不截断。API 使用有界的原生页面，不为正常请求建立 Catalog HTTP 连接。这些预算不构成生产容量资格。

## 数据与凭据

原生入口位于 `yoao_assets_v1`：`list_catalog_datasets`、`get_catalog_dataset`、`list_catalog_items`、`create_catalog_dataset`、`update_catalog_dataset`。冻结 SQL 和 JSON Schema 位于 `apps/api/alembic/sql/0211_dataset_catalog.*`。写入和 `dataset_catalog_operations` 回执共用一项调用方事务；函数不自行提交，不执行 S3 或 broker 操作。

独立制品验收先完成 module-data owner 授权，再运行 `infra/docker/assets-catalog-grants.sql`。其 LOGIN 只能执行目录、关联与空间素材的 29 项公开能力，不获业务表、视图、序列或其他 owner schema 权限。内嵌 API 使用自己的 LOGIN，由当前账号事实和 Workspace／Labeling 投影复核权限；目录回执仍是 owner 私表。

目录 S3 身份仅需 `GetObject`：一个 datasets 桶的历史及新来源路径，以及固定 media-cache 桶中的 `thumbnails/`、`videos/`、`playback/`、`image-pyramids/`。不授予列桶、写入、删除或桶管理权限。公开签名 origin 显式配置；不能从请求指定 bucket、endpoint 或任意 key。空间素材准备还使用同一只读身份条件读取有限字节的标定 JSON；固定连接 origin 可通过 `ASSETS_CATALOG_S3_ENDPOINT` 与公开签名地址分开配置。

## 切换与恢复

产品不再读取 `ASSETS_CATALOG_URL`、专用 token 或 timeout，旧 Compose 文件已退役。停止旧写入口、保留同一数据库和回执，再刷新已配置普通运行 LOGIN 的 API；发现不确定结果时沿用原键，不另发一次写入。独立制品仍以专属数据库 URL、token 和只读 S3 运行，不把这些凭据交给 API。

回退服务实现可保留兼容 SQL 和回执；如要回退迁移，先停止所有目录写入并保存尚需重放的操作。删除回执表会失去不确定提交的确认能力，不能直接按空库迁移测试推断在途回退安全。当前回执没有自动清理。

## 关联读取

两个读取使用独立的 `aap-assets-associations/v1` envelope；成功仍仅含 `schema,ok:true,data`，失败仅含 `schema,ok:false,error:{code}`。请求只接受两个 UUID，网关从已认证账号取得 `actor_id`。

| 公开 REST                                    | 内部 POST                      | 成功 `data`           |
| -------------------------------------------- | ------------------------------ | --------------------- |
| `GET /api/v1/datasets/{dataset_id}/projects` | `/v1/catalog/dataset-projects` | `dataset_id,projects` |
| `GET /api/v1/projects/{project_id}/datasets` | `/v1/catalog/project-datasets` | `project_id,datasets` |

素材库侧沿用 `datasets:read`：活跃账号可以查看关联项目，不额外套用项目成员过滤；数据集缺失或没有关联都返回空列表。项目侧沿用当前项目可见性，不额外要求 `datasets:read`；缺失或不可见均为 404，停用账号为 401，无效账号角色、无效成员职责或不兼容的 viewer 职责为 403。可见项目没有关联时返回空列表。

项目列表保留原 `ProjectOut` 行为：配置由 Workspace 提供，任务计数和 `batch_summary` 使用 Labeling 已保存的投影。类别、类别配置和属性 schema 由公开 `yoao_workspace_contract` 函数派生。关联列表不增加负责人姓名/头像、成员数量、当前成员职责、模型实例或 AI 完成量查询；这些字段沿用原来的 null/0/空值。

项目关联的数据集每行仅含 `id,display_id,name,data_type,linked_at,items_count,tasks_in_project`。条目计数包含全部素材；任务计数只包括路径项目下通过 `Task.dataset_item_id` 直接引用素材的 Task，不按状态过滤，不回退 secondary 或旧 `primary_lidar` 链接。时间使用 UTC ISO 字符串和 `+00:00`，结果按关联时间倒序并以关联 UUID 消除同时间歧义。

原生入口为 `yoao_assets_v1.list_dataset_projects(uuid,uuid)` 与 `list_project_datasets(uuid,uuid)`；SQL 和冻结 schema 位于 `0212_asset_association_reads.*`。Identity 提供最小账号事实，Workspace 提供配置/当前可见性，Labeling 通过 Assets 的来源投影做整组计数；消费者不读取其他 owner 的私有表，也不传输大批素材 UUID。一次顶层 SQL 语句的快照同时决定权限、关联、配置和计数；正在执行的读取按该快照完成，后续请求看到已提交的撤权或关联代次变化。

业务错误码为 `invalid_request,account_not_active,invalid_account_role,project_missing,invalid_project_role,incompatible_project_role`。错误 schema、错父级或重复 UUID 会明确失败，不返回部分列表。Scene、位姿、标定、来源快照与同事务导入详见[空间素材协议](./assets-spatial)。

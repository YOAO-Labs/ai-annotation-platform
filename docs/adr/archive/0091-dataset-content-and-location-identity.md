# 0091 — 数据集内容身份与托管位置兼容分离

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** 项目维护者授权第一阶段实施；实现依据当前仓库契约
- **Supersedes:** —

## Context

数据集名称没有唯一约束，却参与托管对象键。改名后继续上传会分散到新目录，同名不同内容可能覆盖旧任务原图。历史 content_hash 混合真实 MD5、ETag 和未标来源的值，不能直接升级成可信内容身份。Item 的 updated_at 参与导出快照，纯管理回填也不应改变它。

## Decision

- 新托管键采用 `__aap_managed_datasets__/<dataset UUID>/_objects/<upload UUID>/<逻辑相对路径>`。同一数据集内逻辑相对路径发生冲突时在扩展名前追加序号；唯一上传 UUID 防止失败清理或重复请求删掉其他 Item 的对象。分配逻辑名称时持有 Dataset 行锁。
- 新 Item 的 metadata.storage_relative_path 保存分配后的逻辑路径，扫描保留真实对象键。Scene、点云帧分组和导出优先消费该路径；旧 Item 不回填元数据，继续兼容原键。托管根下 `_objects/` 是平台写入保留区，普通 UUID 场景目录不作为上传 UUID 解析。
- 新增 dataset_assets、dataset_asset_locations、storage_locations 和 storage_bindings。Asset 通过唯一 dataset_item_id 反向关联样本，回填不用更新 DatasetItem；当前每个样本独立内容身份，不按摘要跨数据集合并。
- 历史映射复制实际 file_path，不推算旧名称前缀，不搬字节、不新建任务、不改 Scene/项目关系或冻结 JSON。历史 content_hash 只存 legacy_hash_hint，校验和与位置状态均未核验。
- 新写入同时计算兼容 MD5 与可信 SHA-256。旧 hash 仅筛选去重候选，实际读字节并匹配后才核验 Asset；无法读取的旧候选不会阻断导入可用新副本。ETag 单独记录。直传完成在线程中分块校验，使用 HEAD 的 IfMatch/VersionId 固定 GET 身份，并核对长度，沿用 5 GiB 直传边界。
- 已核验 Asset 的样本归属、类型、大小与摘要由数据库触发器保护，不能降级或替换。现有读取仍以原 file_path 进行；统一 resolver 和可变外部原件的持续覆写保护由后继能力交付。
- 默认位置由现有部署配置固定 endpoint/SSL，凭据仍由 StorageService 持有。首次登记或显式回填初始化九用途的固定桶绑定；只读核对覆盖位置配置与全部九用途。没有切换绑定的 API，也不将尚未迁移的头像、附件或训练包按新绑定重解释。
- storage_scan_prefix 固定扫描根：升级保留当时名称前缀，新建使用 UUID 根；后续改名不改变扫描范围。实际旧键不受扫描根推导影响。升级前已改名、尚未登记的旧根需要独立确认，不能猜测拥有范围。
- Alembic 0194 只增不改旧列。应用回退保留迁移与映射；schema downgrade 仅允许未使用的表，已有内容/绑定则拒绝。兼容现有迁移可逆后缀验收，不增加第二个不可逆屏障。

## Consequences

正向：同名数据集和重复上传不会互相覆盖；管理回填不使业务快照漂移；未知历史 hash 不会伪装成已校验内容。

负向：直传完成增加一次实际字节读取，旧文件首次参与内容去重也可能增加读取；逻辑名称分配在单数据集内串行。本阶段没有目录原地注册、存储切换或全消费者 resolver；回填成功只表示映射完整，不能证明历史字节在线或一致。

## Alternatives Considered

只把名称换为 Dataset UUID 仍会覆盖同路径旧内容。直接按旧 hash 建可信 Asset 会延续 ETag 误判。回填 Item.asset_id 会更新现有 ORM 时间戳，必须额外绕开业务快照；反向关系能减少该风险。

## Notes

- 模型：`apps/api/app/db/models/dataset_asset.py`；唯一 I/O owner 继续是 `apps/api/app/services/storage.py`。
- 实现：`apps/api/app/services/dataset_assets.py`、`dataset_storage_keys.py`；运维命令 `apps/api/scripts/backfill_dataset_assets.py`。
- 并发插入复用 [SQLAlchemy PostgreSQL ON CONFLICT](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)；管理映射与内容核验在同一数据库事务提交。

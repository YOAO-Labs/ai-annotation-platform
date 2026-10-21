# 0153 — 原事务持有的健康 Native 数据能力

- Status: Accepted
- Date: 2026-10-21
- Deciders: AAP maintainers
- Supersedes: None; follows ADR-0150 through ADR-0152

## Context

健康策略与外部观察已由 Inference 包持有，但 API 数据适配器仍直接查询 Registry、GPU membership、Pool 和 Member，服务池对账依赖 ORM flush 写入。将这部分改为远端自行提交，会拆开调用方 Audit、能力漂移接受和 GPU 围栏的事务。立即写入服务池也会改变现有延迟 flush 与回滚语义。

## Decision

Native0277 在 `aap_inference_health_data_v1` 发布11项固定能力：policy、完整 Registry 行、membership 事实、观察时钟、资源锁、promotion barrier、后端健康写入、成员池定位、完整 Pool／Member 行以及能力对账 apply。行输出使用显式字段，不暴露私有表的 composite type。函数没有提交、回滚或连接所有权。

健康协调器仍是单一策略实现。初次读取保留完整 JSONB 物化与同一身份对象；HTTP 仍先于健康行锁。原数据库时钟之后的端点、GPU epoch 与观察时间围栏不变。后端写入只接受健康字段，元数据在原写入阶段转换为 JSONB，保留历史非有限 JSON 的失败阶段。

服务池仍先修改已加载对象。协调器记录变更前的 generation、fingerprint 与 member state；API 绑定在原 Session 的 `before_flush` 阶段调用闭合的 seed／disable Native apply，随后同步这些已应用属性的 ORM history，避免相同健康字段再通过私有表 UPDATE 写入。其他待提交对象、名称／权重等调用方修改仍由原 unit of work 处理；暂存之后的调用方编辑也会保留。回滚包括 savepoint 回滚会清除暂存，对象的 `populate_existing` 重读不会重放被丢弃的对账。

Native apply 先按 Registry → Pool → Member 锁序复核归属与旧版本，再应用这两种状态转换；不一致报40001。能力规范化与指纹仍由可信协调器计算，SQL 不再实现另一份策略。读取 Pool 先锁 Registry KEY SHARE，单独触发的对账同样遵循锁序。此版本检查不构成路由停流或 Redis 连续性证明。

`HealthDataPostgresData.bind(connection)` 是不依赖 API／ORM 的独立包实现，只借用已经处于事务中的 asyncpg 连接。绑定核验函数白名单和精确 Native policy；该实现不创建、提交、回滚或关闭连接。它完整解码 Registry／Pool JSONB 并保留 DTO 身份和延迟 apply；最终事务仍由调用者管理。

`inference_health_data_client` 是可信协调器的数据角色，只获得上述11函数，没有原表、序列、TEMP 或其他 owner 权限。观察角色与 subject origin 角色不增加健康写权限。API 的兼容绑定仍使用过渡角色和原 ORM unit of work，尚未移除整套 API 的宽表权限。没有新增公开 HTTP 健康写接口，也不接受来自公开请求的 healthy/capabilities 权威值。

## Consequences

健康数据访问可由独立安装包通过函数合同替换，同时保留原事务。绑定层负责 ORM 兼容，不能据此声称所有 Registry／Pool writers、路由／GPU、stats 和整个 Inference 已收口。跨主机的完整健康协调协议与全部真实消费者仍需后续实施；Go 继续只是实验。

固定函数增加数据库调用与授权维护，API 的暂存／flush 适配也有额外复杂度。函数授予可信健康协调器，不能当作任意外部调用方的健康授权接口。生产切换、真实 GPU、AMD64与生产容量不在本次本地资格内。

## Scoped local qualification

本地证据将在 `.worktree/inference-health-data-qualification.json` 记录。冻结观测只允许声明 Native wrapper 与 seed／disable 写宏的展开，不改写基线；源文件、安装包、镜像、数据库与清理结果分别记录，不把重复检查累计为新案例。

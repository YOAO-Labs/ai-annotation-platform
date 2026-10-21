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

`HealthDataPostgresData.bind(connection)` 是不依赖 API／ORM 的独立包实现，只借用已经处于事务中的 asyncpg 连接。绑定核验函数白名单和精确 Native policy；该实现不创建、提交、回滚或关闭连接。它完整解码 Registry／Pool JSONB 并保留 DTO 身份和延迟 apply；最终事务仍由调用者管理。绑定记录原事务 XID；每次 Native RPC 前核验该事务仍处于活动状态且 XID 相同。提交、回滚或换事务后拒绝复用，调用方必须建立新绑定。这会在独立 asyncpg 路径增加事务元数据查询，不改变 API 的 ORM 兼容路径。

`inference_health_data_client` 是可信协调器的数据角色，只获得上述11函数，没有原表、序列、TEMP 或其他 owner 权限。观察角色与 subject origin 角色不增加健康写权限。API 的兼容绑定仍使用过渡角色和原 ORM unit of work，尚未移除整套 API 的宽表权限。没有新增公开 HTTP 健康写接口，也不接受来自公开请求的 healthy/capabilities 权威值。

## Consequences

健康数据访问可由独立安装包通过函数合同替换，同时保留原事务。绑定层负责 ORM 兼容，不能据此声称所有 Registry／Pool writers、路由／GPU、stats 和整个 Inference 已收口。跨主机的完整健康协调协议与全部真实消费者仍需后续实施；Go 继续只是实验。

固定函数增加数据库调用与授权维护，API 的暂存／flush 适配也有额外复杂度。函数授予可信健康协调器，不能当作任意外部调用方的健康授权接口。生产切换、真实 GPU、AMD64与生产容量不在本次本地资格内。

## Scoped local qualification

本地证据见 `.worktree/inference-health-data-qualification.json`。冻结观测只允许声明 Native wrapper 与 seed／disable 写宏的展开，不改写基线；源文件、安装包、镜像、数据库与清理结果分别记录，不把重复检查累计为新案例。

153项去重选定检查通过，覆盖旧健康／worker、拓扑并发、三观察绑定、完整行读取与同一 ORM 身份、延迟 flush、只 flush 其他对象时不提前写入健康状态、调用方后续修改、savepoint 回滚、函数专用 LOGIN、完整迁移、非空恢复和实际容器链路。它们并非全部数据库或真实设备案例。三种观察绑定仍各匹配24组冻结观测；六个基线文件保持原字节。独立端口还验证了提交／回滚后拒绝旧绑定，以及新事务必须重新绑定，避免自动提交与旧暂存状态跨事务复用。

Native277完整迁移的五阶段通过。非空转储恢复保留观察密钥和16项函数（观察5项、数据11项）的定义、owner、ACL及配置，恢复后实际健康观察和原调用方提交可用。源码213项通过；独立非 editable Inference／Workspace 分别213／530项通过，56／278个包文件与当前源码一致。最终三镜像另行核对这些文件，API核对1,253项输入、OpenAPI和 Native head277。

受限观察容器已通过公开 API 的真实写回、重启与停止后503／不回退。有限并发样本为16次、并发4，无 OOM；API、数据库和后端在该容器资源预算之外，不作为生产 SLO。初次镜像与安装验收对应加入事务身份围栏之前的实现，保留为历史证据；最终资格只引用围栏后的工件。

失败尝试保留：底层 owner 初始缺少全行读取／行锁权限；API 暂存方法初始未按异步 port 签名实现；独立包测试曾同时断言刷新前后的同一字段；旧 Registry／Pool 回放首次未提供固定 SHA。对应授权、适配器或测试驱动均已修正并验证，不把失败父回执改写为通过，也不累计重复案例。三个已整合的修改子工作树在核对提交等价、干净状态和仅有 Ruff／Python 缓存后移除，源分支保留。

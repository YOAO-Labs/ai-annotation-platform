# Changelog

本文件记录 Python SDK、随包 CLI 与可选 TUI 的重要变更，格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- SDK 与 CLI 补齐最后 44 项任务上下文、图像金字塔、跨帧作业、租约、相机标定、Scene track 和几何操作；全部 558 项稳定公开 REST（含运维）均有具体入口。另增加六条可选 WebSocket typed 事件流、离线协议描述与有界 NDJSON 监听。

- SDK 与 CLI 收录讨论／历史／缺陷报告、Mask 格式／QC／修复及点云质量共 51 项 API，保留签名附件隔离、计划回执、版本摘要与部分结果。

- 收录项目／批次／数据运维、绩效、搜索、指引资源、看板和预测／标注导入 52 项 API，扩展现有资源方法并提供 CLI、typed models 与离线约束。

- 收录账户／认证、用户／分组／邀请、健康、系统设置、审计、连接器与 ML 运维共 106 项公开操作，提供 typed models、CLI 与离线操作约束。

- 增加任务文件分步上传／签名 URL／缩略图回填及数据集场景读取／轨迹／更新，提供 CLI、类型模型和上传内容的独立流式步骤。

- 收录视频媒体／章节、分段协作、追踪创建／恢复／决策、Mask 内容／关键帧、质量审阅、轨迹编辑、项目模板和存储诊断／重试，提供 typed models、CLI 与离线约束说明。

- 新增 Data Manager 11 项与任务视图／任务查询 8 项 API，提供对应 typed models、CLI 和离线操作描述；支持 schema 驱动查询、分配预览／应用与明确选中任务的幂等导出。
- 收录通知中心 8 项、反馈线程与处理 4 项、编排库及预标注 7 项 API；增加对应 CLI 以及现有 Job 的直接读取命令，操作描述随离线目录分发。
- 预标注明确区分编排复制与推理受理，支持调用方保存的幂等键；通知偏好和反馈补丁保留字段省略语义，变更继续受当前账号／项目权限与确认保护。
- 增加离线 `capabilities` / `describe` 及 Python 能力发现函数，随包分发完整 API 台账、请求／响应 schema 和 agent 操作说明，明确区分已覆盖、主动排除和尚未接入。
- CLI 增加可选结构化 JSON 错误和 stdin JSON 请求输入；默认文本错误、成功输出与退出码保持兼容。
- 项目与批次导出支持 JSON 范围／点云请求体，并提供同步 VOC 流式下载入口。

- Python SDK 与 `aap learning` 覆盖冻结训练数据与任务版本流、实验与观测、配方和预览、逐样本评估、受控模型回流／发布、学习策略、有预算的参数搜索及人工主动学习。请求保留预检、幂等 ID 和版本冲突保护，执行器内部协议继续排除。
- TUI 增加按需加载的 Training 队列、项目／状态筛选、分页及执行详情，提供有界日志、遥测和资格／恢复说明。
- 现有项目、数据集、任务、标注和 Job 列表补齐可选筛选；反馈列表支持根反馈筛选与可见范围计数。

### Changed

- 公开接口覆盖路线包含管理员与运维操作，旧 Workbench／高权限排除项改为待实现，仅保留执行器私有训练协议排除。

- AAP 契约验证基线更新为 `0.26.1`，SDK 发布版本保持独立；平台后续升级本身不会要求同步发版。

- 批次重置与失败 Job 重试统一要求交互确认或 `--yes`；ML Backend token 改为隐藏输入或从指定环境变量读取。

### Fixed

- 无来源 tracker 事件保留空 annotation_id，兼容旧字符串空值；sdist 补齐测试支撑文件。

- 事件流异常和 socket 日志隐藏 token，退出／取消释放连接；连接不隐式重试或刷新凭据，CLI 断线保留已输出事件。复用既有预测／RLE 类型并保持核心安装无 WebSocket 依赖。

- 附件下载验证 302 和完整存储响应，签名错误不暴露凭据；超限截图在请求前拒绝。缺陷读取兼容扩展状态，结构化错误保留 reason 和有效 run/job 身份。

- 项目与批次 LiDAR 预检共用请求／响应模型；标注导入部分失败输出完整回执后退出 1，预测清理请求拒绝未知字段，指引资源显式声明支持的 MIME 并隐藏默认 repr 中的签名 URL。

- API key typed patch 保留 null 清除有效期；批量分组要求明确目标或 null，apply 部分失败输出回执后退出 1。邀请与临时凭据隐藏默认 repr，文本敏感输出要求明确选择。
- 根健康请求使用真实 `/health*` 路径，503 保留 typed report；离线描述补齐内部构造的请求模型和 JSON map schema。

- 预签名上传不再把重定向当作成功；完整下载拒绝 206／3xx，失败继续保留已有目标文件。

- 模板／源项目创建不再发送伪造的默认类型标签，显式传递来源 UUID 和指引复制选项；继承由服务器执行。

- 兼容较旧 Pydantic 的合法模型字段，避免导入警告混入 CLI 机器错误输出。
- 修正将视频导出范围和点云选项误作为 query 参数的能力缺口；同步 VOC 不再被误解析成异步 JSON Job，错误或中断保留已有下载文件。

- 流式下载在校验可用 SHA-256／大小后原子替换目标，HTTP 错误、中断或校验失败保留已有文件，预签名下载继续隔离平台凭据。
- OpenAPI 能力台账纳入现有看板、API key、当前主体与预测导入调用，二进制学习资源也通过真实下载调用点核对。

- 核心包未安装 CLI extras 时，`aap` 现在给出明确安装提示，不再输出依赖缺失 traceback。
- TUI Job 轮询在慢请求期间合并后续刷新，避免后台 HTTP 线程重叠。

## [0.20.0] - 2026-08-14

### Added

- TUI 增加 Projects/Datasets/Jobs/ML 筛选、Datasets/Jobs 分页、Dataset/Batch/项目 Pool 详情和 failed Job 重试。
- 绩效视图向所有角色展示本人摘要，super-admin 继续获得全员排行。

### Changed

- TUI 启动只加载 Projects、Jobs 和当前主体；其他视图首次访问时加载，错误按视图隔离并保留最近成功数据。
- ML Backend 总览移除 5 秒 N+1 轮询；单 Backend 详情继续使用实时 WebSocket。
- Textual 支持范围收紧为 8.x，并补齐 80 列终端、worker、筛选、分页与 WebSocket 重连测试。

## [0.19.0] - 2026-08-14

### Added

- 项目作用域增加 registry backend 和 service pool 可用清单、启停与健康检查。
- 增加 super-admin 全局 registry instance CRUD、健康检查与安全卸载命名空间。
- 增加 service pool CRUD、成员权重/drain/resume、能力指纹漂移确认、拓扑和 runtime snapshot。

### Changed

- CLI 明确区分项目 enablement、物理 registry instance 和逻辑 service pool ID；卸载、删除、drain 与漂移接受使用统一确认守卫。
- OpenAPI 能力台账已无计划覆盖项，所有已选定能力要么由 SDK 覆盖，要么有显式排除原因。

## [0.18.0] - 2026-08-14

### Added

- 增加批次流转、分配、批量处理和导出 SDK / CLI 能力。
- 增加任务提交、跳过、撤回、重开、退回重做和审核闭环 SDK / CLI 能力。
- 增加标注批量更新与 async job 失败项重试。

### Changed

- 批量批次命令在部分失败时先输出完整结果，再以退出码 1 结束。
- OpenAPI 能力台账将生产工作流端点从计划覆盖提升为已覆盖。

## [0.17.0] - 2026-08-14

### Added

- Projects、datasets、dataset items、dataset-project links、batches 与 project members 增加资源管理写接口。
- CLI 增加对应的更新、删除、成员管理、批次管理和数据集解绑命令；破坏性操作统一支持交互确认与 `--yes`。

### Changed

- AAP target 跟进当前主仓库 OpenAPI 基线。
- OpenAPI 能力台账将资源管理端点从计划覆盖提升为已覆盖。

## [0.16.0] - 2026-08-14

### Added

- 增加 `__aap_target_version__`，用于查询当前 SDK release 完成 OpenAPI 对账与测试的 AAP 基线。
- 增加 OpenAPI 能力台账，对 SDK 关注领域的端点标记为已覆盖、计划覆盖或明确排除。

### Changed

- SDK、CLI 与 TUI 使用独立于 AAP 的 SemVer；`aap --version` 同时展示 SDK 版本和 AAP target。
- 包元数据与运行时版本改为读取同一个静态来源，消除安装版本与 `__version__` 漂移。
- OpenAPI contract test 从 `client.py` 的真实 HTTP 调用点自动提取契约，不再维护容易漏项的端点元组。

# Changelog

本文件记录 Python SDK、随包 CLI 与可选 TUI 的重要变更，格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- Python SDK 与 `aap learning` 覆盖冻结训练数据与任务版本流、实验与观测、配方和预览、逐样本评估、受控模型回流／发布、学习策略、有预算的参数搜索及人工主动学习。请求保留预检、幂等 ID 和版本冲突保护，执行器内部协议继续排除。
- TUI 增加按需加载的 Training 队列、项目／状态筛选、分页及执行详情，提供有界日志、遥测和资格／恢复说明。
- 现有项目、数据集、任务、标注和 Job 列表补齐可选筛选；反馈列表支持根反馈筛选与可见范围计数。

### Changed

- AAP 契约验证基线更新为 `0.26.1`，SDK 发布版本保持独立；平台后续升级本身不会要求同步发版。

- 批次重置与失败 Job 重试统一要求交互确认或 `--yes`；ML Backend token 改为隐藏输入或从指定环境变量读取。

### Fixed

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

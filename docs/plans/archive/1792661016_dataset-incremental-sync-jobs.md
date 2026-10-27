# EPIC-04：增量同步与后台任务编排

> 状态：完整 E04 已完成本地回归与真实浏览器验收，归档；人工验收环境继续保留。未推送、未发布、未分配版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](../backlog/README.md#promotion-gate)。[草案总览](../backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](../backlog/1792661016_dataset-storage-architecture.md)和[交付验收](../backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 项目          | 内容                                                     |
| ------------- | -------------------------------------------------------- |
| 状态          | 完整 E04 已验收并归档         |
| 日期          | 2026-10-23（Asia/Shanghai）                              |
| 优先级 / 阶段 | P0：M1 幂等手动扫描与变化保护；P1：M2 周期同步与裁决     |
| 负责角色      | 后端、任务系统维护者、前端、测试                         |
| 最小依赖      | E01 身份模型、E02 位置契约、E03 来源配置                 |
| 协作          | E05 校验与访问；E08 变化列表；E11 删除规则；E12 任务指标 |

## 本轮实施定案：E04-A

基线 `b73fd4a7e`，工作树干净。E01-A/E02-A 已完成；当前 checkout 的 dev/test 服务已停止，不刷新其他 checkout。沿用受控只读根和指定 worker；无新凭据、依赖、环境变量、服务或镜像构建。

最小方案是 worker 内存递归全扫后一次提交，但中断丢失全部进度且无法拒绝旧执行者。本轮采用来源配置 + 持久目录队列 + 每目录原子批提交。复用 PostgreSQL 行锁、现有 AsyncJob 和 Celery worker_direct；消息确认不是数据库幂等保证。

### 合同和边界

- 新 DatasetSource 仅绑定已有数据集、filesystem 位置、相对目录、固定 Celery hostname、递归及过滤规则。此阶段 mode 为 inventory，不是可标注原地引用；不创建 Item/Asset/Task，不修改历史 missing。路径、规则、节点创建后不可重定向；名称/启停带 expected_revision，活动扫描时须先取消。
- 超管 API：`/dataset-sources` 创建/按数据集列表/详情/元数据修改；`/{id}/scans` 启动或复用活动扫描、列表；`/dataset-scans/{id}` 详情、`/entries` 分页、`/resume` 恢复。任务取消复用 `/async-jobs/{id}/cancel`，扫描详情保留当前 job_id。所有读取、取消、恢复及 worker 批提交复检当前超管身份；通用任务列表也不得向降权账号泄漏目录观测。
- 三表：DatasetSource、DatasetScanRun、DatasetScanEntry。Run 固定 source/location 修订、节点、根 anchor、规则和预算快照。Entry 以 run_id + 路径 SHA256 唯一，路径为非索引 TEXT，避免长中文路径超出索引上限；目录行承担 pending/captured 队列及目录身份/摘要。文件 stat 仅是观测，content_verified 恒 false。
- 数据集删除把 source.dataset_id 置空，保留扫描账本；位置被 Source 引用时不得删除/改向。终态 AsyncJob 清理用 SET NULL，不连带删除 Run/Entry。0196 降级在账本已有数据时拒绝破坏性清理。
- 单目录检查最多5000原始条目、循环预算2秒；每运行最多50000条观测、5000目录、相对路径1000字符/32层。过滤项、链接、特殊类型也记账和计入原始预算；只遍历普通目录，不跟随链接/子挂载，不读取文件字节。固定阈值进入 Run 快照，不借用复制导入的传输字节限额。
- 检查点是一个完整目录：列举后在同一事务分块写入观测/子目录队列/目录完成标记，事务失败不前进。捕获前后核对目录身份及 mtime/ctime；恢复和收尾重算已捕获目录摘要。目录变化、权限失败、根替换、预算、超时、取消均不完整。活跃文件系统不是全树原子快照，完成不意味着可判 missing。
- 一个来源仅一个 pending/running Run，数据库部分唯一索引 + 短行锁序列化启动。锁顺序 Location→Source→Job→Run，不跨文件系统 I/O 持锁。每次 claim 使用 source 单调 generation、随机 token 和60秒租约；每批落库检查 task_id/generation/token/有效期/修订/授权。一次 job 仅 claim 一次；失租后通过显式 resume 创建新 job/task_id，旧消息/信号不能结束新尝试。
- 源根目录首次在固定 worker 固定身份，此后不静默换目录。启动/恢复/claim 需要当前指定节点的 list 已验证证据；运行中固定配置/anchor，每批复核启停及修订。一次 Celery 尝试软限240秒、硬限300秒，阻塞内核 I/O仍有平台限制；失租可显式接管，不新增 beat 调度器。发布失败保留可恢复任务，不虚报启动成功。
- AsyncJob 记录阶段与累计数，未知总量不展示百分比；补现有铃铛/详情的该 kind 展示、取消与通知文案，不增加管理页面。扫描中断由 fenced signal owner 处理，不走通用无租约状态转换。

### 文件与流程

本轮超过8文件：`db/models/dataset_source.py`/模型注册、`alembic/versions/0196_dataset_scan_ledger.py`、`services/filesystem_storage.py`/`dataset_scans.py`/`storage_location.py`、`schemas/dataset_source.py`、`api/v1/dataset_sources.py`/router/async_jobs、worker/celery/signals、AsyncJob model/notify、Web asyncJobs/presentation/JobsBell/详情、对应测试、API快照/SDK清单、正式API/用户/开发文档、ADR0093、CHANGELOG及计划导航。

```text
超管 API -> Source / ScanRun -> 指定 Celery hostname
              ^                       |
              |  短事务 fence         v
              +--- 观测/目录队列 <- openat2 只读目录快照
              |
              +--- AsyncJob / 现有任务中心（取消、阶段、累计数）
```

### 验证和回退

在隔离 test 数据库/Redis/临时根验证：重复启动/投递、真实指定节点、离线、逐目录恢复、批事务回滚、失租接管和旧提交/旧失败信号、取消及权限撤销、旧修订、根/子目录替换、目录变化/超限、长中文路径和特殊文件。证明不完整和正常扫描均不创建业务样本/任务，也不修改历史Asset状态；验证AsyncJob清理保留账本。运行旧存储/导入/job回归、0196迁移往返、OpenAPI/SDK、Web展示测试/类型检查、文档构建和diff检查。结束销毁本轮资源并清理产物。

回退仅关闭新入口、停止/取消本轮任务并保留来源和观测账本；不删除源目录、不回退已保存历史，不将相对路径交给旧 S3 拼接器。本轮不开放周期同步、missing裁决、内容hash、原图HTTP读取或数据集UI创建向导。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                     | 已有能力、缺口与复用约束                                                                                                                   |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| [dataset_import.py](../../../apps/api/app/workers/dataset_import.py)         | 现有逐文件提交、文件间取消、最多 100 条 JSON 错误摘要；先收集限额内条目，没有持久游标、单项账本或扫描租约，枚举/大文件传输中取消仍需新增。 |
| [dataset.py](../../../apps/api/app/services/dataset.py)                      | 建任务用 NOT EXISTS 等顺序去重，项目/样本对无数据库唯一约束，不等于并发幂等。\_unique_file_name 逐项查文件名全集可批量优化。               |
| [async_jobs_cleanup.py](../../../apps/api/app/workers/async_jobs_cleanup.py) | 终态 job 有清理期限，长期扫描/变化审计不能只寄存在 job JSON；仍沿用同一任务中心。                                                          |

候选修改入口：[dataset_import.py](../../../apps/api/app/workers/dataset_import.py)、[dataset.py](../../../apps/api/app/services/dataset.py)、[async_job.py](../../../apps/api/app/services/async_job.py)、[async_job.py](../../../apps/api/app/db/models/async_job.py)、[async_jobs.py](../../../apps/api/app/api/v1/async_jobs.py)、[AsyncJobDetailModal.tsx](../../../apps/web/src/components/jobs/AsyncJobDetailModal.tsx)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与现有基础

原地目录会不断增加、改名、覆盖或丢失文件。“首次可登记”之后，还需要一条可以解释和恢复的同步链路。最危险的情况不是同步稍慢，而是一次网络故障被当成全部删除，或同名新图片直接继承旧图片的标注。

现有系统已有 Celery 导入任务、AsyncJob 历史、阶段、取消、错误和跳转。继续扩展这些能力，并为来源扫描增加逐条结果与幂等状态，不另建一套任务中心。[代码依据：导入 worker](../../../apps/api/app/workers/dataset_import.py)、[JobsBell](../../../apps/web/src/components/shell/JobsBell.tsx)、[任务详情](../../../apps/web/src/components/jobs/AsyncJobDetailModal.tsx)

## 2. 目标与范围

- 手动重扫、可配置周期扫描，以及后续可选文件事件提示。
- 新增、未变、内容变化、疑似移动、缺失、不可读的明确分类。
- 逐批检查点、单文件重试、任务取消和来源级互斥。
- 支持“新文件自动加入”策略；内容替换、删除等高影响动作默认人工确认。
- 保留已关联项目的任务幂等性；不同来源的重试不能导致重复任务。
- 对源目录、标注、训练冻结版本之间的变化关系提供审计。

本 Epic 不实现质量评分算法、存储复制容灾或跨站点文件系统一致性。源事件只能提示“需要核对”，不能替代周期完整核对。

## 3. 用户故事

| 编号    | 用户故事                               | 结果                                             |
| ------- | -------------------------------------- | ------------------------------------------------ |
| E04-S01 | 数据管理员点击重新扫描，只登记新增图片 | 不变条目跳过，新文件按配置加入                   |
| E04-S02 | 用户按小时或每天同步采集目录           | 每个来源有独立计划、时区和最近执行结果           |
| E04-S03 | 用户在同名图片变动时先看到差异         | 不自动覆盖旧样本与旧标注                         |
| E04-S04 | 用户遇到连接中断后继续扫描             | 从检查点恢复，缺失判定保持保守                   |
| E04-S05 | 用户只重试损坏或权限恢复后的失败条目   | 无需重跑整个目录                                 |
| E04-S06 | 项目负责人只为新增有效样本生成任务     | 重扫、重试和重复回调不生成重复任务               |
| E04-S07 | 用户看得懂进度与实际完成范围           | 未知总量时显示已扫描数量，不展示虚假的精确百分比 |

## 4. 扫描生命周期

建议一次 `ScanRun` 记录来源配置版本、扫描范围、触发方式、开始/结束时间、检查点、扫描代次、状态与结果计数，并关联现有 AsyncJob ID。

1. **预检**：验证来源可达性、权限、扫描范围与配置版本。
2. **枚举**：分页记录观测条目；已扫描数量持续增长。
3. **比对**：按来源条目标识和观测信息筛选可能变化项。
4. **核验**：对需要的条目读取内容、校验摘要、提取元信息。
5. **应用**：按策略新增样本、登记变化候选、更新位置状态。
6. **核对**：仅在完整扫描成功后，评估本次未见的历史条目。
7. **完成**：汇总已应用、待处理、失败、跳过；写入最终报告。

每个来源同一有效配置只运行一个写入型扫描；重复提交返回现有任务或进入等待队列。任务锁需要租约与失效恢复，并通过运行代次阻止旧 worker 在接管后继续提交结果。

## 5. 变化处理规则

| 观测结果                     | 默认行为                                      | 对已有标注/版本的影响          |
| ---------------------------- | --------------------------------------------- | ------------------------------ |
| 新路径、新内容               | 按新文件策略新增样本                          | 关联项目可创建新任务           |
| 同路径、已验证内容一致       | 刷新最近检查时间                              | 无业务变化                     |
| 同路径、内容不同             | 原引用标记 changed，建立待处理候选            | 旧样本固定；禁止新字节配旧标注 |
| 老路径消失、新路径摘要相同   | 提示疑似改名/移动；确认后新增位置并退役旧位置 | 可保留原样本和标注身份         |
| 完整扫描未见条目             | 标记 missing 候选，必要时二次 stat/稳定等待   | 不自动删样本、标注或冻结版本   |
| 目录离线、分页失败、任务取消 | 记录扫描不完整                                | 不依据未见条目批量判 missing   |
| 权限收回或单文件拒绝访问     | 标记 unreadable 原因                          | 不等价于不存在                 |
| 修改过滤规则                 | 先预览影响，旧条目标记规则外候选              | 不自动删除历史业务数据         |

### 对内容变化的裁决

用户可忽略本次候选、将新内容作为新样本加入，或经专门修订流程确认替换关系。默认方案使用新 DatasetItem，记录 `supersedes` 等业务关系；旧任务继续绑定旧 Asset。旧字节已经不可得时，旧任务明确不可用，不能把新图当作旧图展示。

继承旧标注不是默认行为。若未来提供“复制标注到新修订”，必须单独确认图像尺寸、坐标语义和复核状态，且记录来源；不纳入 M1。

## 6. 幂等、恢复与规模策略

- 登记幂等键至少覆盖来源 ID、稳定条目标识、目标内容修订和操作类型。
- 数据库约束与事务保证去重；不能仅依靠 worker 内存中的“处理过”集合。
- 样本登记与任务创建采用可重放事件或同等可靠方案；消费侧再次去重。
- 事务按批提交，检查点只在该批持久化后前进。
- 规则改变、根路径改变或来源代次改变时，旧检查点不得盲目复用。
- 对临时网络错误做有上限退避；权限错误、内容冲突等不无限重试。
- 文件错误明细分页存储；日志不把整份失败清单写进单个 JSON 字段。
- 扫描既有文件名时使用索引/批量查询，避免每处理一项都读一遍整个数据集文件名。

现有 `_unique_file_name` 对逐项查询模式是优化切入点；实际成本需基准测量，不以静态代码推断精确耗时。[代码依据：数据集服务](../../../apps/api/app/services/dataset.py)

## 7. 数据与 API 草案

| 资源 / 操作                                 | 建议内容                                                                     |
| ------------------------------------------- | ---------------------------------------------------------------------------- |
| ScanRun                                     | source_id、config_revision、generation、cursor、completeness、counts、job_id |
| SourceEntryObservation                      | 路径、大小、修改时间、对象版本、观测 fingerprint、观测结果                   |
| SourceChange                                | before_asset_id、候选内容、变化类别、裁决状态、执行者、理由                  |
| `POST /dataset-sources/{id}/scans`          | 手动触发，返回现有或新 job_id                                                |
| `GET /dataset-sources/{id}/changes`         | 按类型/状态分页                                                              |
| `POST /dataset-source-changes/{id}/resolve` | 明确裁决与幂等键；校验候选仍对应当前内容                                     |
| `POST /jobs/{id}/retry-items`               | 仅重试选定失败项，或复用现有任务 API 的同等扩展                              |

具体 URI 需与现有 API 规范对齐，表中不是已实现接口。周期任务只保存计划，不在 Web 请求中等待目录扫描。

## 8. UI 与任务入口

数据集详情的“来源与同步”显示最后完整扫描、正在执行任务、待处理变化和错误摘要。点击进入变化列表或现有任务详情；全局后台任务仍是统一历史入口。

存储位置页仅显示该位置相关任务和受影响来源数量，不复制一份数据集样本变化工作台。用户从位置故障进入某个数据集时带上来源筛选上下文。

取消按钮说明“已完成条目会保留”。失败状态区分整体失败、部分完成、等待用户处理；待处理变化不是系统故障。

## 9. 验收标准

| 编号     | 可执行验收                                                                                      |
| -------- | ----------------------------------------------------------------------------------------------- |
| E04-AC01 | 同一目录连续完整扫描三次，未变化样本数与任务数不增长，只更新观测时间                            |
| E04-AC02 | 新增图片后手动重扫，仅新增有效图片；同一任务重投递不产生重复样本或任务                          |
| E04-AC03 | 扫描完成前断开目录连接，未见历史文件不被批量标记删除，任务标记为不完整且可恢复                  |
| E04-AC04 | 原路径替换为不同内容，已有任务仍指向旧 Asset；接受变化创建新修订/样本，旧标注不自动迁移         |
| E04-AC05 | 把图片移动到新目录，摘要核验后可显式重定位并保留样本身份；仅名称相似时不能自动认定同一文件      |
| E04-AC06 | 并发点击重扫和调度器同时触发，同一来源只有一个有效写入代次，失效 worker 的结果被拒绝            |
| E04-AC07 | 取消后继续执行、worker 中途重启、数据库事务重试均不跳过已确认未提交的条目，不重复提交已完成条目 |
| E04-AC08 | 对部分失败项重试只处理所选项；源配置已变更时要求重新核对范围，不复用失效检查点                  |
| E04-AC09 | 原文件消失只标记可见异常；已冻结训练版本和历史标注未被级联删除                                  |
| E04-AC10 | 未知总量扫描显示已处理数与阶段；完整扫描结束时各结果计数可对账，错误明细支持分页                |

## 10. 测试计划

- 并发与幂等：重复消息、租约过期、旧任务迟到、两次调度重叠、幂等键冲突。
- 文件变化：同大小不同内容、同名覆盖、只改时间、移动、大小写改名、部分写入、快速删建。
- 完整性：分页中断、过滤规则变化、枚举漏页模拟、权限收回、目录重挂到不同数据源。
- 业务联动：多项目关联、已取消项目、标注中样本、冻结版本引用。
- 规模：观测记录增长、查询索引、单来源限流、公平调度；具体阈值经 M0 校准。

## 11. 迁移、回滚与待决策

已有一次性导入继续可用，新同步配置默认关闭。先开放手动重扫，再开放周期计划。回滚时暂停计划、等待或取消运行任务，保留 ScanRun 和变化审计；不回退已经确认的业务修订。

待决策包括自动加入新文件的默认值、缺失候选等待时间、扫描计划数量限制和任务明细保留时长。建议新来源默认手动，启用自动同步时显式选择新文件策略。

## 12. 工程任务与完成定义

- [x] 实现 ScanRun、条目结果和变化候选模型。
- [x] 复用 AsyncJob 阶段、取消、历史与错误展示。
- [x] 实现来源级租约、运行代次、幂等登记和任务创建去重。
- [x] 实现完整扫描核对与保守缺失规则。
- [x] 实现单项重试、周期计划和变化裁决界面。
- [x] 通过故障注入与已有标注/冻结版本不变性测试。

完成定义：目录持续变化时，系统能说明新增了什么、哪里不可用、哪些变化尚待确认；重试和故障不会悄悄改变样本内容或销毁业务历史。

## 本项目推进切片与晋升条件

后端固定观测账本、租约代次及事务边界，测试负责人列重复投递/接管/取消/部分枚举故障注入。新来源默认手动，周期启动需显式新文件接纳策略。

| 切片  | 独立结果             | 交付范围                                                                   | 验收 / 回退边界                                                |
| ----- | -------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------- |
| E04-A | 可恢复手动扫描基座   | 来源合同、条目账本、批提交游标、来源租约/代次和并发去重先于 E03 入口完成。 | 旧 worker 迟到拒绝；不完整枚举不判 missing；旧复制导入仍可用。 |
| E04-B | 重扫、周期与变化裁决 | E03 接入后开放手动重扫、单项重试、周期计划及覆盖/移动差异确认。            | 无变重扫不增样本/任务；新修订不继承旧标注；可停止调度回退。    |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_dataset_import.py](../../../apps/api/tests/test_dataset_import.py)、[test_dataset_link.py](../../../apps/api/tests/test_dataset_link.py)、[test_task_dataset_link.py](../../../apps/api/tests/test_task_dataset_link.py)、[AsyncJobDetailModal.test.tsx](../../../apps/web/src/components/jobs/AsyncJobDetailModal.test.tsx)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](../backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-connections.md](../../../docs-site/dev/concepts/storage-connections.md)、[index.md](../../../docs-site/user-guide/datasets/index.md)、[debug-celery.md](../../../docs-site/dev/how-to/debug-celery.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](../README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

- Landed changes: E04-A inventory-only DatasetSource, durable ScanRun/Entry ledger, bounded metadata enumeration, per-directory atomic checkpoints, explicit resume, generation/token/lease fencing, direct-worker routing and current-role checks. Included in the commit containing this outcome; no release milestone assigned.
- Task center: existing job list/detail, cancellation and notifications support directory scan phases and cumulative counts without invented percentages. No new dataset creation UI or in-place sample ingestion is enabled.
- User documentation: `docs-site/user-guide/datasets/index.md`. API documentation: `docs-site/api/guides/dataset-sources.md`, `docs-site/api/guides/async-jobs.md`, OpenAPI snapshot and SDK coverage manifest.
- Developer documentation: `docs-site/dev/concepts/storage-connections.md`, `docs-site/dev/concepts/audit-and-notifications.md`, `docs-site/dev/how-to/debug-celery.md`, `DEV.md` and `README.md`. ADR: `docs/adr/archive/0093-resumable-directory-inventory.md`. CHANGELOG: Unreleased entry added.
- Verification: focused service/API/filesystem coverage and a real direct Celery worker passed. Final service/API and concurrent retention/resume/start/stale-signal regression: 10 passed, including re-reading the current Job association after Source serialization. Coverage includes atomic rollback, cancellation, expired leases, old workers/signals, directory changes, role/config revocation, budgets, source retention and no business sample creation. The directory handle check was narrowed to task-owned handles to avoid unrelated socket collection affecting its baseline. Compatibility suite: 93 passed; Web presentation/components: 34 passed; SDK OpenAPI contract: 4 passed. Local TypeScript, CSS token lint, documentation build, ADR/frontmatter/navigation and rendered-document link checks passed. Repository-wide link scanning still reports 23 existing links in unchanged ROADMAP/research files; no live browser or remote CI acceptance is claimed.
- Migration: full fresh, reversible suffix, legacy-forward and dump/restore verification passed through 0196; populated ledgers reject downgrade. Validation databases were removed with no owned leftovers.
- Cleanup: task-owned worker and temporary fixtures were stopped/removed; isolated test database, Redis and buckets were destroyed, and generated documentation/test outputs were removed. No backend image build, real business mount or shared development runtime change.
- Remaining work: E04-B periodic synchronization, change classification/resolution, item retries and business ingestion remain gated. Next dependency slice is E05-A content-verified reads, followed by E03-A/E08-A/E09-A dataset and storage UI integration. Completed inventory is not a consistent filesystem snapshot, verified content or authority to mark historical assets missing. This Epic remains in the active root because its remaining contracts still constrain future work.

## 本轮实施定案：E04-B1 手动增量差异与仅新增接纳

基线 `951b5f1b8`，工作区干净；完整E03已归档。用户授权继续下一阶段，沿用提交和实际浏览器验收要求。已复读0200模型、扫描/登记/内容/授权/生命周期和S3/SFTP适配器；本轮不重建镜像、不改共享配置或业务挂载，无新依赖、环境变量或运行服务。E04-B拆成独立可用切片：B1提供手动差异及新增接纳；后续周期计划、同路径新修订与移动裁决继续保留，不宣称整个E04完成。

最小方案是每次打开列表时实时对比扫描和当前映射，但会随本轮登记改变基线且无法追溯。选择复用A2的两阶段预检、租约与worker，在预检开始的Source序列化事务中固定已接纳DatasetSourceItem基线，持久保存本轮差异；不新造任务状态机。新增迁移0201：RegistrationRun.comparison_complete和DatasetSourceChange(run_id,path_digest)，保存原路径/旧Run/旧Item/旧SHA及观测、新SHA、分类和原因。旧Run不伪造差异。每侧最多50000条，SQL批量初始化，内容逐项核验/提交；不在API进程攒整目录列表。

分类只依赖完整当前扫描与实际预检：新路径核验成功为added，旧路径SHA及观测相同为unchanged，SHA不同或观测改变为changed并保留旧绑定，单文件核验失败为unreadable；完整扫描未见历史路径只记not_seen（包含未遍历路径），存在但不匹配普通文件的记excluded。not_seen不是确认丢失，尤其S3多页LIST无全局快照、父路径阻断也可能未见。预检未完成/取消/失租不发布差异列表；单项修复重试重新关闭发布门，完成后发布。旧样本已固定的观测不能被“同SHA”隐式刷新。

扩展现有confirm为可选scope=all_safe|additions_only，无body保留原调用；仅新增确认固定选择，跳过已核验unchanged，将changed保留为阻断条目，不接纳新字节为旧样本。正式登记仍复用严格二次读取、Dataset锁和Source/path幂等；重投递、并发单项登记或预检后换图不能创建重复Task或覆盖旧内容。GET /dataset-registrations/{id}/changes按类别分页并返回完整计数/观测时间；实时沿用来源管理范围和API Key scopes。界面在既有预检区域显示差异与确认入口，不增加新管理页。

最脆弱前提是完整枚举能证明文件不存在；设计不依赖该前提，只提供未见候选且不改变Asset可用状态。S3固定旧版本可读不代表当前key存在。取消/权限撤回/源离线沿用既有屏障；新差异表不依赖短期AsyncJob。回退关闭新差异入口，保留0201账本；有差异记录时拒绝schema降级，不改外部文件或旧业务身份。

文件范围超过8个：`db/models/dataset_source.py`、`alembic/versions/0201_dataset_source_changes.py`、`services/dataset_source_changes.py`、`services/dataset_registration.py`、`schemas/dataset_source.py`、`api/v1/dataset_sources.py`、Web `api/datasetSources.ts`/`DirectoryRegistrationProgress.tsx`/新差异面板、对应回归与迁移测试、OpenAPI/SDK清单、README/CHANGELOG、用户导入指南、API来源指南、开发存储概念、ADR0100及本计划/路线进度。现有worker复用服务调用，不新建worker类型。

```text
Source + complete Scan -> A2 preflight / existing worker -> per-item SHA
             |                      |
             +-- frozen baseline -> SourceChange ledger -> paged UI
                                         |
                               confirm additions_only
                                         v
                             existing strict registration -> Item / Task
```

验证：本地venv和owned test/dev；分类/固定基线、同SHA观测变化、相同stat异字节、未见与链接/过滤、无基线扫描不误判、损坏修复单项重试、取消/旧lease/撤权、正式登记前换图、并发幂等、历史Job清理保留差异与迁移往返；回归FS/S3/SFTP/授权/生命周期。真实浏览器登记基线并重扫验证无新增，再修改/新增/移除/损坏夹具，重扫预检查看分类分页，仅确认新增，检查旧Item绑定和外部文件不变；旧Task与标注保护通过API/DB回归核验，不重复已完成的E03工作台验收。同步OpenAPI/客户端/SDK、Ruff/类型/ESLint/CSS token/文档构建/diff检查；测试后清理资源与产物，不推送或发布。

实现依据：复用[PostgreSQL事务与行锁语义](https://www.postgresql.org/docs/current/transaction-iso.html)及[SQLAlchemy PostgreSQL ON CONFLICT](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)。

## Outcome — E04-B1

- Landed changes: preflight-owned differences freeze accepted SourceItem baselines, publish only after complete preflight, retain full-content candidate hashes and six paginated categories. Confirmation can freeze additions_only; unchanged entries are retained, changed/unseen entries do not rebind or delete business objects. Retry cannot expand the accepted scope. Existing two-stage worker, fencing, authorization and strict registration remain the execution owners.
- Compatibility and migration: 0201 adds comparison evidence and a completion flag; old ledgers keep legacy confirmation. Empty-schema roundtrip passed; populated evidence refuses destructive downgrade. AsyncJob cleanup preserves differences. API snapshot, generated web client, route index and SDK exclusion manifest are synchronized; no release version assigned.
- Backend verification: 159 unique tests passed across the focused compatibility suite and follow-ups (FS/S3/SFTP, authorization, content, policies, registrations and migrations). Covered three unchanged rescans with stable task counts, accepted versus unaccepted baselines, same SHA/different observation, same size/mtime/different content, six categories, concurrent direct registration, cancellation/stale fence, repaired-error publication, post-confirm replacement rejection, frozen scope retries and retained annotations/tasks. The old 0196 migration test now includes the 0201 dependency. SDK OpenAPI contract: 4 passed.
- Browser verification: isolated current-checkout API/Web and both host workers served 24 reference images. Unchanged rescan reported 0 added/24 unchanged and disabled confirmation. Mutated fixture reported added=1, unchanged=21, changed=1, not_seen=1, excluded=1, unreadable=1. Type filtering and 20+6 pagination passed. Additions-only confirmation grew the dataset from 24 to 25 with only fresh.png added; all 24 original IDs/content locators were preserved. Changed original read returned409. Source bytes/links stayed unchanged by platform operations, original copy bytes were0, and no browser exceptions were observed. Existing annotation/task preservation was verified through API/DB tests, not repeated Workbench UI acceptance.
- Static checks: changed-file Ruff lint/format, TypeScript, ESLint, CSS token lint, OpenAPI check, docs build, ADR/frontmatter/navigation checks and git diff --check passed. CSS lint retains two existing warnings in unchanged TrainingComparisonResults.tsx. Independent read-only backend/frontend review found no remaining blockers. No remote CI or production deployment claimed.
- Documentation: README, CHANGELOG Unreleased, user import guide, source API guide, developer storage concepts, ADR0100, route index, SDK coverage and roadmap/backlog progress updated in this commit.
- Cleanup: isolated dev/test databases, Redis instances, buckets, temporary source/auth/browser fixtures and generated docs/test outputs removed. No backend image rebuild, shared configuration change, business mount mutation, push or release.
- Remaining scope: this delivers B1 only. Periodic schedule/actor authority, accepting changed content as a new revision, explicit move resolution, delete decisions and ledger retention/compaction remain separate work. Not-seen is a candidate, never deletion proof; a complete scan is not an atomic snapshot or proof of current online content. Keep the E04 plan active until its remaining contracts are delivered.

## 本轮实施定案：E04-B2 周期扫描与预检

基线 `c877234c2`，工作区干净。用户继续授权下一阶段及提交；复用现有 Celery beat、Source 锁、Scan/Registration 租约与差异，不新增依赖、服务、环境变量或镜像构建。固定间隔为15–10080分钟，默认60分钟，以UTC保存下次时间；不提供日历/cron或本地时区时间，避免引入夏令时裁决。

最小方案是在beat里直接遍历并创建扫描，但不能刷新指定节点的五分钟探测，也无法恢复提交与发布之间的中断。选择新增每来源一条持久周期计划，由beat每分钟有界唤醒，计划保存当前执行账号、Key ID/初始摘要证明、配置/根锚点、修订、当前扫描/预检和下次检查点；不保存JWT或明文Key，不冒用固定超管。调度派发令牌和90秒租约拒绝重复或过时唤醒；每轮最多100个来源，按下次检查时间公平前进。

周期流程仅自动扫描→预检→差异，不登记样本。实际协调任务固定路由该来源worker，真实刷新list证据，I/O后重查计划令牌、账号、位置授权、Key read/write scope、原凭据和来源/位置修订及根身份。后台预检显式使用worker入口，不把worker探测伪装成API探测；人工确认仍在真实API节点重新验证原图。计划与业务Run独立，暂停只停止后续协调/触发，当前任务继续按原权限执行；需要取消时使用原任务取消入口，不自动复活已取消任务。

遇手动active Scan或Registration保持等待，不接管、不创建重叠周期。当前周期pending派发可重放，失租更换原执行尝试，Scan完成后由持久指针补建预检，不依赖内存回调。正常无变化完成后按间隔安排下一轮，不补跑错过的周期。新增/变化/未见/排除/核验失败进入待核对，暂停后续扫描；人工登记完成或显式“跳过本轮差异，继续周期”后再安排下一轮。跳过仅确认已查看，不改变差异或业务样本。权限、凭据、配置或根资格改变阻断计划，须有权用户重新启用；暂态不可达按间隔重试，有界且不把未见推成删除。

新增迁移0202：SourceSyncSchedule按source_id唯一，配置CAS修订、私有授权快照、公开状态/时间/错误和历史Run指针。GET/PUT `/dataset-sources/{id}/sync-schedule`；POST `/{id}/sync-schedule/continue`带expected_revision和scan_id，防止跳过错轮。旧来源没有计划，保持手动。保留计划/Run证据，只有空计划表可降级；应用回退停止新beat任务，已有业务记录不回退。

文件超过8个：Source模型/0202迁移、新调度service/worker、原Registration后台入口、Celery include/routes/beat、Source schema/API、Web API/新计划面板/DirectoryReferencePanel、调度及迁移回归、OpenAPI/SDK清单/路由索引、README/CHANGELOG、用户导入/API/开发存储与部署说明、ADR0101和本计划/路线进度。无需重整现有任务中心。

```text
UI configure -> SourceSyncSchedule <- existing beat / bounded tick
                       |                     |
                 revision + token ---> exact source worker
                       |                     |
                       +--- durable Scan -> preflight -> frozen differences
                                               |
                                  manual confirm or acknowledge
```

验收：重复tick/重复消息/过期令牌、提交后未派发、手动冲突、探测过期刷新及错误节点、扫描完成后补预检、失租恢复、取消不复活、暂停竞态、CAS/错轮确认、撤权/Key轮换/配置或根变化、无变化连续周期、待确认阻断、新增人工接纳、任务历史清理和迁移往返。使用本地venv和owned test/dev资源，真实Celery执行与浏览器配置/暂停/继续/差异验证；运行相关旧回归、Ruff/TS/ESLint/CSS、API/SDK与文档检查。完成清理资源及中间产物并提交，不推送或分配发布版本。

最脆弱前提是某次触发只会投递一次；设计不依赖它，使用数据库短锁和持久令牌，业务Run仍通过自身task/generation/lease fencing。依据[Celery周期任务的单调度器与重叠说明](https://docs.celeryq.dev/en/main/userguide/periodic-tasks.html)，beat只唤醒，不承担业务幂等。

## Outcome — E04-B2

- Delivered per-source fixed-interval schedules (15–10080 minutes), revision-based updates, pause, explicit review-and-continue, and a bounded existing-beat tick routed to the exact worker. The schedule retains its current Scan/Registration pointers across publication gaps. Background work creates inventory and frozen preflight differences only; ingestion remains a human action.
- Preserved the original actor and API-key proof around I/O and nested domain calls. Duplicate/expired wakeups are fenced. Current authority/configuration/root changes block the schedule, transient failures retain checkpoints, cancelled runs stay cancelled, and manual work is never taken over. Formal registration retains the confirming user's authority and its existing recovery owner. Completed job cleanup cannot erase the periodic run owner. Reauthorization after a paused terminal cycle is possible while completed differences retain their review gate.
- Verification: 90 unique backend tests passed across the 88-test regression and two added reauthorization cases; the final B2 suite passed 28 tests, with the review-date correction checked separately. Coverage includes authorization/key rotation, scope/expiry, manual conflicts, publication recovery, lost leases, pause during I/O, review CAS, no-change repeated cycles, cancellation, retained job history, and migrations 0196/0202. Four SDK/OpenAPI contract tests passed. OpenAPI consistency, generated Web types, Ruff, TypeScript, changed-file ESLint, CSS token validation, documentation build, and diff checks passed locally. Remote CI was not run.
- Live acceptance: the owned dev environment ran the current API, general/maintenance workers, and a task-owned beat using the production 60-second source-sync entry. Exact worker task registration was verified. Browser operations enabled a 15-minute plan, observed automatic scanning/preflight and two added candidates with zero business items, paused and re-enabled without losing the review gate, then explicitly skipped this round: zero items and preserved differences. Subsequent human additions-only confirmation created exactly two items, with zero copied original bytes, zero preview bytes, and zero errors. The browser reported no page errors; console entries were development/restart notices. The final screen was inspected. No primary environment, shared configuration, image, or source file was modified by acceptance.
- Cleanup: stopped the task beat/browser and owned dev/test clients; destroyed this checkout's disposable databases, Redis instances, buckets and local mode data; removed the task auth-vault entry, temporary source images, logs, screenshots, reports and generated build/cache outputs. Retained worktree identity/lock files. The API snapshot and generated API route index are intended tracked artifacts.
- Remaining scope: changed-content acceptance as a new revision, explicit move/delete decisions, automatic ingestion policy, calendar/timezone scheduling and ledger retention/compaction remain separate work. This closes B2, not the whole E04 Epic; keep this plan active. No release version was assigned, no image was rebuilt, and no push or release was performed.

## 本轮实施定案：E04-B3 逐项接纳内容新修订

基线 `c6b80e026`。用户要求保留当前工作树的人工验收环境并继续实施、提交；dev 数据、原始图片与 systemd 用户服务保留，自动测试只访问独立 test 资源。不重建镜像，不修改共享环境配置。本轮只允许在已完成、差异完整的预检中，逐项接受 `changed/content_changed`；开始仅新增登记后需重新扫描，不能借普通重试扩大确认范围。移动、删除、同摘要身份变更及标注迁移继续保留草案。

现有 SourceItem 兼任路径当前绑定与历史，不能直接覆盖。本轮新增不可变 SourceRevision 账本、SourceItem.current_revision_id 和 Change.before_revision_id；迁移0203把已有绑定回填为修订1，但不虚构接纳人和接纳时间。新修订保存来源、路径、观测Run、原业务身份、前驱、确认预检及真实操作者。Item/Asset删除仅清空活动外键，保留原身份。旧引用保留原ScanEntry；引用唯一性改为来源/路径/观测Run，禁止同一候选重复登记。

单项 POST 固定预检generation、前驱revision ID和候选SHA；API当前权限、API Key proof、位置/来源/策略、完成状态、无活动扫描/登记均在I/O后和提交前复检。前驱ID防止内容A→B→A产生ABA误接纳。完整严格读取复用 register_entry，在Location→Source→Dataset锁内原子创建新Item/Asset、空白Task、修订账本、移动当前绑定及审计。托管复制跳过业务Item的SHA去重；存储摘要相同不能继承另一任务身份。重复同一确认返回原结果，不把后续当前绑定退回；另一预检已推进前驱则409要求重扫。历史条目删除后不复活原候选。

旧Item、Asset、Task和Annotation不改写，新样本显式记录前驱关系并在列表区分。来源影响统计与目录媒体所有权必须覆盖历史修订，避免旧样本进入旧式预签名旁路。管理预览只服务当前绑定，不作为旧任务原图；旧filesystem字节已覆盖时仍严格报不可用，S3固定旧版本可继续读取。后续关联项目沿用追加语义，为旧、新样本都建任务。预检差异及原阻断记录保持冻结，另列接纳结果；周期计划仍须显式继续。

```text
完成预检 + 固定候选 -> 单项确认 -> 严格读取 + 前驱CAS
                                      |
                           新Item/Asset/空白Task + 修订账本
                                      |
                           当前绑定推进；旧业务身份保留
```

超过8文件：0203迁移、dataset_source模型及注册、schemas、dataset_source_revisions新服务、dataset_references/changes/lifecycle/policy/previews、dataset_sources路由、Web API/差异面板/样本标识、回归及迁移测试、OpenAPI/SDK覆盖声明、API/用户/开发文档、ADR0102、CHANGELOG、README和计划导航。

验证包括：选择性接纳、多项目空白任务、旧标注与引用不变、copy业务去重隔离、幂等/过期generation/前驱竞争/ABA、权限撤销与来源暂停/文件再次变更、历史媒体防旁路、后续无变重扫、旧候选列表、迁移空往返/真实已有绑定回填/有账本拒绝降级。运行相关后端回归、Web组件/类型/语义色检查、OpenAPI/SDK、文档与真实浏览器验收。回退关闭接纳入口、停止任务并保留账本与已接纳业务；不执行破坏性降级。测试资源和临时产物清理，人工验收dev服务与数据继续运行。

## Outcome — E04-B3

- Landed changes: explicit per-item acceptance of `changed/content_changed` in a completed preflight. New business Item/Asset and blank linked-project Tasks, immutable revision lineage, exact predecessor fencing, duplicate confirmation replay, and current-head advancement commit atomically. Managed-copy SHA dedup cannot inherit another business identity. Included in the commit containing this outcome; no release milestone assigned.
- History: migration0203 backfills existing heads without fabricated actors/timestamps. Database guards protect revision facts and head/ledger agreement. Permanent Item/Asset IDs survive business deletion; historical ownership and lifecycle impact include every revision. Old Scan pages never fall back to a newer same-SHA identity after deletion. Management previews remain current-Asset derivatives.
- User documentation: `docs-site/user-guide/datasets/import-images.md` and `index.md`; API: `docs-site/api/guides/dataset-sources.md`, OpenAPI snapshot and SDK coverage exclusions; developer concepts: `docs-site/dev/concepts/storage-connections.md`; decision: `docs/adr/0102-explicit-directory-content-revisions.md`; README and CHANGELOG Unreleased updated.
- Verification: 151 backend regression cases passed across incremental differences, periodic coordination, directory registration/policies/references, S3/SFTP and migration compatibility. Final 18 targeted cases passed, including independent concurrent confirmations, stale predecessors, ABA/deleted history, retained annotations, two changed paths with selective acceptance, blank Tasks in two projects, copy identity isolation, pinned S3 old/new versions, I/O actor/source/policy revocation and original API-key rotation/revocation at both read barriers. Migration coverage includes fresh upgrade, empty roundtrip, actual-head backfill, null provenance and populated rollback refusal. A final 46-case revision/reference/policy regression passed after adding historical-scan replay protection; two final S3 replay/concurrency cases passed on the final implementation. Web: 29 component cases passed; local TypeScript, ESLint, semantic CSS tokens and 4 SDK OpenAPI contract cases passed. Documentation build passed. No remote CI or release qualification is claimed.
- Live acceptance: retained current-worktree dev stack at Web3100/API8100 with actual workers and beat. Browser confirmed candidate preview, explicit single-item confirmation, accepted-result priority and revision badge; opened the new task with the new image and zero annotations. Independent API assertions confirmed two Items/Tasks/revisions, the old annotation unchanged, old overwritten filesystem content rejected with409 and new content returned200. Browser error collection was empty.
- Environment and cleanup: user-requested manual dev database, Redis, buckets, source fixtures and supervised services remain running; guide and restart helper are in ignored `.worktree/manual/`. Original three-image dataset remains usable; a separate revision demonstration is retained. Disposable test resources, task browser/auth entry and temporary test/build outputs are removed at completion. No backend image rebuild or shared configuration changes.
- Remaining work: E04 remains active for same-SHA observation/qualification changes, move/rebind and missing decisions. Old external bytes are not backed up by this feature. Starting additions-only registration closes this preflight's revision action; unhandled content changes require a new scan/preflight. Frozen difference/error counts remain historical, and per-item acceptance does not automatically release the periodic review gate. New project links enumerate both retained old and new samples.

## 本轮实施定案：E04-B4 同路径同摘要重新核验

基线 `85f10fdc0`，工作区干净，人工验收环境 Web3100/API8100 正常且继续保留。用户授权推进与提交。本轮只开放完整预检中的 `changed/observation_changed`，即路径、来源资格和 SHA 相同、文件观测改变。资格锚点变化、移动、未见/删除及账本压缩仍保留草案；不修改部署资格，不重建镜像，不新增依赖、配置或服务。

最小方案是改写原 Reference 的观测，但会丢失历史。本轮复用不可变 SourceRevision：追加同 Item/Asset/SHA 的接纳修订，继承原媒体所有权并推进路径头；原 Reference、业务对象、任务、标注和样本元数据不变。无需 schema 迁移。读取只选择原 Reference 同来源/路径、同 Item/Asset 的最新已接纳修订，按其固定观测重新严格读取；失败不回退旧凭据，不搜索其他同摘要样本。后续不同内容的新样本不会改变旧样本读取身份。

新增 `POST /dataset-registrations/{registration_id}/changes/{path_digest}/revalidate`，请求与新内容接纳相同，固定 generation、前驱修订 ID 和候选 SHA。沿用 Location→Source→Dataset、两次严格快照及授权/Key proof/策略屏障；只接受同摘要且原样本和 Asset 仍存在的当前头。并发相同请求返回同一接纳记录，旧预检不能越过前驱，重复确认不回退后续头。审计区分重新核验与新内容接纳；差异与旧预检异常保持冻结，结果单列，周期门禁仍需人工继续。

```text
完整预检 + 同路径同SHA观测变化 -> 人工预览/确认 -> 严格读取 + 前驱CAS
                                                     |
                         同Item/Asset的追加接纳记录 + 当前路径头
                                                     |
                         原任务按最新明确接纳的观测严格读取
```

超过8文件：dataset_source_revisions/references/changes、API路由与schema、Web API/差异面板/确认弹窗/登记进度、后端并发/权限/引用回归与Web测试、API快照/SDK声明/路由索引、用户/API/开发文档、ADR0103、README/CHANGELOG及计划进度。复用当前所有者和界面，不增加管理页。

验证：同字节重写前拒绝/确认后恢复原任务读取、Item/Asset/Task/标注及数量保持、多次重新核验、原Reference/回执不变、后续内容新修订不污染历史样本、托管复制不新增原图或丢失所有权、S3同字节新版本、SFTP观测兼容、普通登记不隐式恢复、错误类别/资格变更拒绝、过期generation/前驱与重复/并发、I/O文件变化/撤权/Key轮换及读取上下文变化。运行相关回归、API/SDK/Web/文档检查与真实浏览器确认，清理一次性test资源及产物；保留dev人工环境和演示数据。

回退关闭新入口并保留已接纳记录与新版严格读取；应用整体回退会失去重新核验后的可读性，因此不删除账本或改写原引用来掩盖差异。该阶段不备份外部原图，也不自动接纳扫描变化。

## Outcome — E04-B4

- Delivered explicit same-path, same-SHA observation revalidation in completed preflight. Immutable revision receipts advance the path head while preserving Item/Asset, original Reference, metadata, Tasks, labels and ownership. Ordinary scans/registration never implicitly renew old reads. Qualification changes remain blocked; no schema migration.
- Reads verify the original binding and use the latest accepted receipt for the exact source/path/business asset, with the selected revision ID checked across I/O. Later content revisions cannot retarget old samples. Historical API distinguishes initial acceptance, content revisions and observation revalidation; frozen differences and periodic review gates remain unchanged.
- Verification: the 109-case backend regression passed 108 cases initially; its new S3 test used a nonexistent helper name, which was corrected and passed separately. An additional SFTP same-byte/changed-mtime case passed. Includes independent concurrent confirmations, both I/O authority barriers, original Key rotation/revocation, strict read before/after revalidation, no duplicate business objects, retained labels, managed-copy ownership/no new upload, and historical isolation. Five Web component cases, TypeScript, full Web lint/CSS tokens, Ruff, SDK/OpenAPI (4 cases), docs build and diff checks passed locally. Existing unrelated lint warnings remain; no remote CI claimed.
- Actual browser acceptance: separate retained fixture showed a decoded candidate with matching old/new SHA, explicit checkbox and successful single-item revalidation. The original Workbench Task then displayed the original image and one retained manual box, with one Item/Task total. No browser errors. Current-checkout API, both workers and beat were refreshed without image rebuild; all user-requested manual fixtures/services remain.
- Full-E04 scope audit: AC05 still requires explicit relocation. Original contracts also require opt-in pure-addition ingestion, filter-change impact review, schedule timezone display, source/history navigation, and a frozen-version invariance regression. Continue these before closing or archiving the Epic. Physical deletion, external qualification replacement, calendar cron and ledger compaction are outside the required closure; durable evidence remains retained under existing source budgets.

## 本轮实施定案：E04-B5 显式移动重定位

基线 `03308a2ad`。为满足 AC05，在同一来源、完整预检中，由用户把 `not_seen` 的旧当前样本和 `added/ready` 的同 SHA 新路径明确配对。摘要相同仅生成候选，不自动认定移动；支持多候选分页选择。旧路径未见仍不是物理删除证明，所有外部文件保持只读。

POST `.../changes/{目标path_digest}/relocate` 固定 generation、旧前驱 ID 和候选 SHA。追加同 Item/Asset 的目标路径修订，业务前驱指向旧路径当前修订，目标路径修订号独立递增；继承所有权。一个事务内退役旧路径头、建立新头、把目标登记条目标为 registered 并调整原计数，原差异两端保持冻结且显示同一裁决结果。重复确认返回原记录；目标已占用、旧头过期、业务删除或候选变化拒绝。旧路径日后重新出现视为新增，不自动抢回已移动样本。

0204迁移新增数据库单调 acceptance_order，既有账本按来源/路径/修订号回填，不虚构接纳时间；不可变触发器继续保护记录，并增加跨路径前驱身份一致性检查。读取保留原 Reference 起点校验，按同来源/同 Item/Asset 最新接纳序号解析实际新路径，固定原资格和根；不根据路径内修订号、时间或摘要猜最新位置。存在移动记录时拒绝降级到不支持跨路径读取的应用 schema。

复用 register_entry 两次严格快照、Location→Source→Dataset 锁、原账号/Key proof/策略/前驱复核及并发重放；移动不会建 Item/Task、复制标注或托管原图。旧路径派生预览先记录其准确对象键，原子删除旧头后按新路径生成预览，提交后仅清理旧预览对象。失败保留旧有效定位。

```text
旧未见项 -> 分页同SHA新增候选 -> 人工选择与图像确认
                                      |
                      严格读取 + 旧头CAS + 目标空缺
                                      |
                 接纳定位账本 + 新头 / 退役旧头 / 消费新增候选
                                      |
                      原Task按新定位读取，标注身份保留
```

超过8文件：0204迁移、模型、revisions/references/changes及移动服务、schema/API、Web API/差异面板/移动确认、后端与迁移回归、API/SDK/用户/开发文档、ADR0104、CHANGELOG/README和计划进度。无新依赖、服务、环境配置或镜像构建。

验收覆盖 A→B、A高修订→B低修订、A→B→A、原路径回归、同SHA多候选需明确选择、目标消费后不重复登记、双方CAS/并发幂等、两次I/O撤权/候选变化、最新定位失败不回退、预览清理与托管所有权、旧标注/任务/业务身份不变、迁移回填与回退屏障。实际浏览器移动前读图拒绝、目标预览与确认、原Task恢复且原框保留。保留人工dev，测试只用独立test资源；全Epic其余合同继续推进。

## Outcome — E04-B5

- Delivered explicit same-source relocation from a frozen not-seen predecessor to a user-selected, same-SHA added/ready target. Business Item/Asset/Task/labels and original Reference remain unchanged; the transaction retires the old head, creates the target head, consumes the target registration entry, preserves ownership and records audit. Both frozen difference rows display the resolution. Returning former paths are independent additions.
- Migration0204 adds immutable database acceptance ordering and cross-path predecessor identity guards. Current reads choose the latest accepted locator for the exact business asset across paths, retaining original qualification checks and rejecting failed latest reads without fallback. Path-local revision numbers remain independent. Backfill preserves original facts; populated relocation history blocks unsafe downgrade. Old preview keys are cleaned only after commit; managed originals remain untouched.
- Verification: 35-case focused backend regression passed; final 14-case relocation/migration suite passed, including direct SQL rejection of changed cross-path identity/source. Five independent concurrent-transaction cases passed: content and observation replay, identical moves, two targets competing for one predecessor, and two predecessors competing for one target. Covered high-to-low path revision numbers, roundtrip moves, explicit multi-candidate selection, returning paths, target consumption, I/O authority/file changes, managed ownership/preview cleanup, and exact S3 keys. Ten Web component cases passed, including decoded-target confirmation and no remaining additions after all targets are consumed. TypeScript, full Web lint/CSS, Ruff, OpenAPI consistency, SDK contract (4), documentation build and diff checks passed locally. No remote CI claimed.
- Actual browser: a separate retained filesystem fixture was moved before preflight; old Task content was409. Browser explicitly selected and previewed the target, confirmed relocation, displayed matching results on both paths and kept additions confirmation disabled even when its checkbox was checked. Reopened the exact original Task and visually inspected the original image with its original one-box annotation. API assertions confirmed one original Item/Task/annotation, latest target locator, remaining additions0 and content200. Browser error collection was empty. User-requested dev services and all fixtures remain running.
- Remaining full-E04 work: opt-in pure-addition ingestion, schedule timezone, rule-change impact review, last-complete/history and storage fault navigation, and existing frozen-version invariance acceptance. Continue implementation before closing the Epic. Test infrastructure and task-owned temporary artifacts are cleaned between slices; no image rebuild, push or release.

## Execution contract — E04-B6

Base: `050b40832`. Implement opt-in `pure_additions` ingestion on the existing periodic coordinator. Default `manual` is unchanged. A cycle pins its policy at scan creation; enabling automation later cannot adopt a manual cycle. Only a complete error-free preflight containing additions and unchanged entries qualifies. Any other category waits for human review before any business ingestion. Automatic confirmation uses the real source worker identity and the existing additions-only registration transaction, with plan revision/token/authority checked after each external I/O and before publishing the attempt. Only automatically confirmed runs with the exact original delegation may be republished/resumed by this coordinator; human registration remains observation-only. Pause stops new coordination, while already confirmed work retains its original authority.

Persist a validated IANA display timezone and explicit policy in migration0205. Fixed intervals retain elapsed-minute semantics, including 1440 minutes = 24 hours; this is not calendar/cron scheduling. Existing schedules migrate to manual/UTC. Downgrade refuses enabled automatic schedules rather than silently removing their policy. UI exposes policy and timezone, shows times in the chosen timezone, and explains the whole-cycle manual gate.

Touched areas exceed eight files: migration/model/schema, registration confirmation and sync service, schedule UI/API generated artifacts, backend/migration/Web regressions, API/user/developer docs, CHANGELOG/README, ADR and this plan. No dependency, image or environment-variable changes. Test only disposable worktree test resources, refresh owned dev worker/beat, then verify opt-in ingestion and a mixed-change manual gate in the actual browser. Retain the manual environment and original fixtures.

## Outcome — E04-B6

- Delivered default-manual / opt-in pure-addition ingestion through the existing registration state machine. Each scan pins policy; mixed changes/errors hold the entire cycle for review. Automatic confirmation uses the actual worker node and checks original delegation and plan revision/token before and after content I/O. Only owned automatic formal attempts are recovered; human attempts remain observation-only.
- Migration0205 safely backfills manual/UTC and refuses rollback of enabled automation. Validated IANA display timezone retains elapsed-minute intervals. Rejected system aliases that browsers cannot format, with explicit UTC fallback for browser timezone differences. Automatic progress invalidates dataset counts/items as well as scan and registration views.
- Local verification: 69 successful backend cases across focused runs (including corrected rollback-only test assertions), covering existing periodic/manual contracts, automatic dispatch/recovery, whole-cycle mixed-difference gates, non-retroactive policy, both pause/Key-rotation I/O barriers with zero capture, registration compatibility and migration rollback/defaults. Final 36-case automatic/registration suite passed, timezone follow-up passed. Three focused Web cases passed. TypeScript, full Web lint/CSS, Ruff, OpenAPI consistency, SDK contract4 and documentation build passed. An accidentally broad Web invocation ran633 files:632 passed;14 existing StoragePage cases failed for missing QueryClientProvider. This test fixture issue will be addressed with the remaining storage navigation work; no full-Web green claim is made.
- Actual browser: explicitly selected pure-addition policy and Asia/Shanghai in a separate retained source, then waited for real beat, exact-node scan, full preflight and automatic formal registration. Browser showed completed registration and one indexed file; read-only API assertions confirmed exactly one Item and one linked-project Task. Screenshots of the saved policy/timezone and completed registration were visually inspected. Owned API, worker, maintenance worker and beat were refreshed without an image rebuild; worktree doctor is healthy. The mixed-change live cycle remains part of final Epic acceptance.
- The E04 plan remains active for filter-change impact, history/storage navigation, frozen-version invariance and complete acceptance. Retained manual fixtures and dev resources are preserved; disposable test infrastructure and task temporary files are cleaned between slices. No push, release or version assignment.

## Execution contract — E04-B7

Base: `a8b6e0cca`. Add preview/apply for recursive/include_globs/exclude_globs only. Locator, node, root and ownership stay immutable. Preview evaluates up to50000 accepted heads with backend-exact selection semantics; it does not enumerate external storage or predict unknown new files. Bind proof to proposed rules, source/location revision and ordered head revision identities plus active attempts. Apply rechecks original actor/Key under Location→Source locks, rejects active attempts, appends audit, advances Source revision/generation and explicitly blocks the old periodic plan until reconfigured. Old scans/preflights/checkpoints remain stale.

Extract shared selection helpers: filesystem/SFTP excluded directories prune descendants; S3 keeps exact flat-key fnmatch semantics, preserving slashes/encoding. New comparisons classify accepted paths outside new rules as excluded even when ancestor pruning omitted them. Existing business reads compare every locator/config key except these three explicit selection keys, preserving original root/qualification/content and before/after I/O fencing. No Item/Task/label deletion or new identity.

Changes exceed eight files: shared rules helper, rule service, scan/S3/changes/reference callers, schema/API/Web rules panel, backend/Web regressions, generated API/SDK/routes, ADR0106 and user/developer/API/CHANGELOG/README/plan. No migration/dependency/image changes. Verify CAS after head moves, authority, active attempts, reader continuity and strict locator changes, old-preflight rejection, matching parity and actual browser preview/apply/new scan.

## Outcome — E04-B7

- Delivered reviewed recursive/include/exclude changes through dedicated preview/apply endpoints. The bounded accepted-head preview is bound to proposed rules, Source/Location revisions, exact current head identities and active attempts. Apply rechecks initial authority, rejects stale proof or active work, advances revision/generation and audits. Existing schedules are blocked and stale current-cycle pointers cleared; domain history remains. Re-enabling starts a new configuration cycle.
- Shared backend matching preserves FS/SFTP ancestor pruning and exact flat S3 key behavior. Omitted accepted descendants outside the new selection become excluded, never inferred missing. Business reading excludes only the three selection keys from locator comparison; roots, qualification, unknown config, immutable original receipts and before/after I/O checks remain strict. UI rule lists preserve commas, whitespace and embedded newlines without delimiter rewriting. Saving also refreshes paused schedule revision.
- Local checks:47-case rules/FS/S3 regression passed; final19-case rules suite and2 Key-rotation lock-boundary cases passed. Nine Web component cases passed, including exact glob roundtrip, discarded preview after edits and paused-schedule refresh before re-enable. TypeScript, full Web lint/CSS, Ruff, OpenAPI check, SDK contract4 and documentation build passed. No remote CI claim.
- Actual browser: separate retained nested-path fixture previewed fleet/vehicle.png leaving scope, required explicit review, saved fleet exclusion, then ran a new scan/preflight. Difference showed excluded1/not_seen0 with readable explanation. Opened the exact original Task and visually inspected its original image and one bbox annotation. Read-only assertions confirmed Source revision2, original single Item/Task/label, one original acceptance receipt and original content200. Browser errors were empty. Original dev fixtures remain.
- Completed the remaining B6 live gate: real beat cycle with one changed and one added file stopped at awaiting_review; browser and API confirmed no partial automatic ingestion and original single Item/Task. The demo is paused for inspection.
- Remaining full-E04 work: last-complete/history and storage fault navigation; existing managed frozen-version invariance regression; final acceptance matrix, retention decisions and plan archival. Disposable test resources and task temporary artifacts are cleaned between slices. No image rebuild, push or release.


## Execution contract — E04-B8

Base: `775fd5649`. Complete source navigation with independently queried last-complete and active scans, paginated retained history, and selected historical scan details. Source/scan links must validate dataset→source→scan membership and survive clearing URL query parameters within the same authenticated session. Direct source lookup supports sources outside the first200 list entries. Inactive/unregistered sources retain read-only history; selecting an older scan cannot hide active work or enable conflicting actions.

Add a storage-location source summary with separate source/job pagination, fixed query count, current manager/location visibility, and source ownership scope. Existing job payload matching retains prior attempts after resume without casting untrusted payloads to UUID. Related jobs cover all visible sources at that location, not only one source page; open the existing job detail modal. Location list is paginated across all backends and fetched summaries only for the selected location. No source credentials or full configuration are returned.

Changes exceed eight files: API/schema/access, Web API/source panel/registration/Storage/Datasets page and tests, frozen-training integration regression, generated API/SDK/routes, user/API/developer docs and CHANGELOG, final Epic acceptance and roadmap/plan archival. No migration, dependency, environment variable, image or service changes. Fix the existing StoragePage test provider fixture while touching that surface.

Verify source/job scope and pagination, last-complete beyond20 failed attempts, source201 deep link, invalid relationships, active-versus-history behavior, session isolation and read-only history. Build a real managed-copy training version against disposable test MinIO, then rescan changed/removed originals, accept a new revision, cancel another scan and replay the ready build. Assert frozen snapshot, manifest, package and media bytes remain identical. Reference freezing remains E10 scope. Run relevant local checks and real browser history/storage navigation; retain the manual dev stack and fixtures, clean disposable test resources and intermediates. Record final AC01–AC10 evidence and retention/budget limits, archive E04 only after all checks pass, commit without push/release/version assignment.

B8 audit found and reproduced a frozen-package cleanup bug independent of source mutation: a successful ready commit followed by an acknowledgement exception deleted the published attempt ZIP. The B8 repair checks the persisted package_key before cleanup, retains referenced or unverified objects, and covers commit-then-raise against real test MinIO. Source history additionally exposes pinned source_revision and source-level active registration; older resumed runs require refreshing last-complete/history when the active scan changes or disappears. Complete-filter ordering uses completion time rather than creation time.


## Outcome — Complete E04 acceptance and archival

- Landed slices: A/B1–B7 recorded above, including `c6b80e026`, `85f10fdc0`, `03308a2ad`, `050b40832`, `a8b6e0cca`, `775fd5649`; B8 lands in this archival commit. Release milestone: not yet determined.
- B8 delivers independently queried last-complete and active scans, source-level active registration,20-row history, exact source/scan deep links with membership/session checks, and read-only inactive/history inspection. Older resumed scan completion invalidates history and last-complete even when the newest scan failed. Complete filtering orders by completion time. Sources beyond the first200 remain directly addressable.
- Location summary uses current manager/location/source scope, independent bounded source/job pages over all visible sources, historical job attempts and existing detail modal. It returns minimal navigation fields with no credentials/proofs/config payload and makes no external I/O.
- Fixed a reproduced published training ZIP deletion after lost commit acknowledgement. Cleanup rechecks persisted package_key, retaining referenced or unverifiable objects. Real managed-copy integration covers unchanged frozen snapshot/manifest/package/media bytes after changed and removed originals, explicit new revision/replay, cancelled stale scan and ready-build replay. Tests also cover cleanup verification being unavailable. External-reference freezing remains E10.
- Local verification:121 backend regressions passed, including source access/scan/rules/schedules, existing training behavior and migrations0196/0201–0205. Final navigation8 and frozen3 case suites passed; frozen3 adds the unavailable-verification case. Web45 focused cases passed, including pending/error current-task guards and late-session retirement. The pre-existing14 StoragePage provider failures are resolved using a fresh test QueryClient. TypeScript, full Web lint/CSS, Ruff, OpenAPI consistency, SDK contract4, docs build and diff checks passed. Existing unrelated lint warnings remain; no all-Web, remote CI or production claim.
- Actual B8 browser: from the storage location, opened a real failed task showing path_unavailable/incomplete, paginated related jobs, followed the exact source link, viewed history page2 of22 and opened the older complete scan/preflight/differences despite21 newer failures. Pausing retained history and disabled ingestion. Mismatched dataset/source/scan links were rejected; valid exact historical deep links worked. A viewport screenshot was visually inspected and browser errors were empty. The21 failures were generated by real workers against a task-owned temporarily unavailable directory, restored in finally; no fabricated DB history. Earlier B1–B7 live evidence is retained above rather than repeated.

| Acceptance | Evidence and result |
| --- | --- |
| AC01 | B1 three unchanged scan regressions and unchanged live rescan; no Item/Task growth, immutable accepted identity and new observations retained in scan history. |
| AC02 | B1 additions-only registration/replay/concurrency regressions and live25-item result; B6 pure-addition automatic live1 Item/1 Task. |
| AC03 | A scan failure/cancellation, unavailable paths, partial enumeration and recovery regressions; B8 real21 incomplete failures preserve last complete and zero new business items. No missing/deletion inference. |
| AC04 | B3 source-revision tests and browser old/new Task demonstration; distinct new blank Task, original labels retained. |
| AC05 | B5 explicit same-source relocation, multiple same-SHA candidates and stale/replay guards; original Task/bbox live continuity. |
| AC06 | A/B1/B2 duplicate messages, competing scans/coordination, leases and stale generation fences; source active uniqueness. |
| AC07 | A/B1/B2 recovery, cancellation, expired lease and publication-gap regressions; B6 exact delegation recovery; B8 cancelled stale scan commit rejected. |
| AC08 | B1 selected repaired-entry retry and frozen scope; B7 rule impact/CAS/current authority and stale checkpoint rejection; browser reviewed fleet exclusion. |
| AC09 | B1 not_seen preserves bindings; B3–B5 preserve labels; B8 real managed-copy frozen snapshot/ZIP/media unchanged under mutations, cancellation and replay. Reference freezing separately E10. |
| AC10 | Existing task stage/unknown-total presentation and B1 six-category count/pagination browser evidence; B8 history, existing failed-job detail and related-job pagination live. |

Retention decisions are explicit: durable Scan/Registration/Change/Revision evidence has no automatic expiry or compaction; AsyncJob uses its existing independent retention. not_seen has no deletion countdown. Each source has one schedule and at most one active scan plus one active registration; there is no deployment-wide schedule cap. Coordination wakes at most100 due plans per tick, scans retain existing50000-entry/5000-directory/per-directory budgets, and history is paginated. Monitor ledger growth; compaction, physical deletion, cross-source moves, qualification replacement and calendar cron are not required E04 closure.

Official documentation: `README.md`, `CHANGELOG.md` Unreleased, `docs-site/user-guide/datasets/import-images.md`, `docs-site/api/guides/{dataset-sources,storage-locations}.md`, generated routes/API snapshot and SDK coverage, `docs-site/dev/concepts/storage-connections.md`; architectural decisions ADR0093 and0095–0106 as relevant to prior slices. Roadmap and plan links are updated with archival.

Disposable test database/Redis/buckets and task-owned temporary artifacts are cleaned; current-checkout dev API/Web, general/maintenance workers, beat and all manual fixtures are preserved. Manual Web3100/API8100 remain available. No backend image rebuild, shared configuration change, push, release or version assignment. Remaining required E04 work: none; other Epics retain their own plans and gates.

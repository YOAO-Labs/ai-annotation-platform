# 代码库优化、测试精简与 CI E2E 重构计划

> 仓库：`yyq19990828/ai-annotation-platform`
> 审阅日期：2026-09-20（UTC）
> 审阅基线：`main`，提交 `9cec9751a9f7a5518cfa09af6d1d75a789f28758`，已合入 PR #129「项目级员工角色与审核证据闭环」。
> 状态：已完成并归档。P0–P10 的实现、验证、台账关闭与清理均已完成；远端 CI 未运行的边界保留在最终验收记录中。

**路径约定：文中所有代码、配置和文档路径均相对仓库根目录。** 标注“拟新增”的路径是建议落点；实施时若已有同职责模块，应合并到原有模块，不再建立第二套实现。执行前核对当前 HEAD 与上述基线的差异。

## 1. 结论与改造边界

本次目标不是“把长文件拆短”“减少测试数量”或“让 CI 更容易通过”，而是：**减少理解和修改一个业务行为所需跨越的模块、重复规则和测试前置条件，同时保留真实的质量约束。**

四项要求必须一起完成：

| 目标                       | 完成后的状态                                                                   | 不接受的替代做法                                                   |
| -------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| 可复用、可读、可解释的代码 | 同一规则有明确归属；调用链可追踪；业务判断与副作用分离；废弃路径已删除         | 把原有大文件搬进新的大 hook / `helpers.ts`；为了消灭 `if` 引入框架 |
| 精简高质量测试             | 每个重要业务约束有合适层级的保护；重复、过时、只验证实现细节的测试被合并或删除 | 按覆盖率、测试数量或执行时间直接删测试                             |
| 合理的 CI E2E              | PR 验证核心与受影响行为；扩展矩阵有明确触发条件；首次失败可解释                | 提高重试、扩大错误白名单、自动更新截图或放宽预算来换绿灯           |
| 注释与文档可信             | 注释说明当前契约和原因；历史版本说明退出活动代码；架构和测试入口一致           | 全仓正则删除所有版本字符串、删除迁移标识或历史记录                 |

**保持业务语义，默认不改数据库模型、外部 API、协议和产品功能。** 发现真实缺陷时，独立提交“缺陷修复 + 回归测试”；不要夹在行为等价重构中。

### 关于“是不是在基于正确代码修测试”

这个担忧有依据，但需要分三类：

1. **需求改变，旧测试契约不再成立：应该修测试。** PR #129 已把平台员工身份和项目职责分开。提交 `cbd8d620` 修正了 E2E 仍通过用户角色接口设置旧平台 `annotator` 的准备逻辑。不能为了保住旧测试恢复已废弃的全局角色语义。
2. **测试依赖非目标实现细节：应该改测试设计。** 同一提交为多个图片测试和共享视频 helper 补充请求取消规则，说明请求卫生断言存在重复维护成本；取消了合法的旧查询，不等于产品功能失败。
3. **测试或评审发现真实业务问题：必须修产品并保留保护。** 提交 `aa1c879c` 包含事务提交后发布通知、长推理结束后重新授权、锁顺序、离线队列错误分类等产品修复，不能归为“测试太苛刻”。

因此，方向是**让测试围绕稳定的业务契约，而不是取消约束**。现有资料不足以量化“多少失败是测试问题”；第一个工作包会采集真实失败记录，而不是凭提交标题统计。

## 2. 审阅范围与证据边界

本计划基于仓库文件、配置、代码检索及近期 PR/提交内容的静态审阅。没有在本地完整检出仓库，没有执行项目测试，也没有逐条验证全部历史 CI trace。因此不声称已经完成全仓重复率统计、死代码判定或失败根因归类。

以下问题已从文件直接确认；其余目录通过工作包 P0 的全仓清单纳入实施，不把抽样判断冒充全量审计。

| 已确认的现状                                                                                       | 位置                                                                                                      | 改造判断                                                           |
| -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| 工作台页面入口已很薄，外壳使用 model 装配                                                          | `apps/web/src/pages/Workbench/WorkbenchPage.tsx`；`apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` | 保留该分层；不再把“拆页面入口”列为主要成果                         |
| 工作台 model 同时连接任务、权限、离线队列、Mask、AI、视频、Issue、布局与 UI 内容                   | `apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx`                                           | 首要复杂度治理点是业务职责和状态归属，不是行数                     |
| model 的 helpers 同时承载导航调度、离开守卫、推理参数、显示派生等内容                              | `apps/web/src/pages/Workbench/state/useWorkbenchShellModel.helpers.ts`                                    | 按已有领域归属收敛，避免成为第二个混合职责大文件                   |
| 失败与取消信号中重复创建 session、查找作业、按 kind 更新领域表、通知与提交                         | `apps/api/app/workers/signals.py`                                                                         | 抽公共生命周期骨架；保留各作业类型不同的终态语义                   |
| 已存在不可变项目访问上下文和角色能力映射                                                           | `apps/api/app/services/project_access.py`                                                                 | 复用现有授权源；不是再建设一个权限框架                             |
| 测试导入时全局包装 `Project` / `ProjectTemplate` 构造器，将旧字段转成 `tool_bindings`              | `apps/api/tests/conftest.py`                                                                              | 迁移 fixture 到现行数据模型后删除测试 shim                         |
| `httpx_client` 顶部说明称未绑定 session，实际实现已绑定；仍有旧名称别名                            | `apps/api/tests/conftest.py`                                                                              | 修正说明；统一调用名称后删除冗余别名                               |
| 页面流程测试大量 mock 内部 hooks，仍出现旧 `role: "annotator"` 形态                                | `apps/web/src/pages/Projects/ProjectDataManagerPage.flow.test.tsx`                                        | 重新划分 URL 纯逻辑、页面装配和权限集成测试；不直接整文件删除      |
| 测试全局设置 5 秒异步等待；MSW 未处理请求为 `warn`；Konva 全局使用 DOM 替身                        | `apps/web/vitest.setup.ts`                                                                                | 明确测试边界；减少漏 mock 的假通过；真实画布测试不能由这些单测替代 |
| 覆盖率配置包含大量历史计数与阈值调整叙事，且部分画布目录被宽泛排除                                 | `apps/web/vite.config.ts`                                                                                 | 保留当前有效门槛，删除历史叙事；按实际逻辑复查排除项               |
| PR 固定选择四个默认分片与三个 Mask 矩阵；前后端等改动再加入 visual / layout-stress                 | `scripts/plan-e2e-suites.mjs`                                                                             | 当前是 7 或 9 个矩阵条目，不是精确影响分析                         |
| suite planner 已有独立测试，不能当作完全未建设                                                     | `scripts/plan-e2e-suites.test.mjs`；`.github/workflows/ci.yml`                                            | 扩展现有声明和测试，不并行新增另一套选择器                         |
| 每个 `built` E2E job 独立构建前端；矩阵已经 `fail-fast: false`                                     | `.github/workflows/e2e-run.yml`                                                                           | 评估同构建条件产物复用；保留已有失败隔离                           |
| E2E 因共享 fixture 清理使用一个 worker；CI 重试 1 次、首个最终失败后停止当前套件、全局上限 15 分钟 | `apps/web/playwright.config.ts`                                                                           | 先治理隔离和用例体积，不能直接加 worker 或把超时归咎于产品         |
| 当前 planner 对 schedule / 手动事件仅返回两个扩展条目                                              | `scripts/plan-e2e-suites.mjs`                                                                             | “定时运行”不自动等于“全量验证”；新策略必须显式区分执行范围         |

### 2.1 已有能力应保留

保留项目能力映射、API 快照与类型生成、迁移检查、后端真实数据库测试、Python SDK 和示例协议测试、工作树测试库隔离、Playwright trace / JSON 报告、独立 E2E runner 和稳定的汇总检查。

当前 `Frontend E2E` 汇总任务只是检查 planner 与执行任务结果。它因子任务失败而失败，本身不是第二个独立故障；排查应回到具体 suite、test 和第一次失败证据。

## 3. 不可破坏的业务约束

将下表作为重构与删测试的保护清单。每一项必须在测试清单中映射到现有测试标识，缺失时补充最小测试，再改代码。

| 约束                                                                    | 主要保护层级                                       | 必要浏览器验证                                      |
| ----------------------------------------------------------------------- | -------------------------------------------------- | --------------------------------------------------- |
| 平台身份不推导项目职责；不同项目能力互不串用；未知角色拒绝访问          | `project_access.py` 领域/数据库测试与 API 合同测试 | 同一员工跨项目进入不同工作模式                      |
| 自审禁止、冻结审核证据、证据未知不能被伪造为可审核                      | 审核服务与 API 集成                                | 一条真实标注提交→审核链路                           |
| 撤权、停用或所有权转移后，长任务、导出、WebSocket、离线重放重新校验授权 | 服务/worker 集成及前端状态测试                     | 撤权后的核心写入被阻止，错误提示正确                |
| 已确认的标注不能因切换任务、旧异步响应、取消或重试被覆盖                | 状态/命令测试与持久化集成                          | 切换后保存、刷新仍一致                              |
| 任务锁、版本冲突、幂等键和锁顺序保持语义                                | 数据库/并发集成                                    | 冲突反馈与草稿保留各一条代表链路                    |
| 通知与事务提交顺序正确；失败/取消/部分完成不能混淆                      | 后端事务和作业生命周期集成                         | 一条跨会话通知或轮询终态验证                        |
| 图片、视频、点云及 Mask 的坐标、帧、工具和持久化行为正确                | 纯几何/状态测试 + 专项集成                         | 各渲染家族的最小真实交互，不用 DOM 替身冒充画布验证 |
| 不可逆迁移不承诺有损回滚；测试数据不污染开发或生产                      | 迁移与运行环境验证                                 | 不适用                                              |

跨层覆盖不天然冗余：服务测试证明规则，API 测试证明入口没有绕过规则，E2E 证明关键 UI 链路真正接通。需要删除的是同一层反复证明同一件事、或在高层穷举应由低层承担的组合。

## 4. 目标代码组织

### 4.1 依赖方向与规则归属

```text
前端页面 / WorkbenchShell
    → 页面装配 model：连接数据与功能模块，不重新定义业务规则
    → 领域 hook / command：任务导航、编辑会话、AI、Issue、审核
    → API client 与生成的类型
    → 后端路由：解析请求、绑定身份、组织响应
    → 服务：业务规则、授权、事务、持久化
    → worker / 外部模型 / 存储适配

纯函数：输入 → 输出，不读取隐式全局状态，不操作网络或数据库。
测试：优先围绕以上边界，不依赖内部函数拆分方式。
```

这不是要求新建所有这些层。沿用现有目录；只有现有模块确实混合了不同职责时才提取边界。

### 4.2 复用的准入规则

提取辅助函数前，回答三个问题：多个调用点是否共享**相同业务含义**？参数和错误语义是否相同？能否用清楚的领域名称描述？仅仅代码长得相似，不足以合并。

| 当前候选                 | 建议归属与做法                                                                                                                                                 | 明确不做                                                            |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 项目权限判断             | 复用 `apps/api/app/services/project_access.py` 和 `apps/web/src/hooks/useProjectAccess.ts`                                                                     | 前端复制完整角色矩阵；把成员职责写回 `user.role`                    |
| 导航、离开守卫和编辑会话 | 优先复用现有 `useWorkbenchTaskFlow`、`useMaskEditorSession`、`LatestTaskNavigationScheduler` 所在模块；必要时把 scheduler 独立成同目录领域模块                 | 再建一个“通用 workflow engine”                                      |
| Mask 错误恢复与提示      | 从 model 中提取到同目录明确命名的纯策略模块，例如拟新增 `maskMutationPolicy.ts`                                                                                | 用 `utils/error.ts` 混合登录、推理、Mask 与存储的所有错误           |
| 工作台 Issue 显示        | `WorkbenchShell.tsx` 内的状态文案和局部视图按 Issue 职责提取                                                                                                   | 为几个合理 guard 建抽象渲染框架                                     |
| 异步作业终态更新         | `apps/api/app/workers/signals.py` 保留薄适配器；已有 service 承担领域更新，缺失时新增同域终态 handler 模块                                                     | 所有作业共用一个会抹平 `partial` / `rollback_failed` 的万能更新函数 |
| E2E 请求异常分类         | 从 `apps/web/e2e/helpers/video-request-errors.ts` 演进为图片/视频可共享的有类型策略（落点 `apps/web/e2e/helpers/request-errors.ts`）；迁移调用点后去掉重复逻辑 | 直接忽略所有 `ERR_ABORTED`、全部 4xx 或全部 console error           |
| 后端测试数据构造         | 扩展 `apps/api/tests/factory.py`，明确创建用户、项目、成员、工具绑定                                                                                           | 在 ORM 构造器上悄悄兼容历史 fixture                                 |
| 脚本参数解析、文件遍历   | 对 `scripts/` 中确认重复且语义相同的少数纯逻辑就近复用                                                                                                         | 为若干短脚本建立大型 CLI 框架                                       |

### 4.3 如何减少分支而不降低可读性

- **保留 guard clause。** 未登录、无权限、资源不存在、状态不允许等判断应明确可见，不要隐藏进装饰器链。
- **对封闭的同类操作使用穷尽分派。** 例如作业类型→领域终态 handler、Issue 状态→文案。未知值必须有明确行为，不允许静默成功。
- **对有优先级的规则保留顺序。** 如能力禁用原因、冲突处理、授权检查；字典查找不能取代业务优先级。
- **限制布尔参数组合。** 已出现互斥或非法组合时改用可辨别联合/显式命令，而不是不断加入 `force`、`skip`、`legacy`。

示例：`signals.py` 可复用“连接→查作业→调用领域 handler→提交→释放资源”的骨架，但失败、取消、回滚失败、已有已提交分片的部分完成必须由不同规则处理。重构前先锁定这些差异，而不是把相似 SQL 全部合并。

### 4.4 工作台拆分的具体顺序

优先从低风险边缘向中心推进：

1. 提取纯策略：Mask 错误恢复/提示、导航与显示派生；保持测试输入输出不变。
2. 整理现有领域模块的公共接口：任务切换、编辑会话、离线队列、AI 请求、Issue 导航分别明确状态、命令和副作用。
3. 把仍留在 `useWorkbenchShellModel.tsx` 的相应闭包逻辑迁回上述领域；保留一个薄装配 model。
4. 将需要 JSX 的局部 UI 内容交给已有 shell/sidebar/stage 组件；避免领域 hook 同时负责弹窗内容、业务决策和请求提交。
5. 删除过渡转发、未使用导出、旧 helpers；更新直接调用方和文档。

每个异步模块必须回答：哪个 `projectId/taskId` 拥有该请求？切换时谁取消？旧结果由谁拒绝？失败时谁保留草稿？谁负责清理？**AbortController 只是取消机制，不代替响应归属校验。** 不同领域在取消后是否重试、是否提交不能被强行统一。

## 5. 测试精简策略

### 5.1 先建立契约清单，再删测试

P0 生成一个精简测试清单，字段为：

```text
测试标识 | 文件 | 业务约束/故障类型 | 所属层级 | 必需依赖
首次执行耗时 | 历史失败类型 | 处理决定 | 替代测试 | 删除/合并理由
```

处理决定只使用：`KEEP`（保留）、`MERGE`（合并）、`MOVE_DOWN`（下沉）、`REWRITE`（重写）、`DELETE`（删除）。不能只填“太多”“容易失败”。

允许直接删除的范围：已删除功能的测试；只复述类型/常量且有更强契约保护的测试；同层完全重复的断言；只验证框架行为的测试。若“错误输入”“无权访问”“网络失败”等看似相近但保护不同失败方式，不合并成一个含糊 smoke。

对于安全、持久化、并发约束，删除前做一次针对性负向验证：临时破坏相应规则，确认保留测试会失败，再恢复修改。无须全仓上新 mutation 平台；记录最小证据即可。

### 5.2 前端测试具体处理

**修改范围：**

- `apps/web/vitest.setup.ts`
- `apps/web/vite.config.ts`
- `apps/web/src/test/`
- `apps/web/src/mocks/`
- `apps/web/src/pages/Projects/ProjectDataManagerPage.flow.test.tsx`
- `apps/web/src/pages/Workbench/` 下相关状态、几何与组件测试

**执行：**

1. 保留现有 MSW 基础设施。页面集成尽量在 API 边界描述响应；不把一页所有 query hook、权限 hook 和 store 同时替换后宣称验证真实业务链路。
2. 将 data-manager 的 URL 解析/序列化、视图选择、前后退状态等纯规则交给对应状态模块测试。页面流程只保留代表性的进入、切换、恢复、权限拒绝与失败反馈。
3. 路由测试可以 mock 重型子组件，但应明确它只保护路由/装配契约。授权测试使用符合现行 `employee + 项目职责` 的数据，不能依赖旧平台角色造出非真实状态。
4. 为需要集成的测试提供小型 `render` / QueryClient / Router / auth fixture；QueryClient 等可变实例按测试隔离。不要做一个包含所有页面状态的万能 render helper。
5. 新增或改造的集成测试对意外 API 请求失败；通过 MSW 的明确过滤规则放行已知非目标资源。存量 `warn` 分批迁移，不是一刀切后继续堆全局 stub。
6. 不再把扩大全局异步等待作为常规修复。先检查错误 mock、未清理的 timer、串行前置请求和未等待的状态；确需慢等待的操作在局部注明原因。
7. 保留 Konva DOM 替身的局限说明。几何计算、命令和状态使用低层测试；真实 canvas/WebGL、焦点与指针交互保留浏览器验证。
8. 清理覆盖率配置的历史流水账。当前 `lines/statements/functions=45`、`branches=70` 先保持，调整排除范围必须解释口径变化，不能通过扩排除项或自动降阈值补绿。

**验收：**内部模块重命名或提取不再要求批量补 mock 字段；保留测试能检测 URL 状态被覆盖、项目能力错误、异步结果串写；未处理的目标 API 请求能明确失败。

### 5.3 后端测试具体处理

**修改范围：**`apps/api/tests/conftest.py`、`apps/api/tests/factory.py`、`apps/api/tests/` 及必要的测试配置。

**执行：**

1. 盘点 `_install_legacy_class_kwargs_shim()` 所支持的旧 `classes/classes_config/attribute_schema` fixture，迁移为真实 `tool_bindings` 数据后删除 shim。
2. 先迁移测试再判断生产的 `coalesce_legacy_into_tool_bindings` 是否可删。若生产导入、迁移或兼容接口仍使用它，保留在明确的兼容边界；不能因为测试不再调用就删除。
3. 所有 API 测试统一使用实际绑定事务的 `httpx_client`；迁移 `httpx_client_bound` 使用方后删别名，修正文档。
4. 用户 factory 只创建平台身份；项目成员和职责显式创建。保持当前 fixture 不暗中授予 membership 的正确设计。
5. 纯规则测试不必启动数据库；验证 SQL、行锁、提交时序和 API 依赖注入的测试保留真实数据库。先按现有测试布局标记与选择，不为“整齐”全仓搬文件。
6. 共享 session 的 API 测试不能证明独立事务可见性。通知提交顺序、撤权竞争、锁顺序至少用独立连接/事务验证，不能都放在一个 SAVEPOINT 中。
7. 将大段重复的创建用户/项目/成员/任务操作收进小型 factory；将真正独立的权限组合参数化并赋予可辨识 case ID。不要用嵌套循环制造难以定位的巨型测试。
8. 保留工作树模式与测试库安全检查。默认连接解析的宽泛 fallback 应明确记录/报错；涉及迁移和写入时再次确认是获准的 disposable test 数据库。
9. 清理缓存必须可靠，不能用无说明的 `except Exception: pass` 掩盖污染；确认是可选依赖还是必要 cleanup 后分别处理。

**验收：**不存在测试专用 ORM 构造器补丁；测试使用真实模型字段；纯规则测试可独立运行；事务/并发保护未被 mock 掉；fixture 中没有暗含的项目授权。

### 5.4 优先处置清单

| 位置                                                                                                 | 决定                      | 处理原则                                                                 |
| ---------------------------------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------ |
| `apps/web/src/pages/Projects/ProjectDataManagerPage.flow.test.tsx`                                   | REWRITE / MOVE_DOWN       | 保留导航与 URL 恢复行为；减少内部 hook 协议模拟                          |
| `apps/web/e2e/tests/video-issue-context.spec.ts`                                                     | KEEP + 简化准备           | 保留真实的项目职责与任务交接约束；非目标用户创建使用统一 fixture         |
| `apps/web/e2e/helpers/video-request-errors.ts` 与图片 spec 内联分类（落点 `request-errors.ts`）      | MERGE                     | 一处定义可接受取消；HTTP 错误和写入失败仍明确失败                        |
| `apps/web/scripts/video-request-errors.test.ts`                                                      | KEEP / 扩展               | 为共享错误分类保留允许与禁止两侧的边界测试                               |
| `apps/web/e2e/tests/raster-mask-native.spec.ts`                                                      | KEEP / 重分层             | readonly 与 native 是不同配置契约；不能仅因同文件多次执行就删一份        |
| `apps/web/e2e/tests/mask-advanced-operations.spec.ts`                                                | MOVE_DOWN + KEEP 核心链路 | 算法/组合下沉；真实编辑→提交→刷新链路保留                                |
| `apps/web/e2e/tests/native-mask-ai.spec.ts`；`apps/web/e2e/tests/video-tracker-local-review.spec.ts` | KEEP 关键专项             | 明确使用真实还是替代 backend；不把替代服务验证称为真实模型验证           |
| `scripts/plan-e2e-suites.test.mjs`                                                                   | KEEP / 扩展               | 选择器是 CI 门禁逻辑，必须保护 docs-only、共享代码、配置与未知路径等情况 |
| 后端项目授权、审核证据、事务、幂等与 worker 终态测试                                                 | KEEP                      | 文件级盘点后建立对应关系；不因测试多而删除                               |
| Python SDK 与 ML 示例协议测试                                                                        | KEEP / 精确触发           | 这些覆盖独立消费者，不等价于前端或 API 内部测试                          |

## 6. CI E2E 的目标策略

### 6.1 保留现有骨架，改变验证内容和触发范围

继续使用：

- `scripts/plan-e2e-suites.mjs` 作为套件选择单一入口。
- `scripts/plan-e2e-suites.test.mjs` 保护选择逻辑。
- `.github/workflows/e2e-run.yml` 运行选定矩阵。
- `.github/workflows/ci.yml` 的 `Frontend E2E` 作为稳定汇总结果。

当前 planner 已经使用声明式数组，不再新增一套平行的 suite registry。可在原有声明上增加 scope、配置指纹、必需依赖和选择原因；真正复杂后再提取数据模块。

### 6.2 三层浏览器验证

| 层级       | 内容                                                                        | 触发                                               |
| ---------- | --------------------------------------------------------------------------- | -------------------------------------------------- |
| 核心 smoke | 登录/项目进入、代表性标注保存与刷新、任务提交审核、核心权限拒绝、工作台切换 | 涉及应用运行行为的 PR；纯文档变更不强制            |
| 受影响专项 | 图片/视频/点云、Mask 只读与创建、AI 候选/追踪、离线、成员交接、Issue 等     | 按职责与依赖映射；公共依赖触发相关全部专项         |
| 扩展验证   | 全功能矩阵、visual、layout-stress、需要特定渲染或服务环境的资格验证         | 定时、显式全量、合并后；高风险改动在 PR 中提前执行 |

**不能把整个现有默认套件改名为 smoke。** smoke 只保留最小贯通链路；其余先下沉或划入专项，再减少 PR 默认执行量。

### 6.3 路径选择规则

| 变更范围                                             | 必选内容                                             | 不默认全跑的内容                   |
| ---------------------------------------------------- | ---------------------------------------------------- | ---------------------------------- |
| 仅当前产品文档、措辞、非可执行说明                   | 文档构建、相关文档校验                               | 应用 E2E                           |
| `docs-site/dev/examples/` 的可执行示例               | 对应示例协议测试、文档检查                           | 无关的完整画布矩阵                 |
| `apps/web/src/pages/Workbench/` 状态/命令            | 核心 smoke + 对应工具/渲染专项；共享状态变更保守扩展 | 与本次变更无依赖的视觉全矩阵       |
| 权限、成员、审核与相应前端入口                       | 后端完整相关安全集成 + 角色/审核/撤权浏览器链路      | 无关视觉场景                       |
| Mask schema / 持久化 / capability                    | readonly、native、AI-native 相关配置契约与状态集成   | 不能按“同一 spec”简单去重          |
| API 合同、生成类型、共享客户端、通用 UI 基础组件     | 扩展影响范围；通用客户端/授权可选全核心专项          | 不能只按调用文件名猜测单一页面     |
| 布局、主题、字体、公共样式                           | 核心交互 + 相关 visual；全局布局变更加 stress        | 不能把样式 PR 全视为低风险         |
| `packages/python-sdk/`                               | SDK/CLI/TUI 及相关 API 合同                          | 无 API 变化时无关的画布 E2E        |
| `.github/workflows/`、运行环境、planner、lockfile 等 | planner 测试 + 保守范围；环境变更验证全功能选择      | 不允许因为选择器自身出错而空跑成功 |
| 未知路径、无法解析 diff、重命名/删除的影响不明       | 保守执行或明确失败                                   | 不静默降为 docs-only               |

纯文档分类必须是白名单，不把包含脚本、示例程序、配置或生成源的整个 `docs-site/` 目录一概跳过。保留现有对删除/新增路径与特殊文件名的稳健处理。

### 6.4 数据与服务隔离

当前 `workers: 1` 有明确理由：fixture 的 seed/reset/teardown 会删除共享命名空间。第一阶段保留；不能为了缩短 CI 直接开启多 worker。

改造 fixture 时：

1. 每个测试获得自己的项目/任务/用户标识，必要时结合 run、suite、worker、test、retry 标识构造隔离命名空间。
2. cleanup 只删除该 fixture 创建的资源；重试能独立重建；失败证据先保留再清理。
3. 涉及全局配置或不能并存的数据操作继续独立数据库/串行执行，并说明例外。
4. 隔离验收完成后再测 worker 并发收益；保留不共享服务的 CI shard 边界。
5. 用户登录、创建项目、上传素材不是每个测试的目标：优先通过受保护 seed/API fixture 准备；至少保留对应功能自身的一条真实 UI 完整流程。

### 6.5 交互与错误断言

**交互：**以可见业务结果为主；普通按钮优先语义 locator 和正常点击。画布几何操作允许专门坐标 helper，需统一变换与就绪条件。`force`、直接 DOM 事件和注入 store 只能用于明确测试边界，不作为“用户操作一定成功”的证据。

**等待：**等待目标请求、状态或可见结果；不依赖固定 sleep、全站 `networkidle` 或不断加大全局 timeout。长连接和媒体请求不能成为“整页必须完全安静”的前提。

**请求卫生：**共享分类器至少区分用户触发的合法取消、期望的业务错误、意外 HTTP 错误、写入失败、JS 异常。允许项必须限定方法、路径、失败类型，必要时绑定明确的离开/切换阶段。现有 heartbeat 等例外要逐项保留或收紧，不能从“某些 POST 可取消”推导“所有写请求可忽略”。

新增 API 时，不应到多个 spec 各补一份白名单；但也不能用一个宽泛正则把新故障吞掉。分类器必须同时测试“允许”和“邻近但不允许”的路径/方法。

### 6.6 构建、时间预算与可观测性

- 对相同 SHA、构建 mode、环境变量和生成输入的 `built` suites 构建一次并复用产物。不要复用 production 与 `e2e` mode 不同的包；Mask 的隔离 dev-server 配置不能未经验证直接改用同一产物。
- `pnpm build` 本身包含 TypeScript build；CI 另有 typecheck 和 pre-commit。先列出各检查职责，合并确定重复的工作，不直接删除所有独立检查。
- 当前有 15 分钟 Playwright 全局、20 分钟执行步骤、30 分钟 job 的不同上限。报告要明确究竟是哪一层超时；不要把接近上限的 shard 一概当作 flaky。
- 当前矩阵已经 `fail-fast: false`，保留。`maxFailures: 1` 导致该套件余下用例未执行，应在统计中与失败分开；诊断复现可临时放宽，不能把未执行当通过。
- 保留 trace、截图、API 日志和 JSON 报告。汇总增加失败 test ID、首次失败原因、retry 结果、准备/构建/执行耗时以及计划中未完成的 suite。
- 重试可以保留一次用于诊断，但 retry 后通过单独计为 flaky。先治理核心，再要求核心不得靠重试过门禁；不得默默提高次数。
- `failOnFlakyTests` 只在实际锁定的 Playwright 版本支持时使用。`package.json` 的范围不是已安装版本证据；否则从已有 JSON 报告对核心 flaky 做明确门禁。

### 6.7 安全切换与完整验证

按顺序切换，而不是一天删除全部旧 E2E：

1. 新选择策略先只输出选择原因，与旧策略对照；当前门禁不放松。
2. 完成测试下沉与 fixture 改造后，比较旧、新覆盖的业务契约，并对关键漏选风险做缺陷注入验证。
3. 在同一批候选 SHA 上运行新核心/专项；旧全量保留为对照。验证结果差异必须解释。
4. 保留 `Frontend E2E` 稳定名称；新执行方式接通后再调整实际分支保护。纯文档不执行应用 E2E 时，汇总确认“允许跳过的原因”，而不是接受任意 `skipped`。
5. 定时/手动入口明确区分 `extended-only` 与 `full`，不要继续用事件名称隐含不同覆盖范围。读取实际调用 workflow 后同步修改。
6. 发布或推广候选必须有该候选 SHA/产物的完整验证记录；不能拿“昨晚 main 绿过”代替本次候选。没有现成发布自动化时先记录为发布准入规则，不声称已存在该 gate。
7. 新策略出现漏选或不可解释差异，恢复原全量选择；不回滚已验证的产品修复和安全约束。

确需临时隔离 flaky 用例时，记录责任人、具体问题、恢复条件和最长保留期限；权限、数据正确性等核心约束只有存在等价保护时才能移出门禁。不能把隔离区变成永久垃圾桶。

## 7. 注释、历史兼容与文档清理

### 7.1 优先清理位置

| 位置                                                                   | 清理动作                                                                                               |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `apps/api/tests/conftest.py`                                           | 删除头部版本历史与“解锁旧测套”叙事；说明真实事务绑定、身份 fixture、隔离前提；删 shim 后删对应历史说明 |
| `apps/web/vite.config.ts`                                              | 删除历史 case 数、旧覆盖率快照和逐版本阈值变更；保留当前 coverage 口径、代理和 Worker 的约束           |
| `apps/web/vitest.setup.ts`                                             | 去掉版本前缀；保留 Konva 替身局限、环境补丁原因；异步等待说明与迁移后的配置一致                        |
| `apps/web/playwright.config.ts`                                        | 去掉点云等说明的版本前缀；保留软件渲染、X11、真实 GPU 资格验证与工作树隔离条件                         |
| `apps/web/src/pages/Workbench/state/useWorkbenchShellModel.helpers.ts` | 删除“从某版本逐字搬运/第几批拆分”；改成每个函数当前语义、坐标约定和边界                                |
| `apps/api/app/workers/signals.py`                                      | 删除版本号；准确描述信号兜底范围与幂等/终态规则，不将其写成能覆盖所有进程崩溃的保证                    |
| `.github/workflows/ci.yml`；`.github/workflows/e2e-run.yml`            | 删除旧版本起点；说明当前检查目的、产物与服务约束；无法核实的历史外部服务状态不作永久事实               |
| `CLAUDE.md`（`AGENTS.md` 的共享源）                                    | 将“历史 provenance 放入注释”的建议收敛为 Git/CHANGELOG/ADR，避免把删掉的版本历史再次塞回源码注释       |

### 7.2 特别复核：迁移注释和真实行为

`ci.yml` 的 round-trip 通过 `scripts/alembic_reversible_floor.py` 找到可回退位置，再 `alembic stamp`。**stamp 修改的是迁移版本记录，不等于执行了真实 schema/data 回退。**

当前说明将跳过段视为新库上的 data-only no-op；近期提交还修改了员工角色迁移的数据库默认值。实施时必须重新核对该前提，不允许仅修改注释使风险看起来消失。

将迁移验证拆清楚：新库完整 upgrade；受支持可逆段的真实 downgrade/upgrade；不可逆转换的前向数据断言与备份恢复演练。必要时使用独立的新库分别构造起点。**不得在开发/生产上通过 stamp 伪造回滚成功，也不在本次纯重构中重写已发布迁移历史。**

### 7.3 必须保留的内容

保留实际 API 路径与协议版本、依赖版本、锁文件、数据库 `revision/down_revision`、迁移不可逆标记、许可与版权、安全说明、当前运行环境要求。保留 `CHANGELOG.md`、历史 ADR 和归档计划中的真实历史，不把它们改写成“从未发生过”。

删除的是活动代码和当前说明中不再帮助理解的“某版本新增/从某文件搬来/以前如何”的叙事。历史兼容代码只有确认不再服务任何受支持入口后才能删除。

### 7.4 可解释性文档

优先更新已有 `docs-site/dev/concepts/` 和 `docs-site/dev/how-to/` 中同主题页面；缺失时分别拟新增 `repository-map.md` 与 `testing-strategy.md`。

架构说明至少包含：模块职责及允许依赖；平台身份/项目能力真值来源；进入工作台→查询权限→取得任务→编辑→提交→异步处理→通知的调用链；状态与副作用归属；测试层级与运行入口。

每条调用链给出实际文件/符号，不堆目录树，不复制代码，不维护第二套角色矩阵。重大边界变化使用 `docs/adr/` 记录原因；删除/重命名符号同步更新 Markdown 引用。

## 8. 分阶段执行工作包

每个工作包对应一个或多个小 PR。每个 PR 只处理一个明确职责，允许并行盘点，不允许多个写入者在同一工作树重构相同公共模块。以下顺序不代表耗时承诺。

### P0 — 建立全仓清单与质量基线

**依赖：无。**

范围包括所有 `git ls-files` 管理的业务源码、测试、脚本、配置和当前文档；重点包含 `apps/api/`、`apps/web/`、模型 backend、`packages/`、`scripts/`、`.github/`、`docs-site/`。模型权重、依赖、构建产物和自动生成文件分类记录，不手工重构生成输出。

执行：记录当前 HEAD；按目录盘点 owner、入口、依赖、重复规则、历史兼容和未使用候选；导出测试清单；抽取最近至少 20 次相关 CI 的实际结果，区分产品/测试/环境/超时/证据不足以及取消和未执行。

**交付与验收：**一个简洁改造台账和 before 基线；每个目录有“需改/已合理/生成产物/不在范围”的结论。不能只有几个热点文件。每项删除候选有引用搜索和运行入口检查，不凭名称删除。

### P1 — 固化现行业务契约与有效回归

**依赖：P0。**

落实第 3 节约束到具体测试；修正仍使用旧平台角色的测试数据；将真实产品缺陷与测试基础设施问题分开。对照 PR #129，重点复核权限变更、审核证据、事务通知、离线重放。

**验收：**核心保护清单无未映射项；必要的负向验证证明保护有效；没有为了迁移测试而恢复旧角色行为。

### P2 — 后端 fixture 与重复测试清理

**依赖：P1。**

完成第 5.3 节。按领域迁移旧字段构造，删除 ORM shim 和旧客户端别名，拆分纯规则与真实集成，收敛 factory。

**验收：**受影响后端测试及完整后端检查通过；补丁删除后没有隐式兼容依赖；数据库隔离、行锁和事务可见性验证仍有效。

### P3 — 前端测试边界与高 mock 流程清理

**依赖：P1，可与 P2 在独立工作树进行。**

完成第 5.2 节，先以 data-manager 流程测试为样板，再按清单推广到其他页面。复用现有 MSW 和 `src/test/`，不建立第二套模拟服务。

**验收：**目标流程保留；纯规则下沉；页面 test 不再依赖与测试目标无关的内部 hook 细节；覆盖率口径变化可解释，阈值不因失败自动放宽。

### P4 — 后端规则归属与作业分派

**依赖：P2。**

以 `project_access.py` 为授权真值，检查路由、worker、导出、通知与离线相关入口是否仍重复推导权限。以 `signals.py` 为第一处封闭分派改造，明确公共事务骨架与领域终态差异。

**验收：**未知角色拒绝、撤权重检、锁顺序和终态幂等不变；不存在第二套权限矩阵；`partial` 与 `rollback_failed` 未被通用 handler 抹平。通知发布方式按调用方当前契约处理，不机械全仓替换。

### P5 — 工作台职责收敛

**依赖：P3；涉及后端约束的部分依赖 P4。**

按第 4.4 节依次提取纯策略、连接现有领域模块、迁移闭包、副作用与局部 UI。每次只迁一个功能域，补调用图并删除过渡层。

**验收：**装配 model 不重新定义权限、Mask 恢复、任务导航等规则；切换/取消/拒绝/刷新回归通过；无新增循环依赖；不是仅把原逻辑搬到一个更大的 helper。

### P6 — 全仓剩余重复、死代码与注释清理

**依赖：P2—P5 中相关模块已稳定。**

按 P0 台账扫完其他 API/service、页面、SDK、模型 backend、脚本和配置。逐项合并相同语义逻辑；删除无调用实现、废弃 fixture、旧导出和多余转发；完成第 7 节。

**验收：**候选项逐一关闭或说明保留原因；动态路由、反射加载、CLI 入口、worker 注册、测试发现和文档引用均检查；没有“静态没 import 所以删除”的误判。

### P7 — E2E 用例下沉与 fixture 隔离

**依赖：P1；与 P4/P5 按不冲突领域并行。**

统一请求错误分类和数据准备；把几何/权限穷举/状态组合迁到低层，保留真实用户关键链路。按运行配置比较 default 与各 Mask suite 的收集、实际执行、skip 和行为差异。

**验收：**可独立运行并重试；没有跨测试依赖；删除项有替代映射；配置的正反两面均被覆盖。未完成隔离前保持单 worker。

### P8 — CI 选择、构建复用与报告

**依赖：P7。**

在现有 planner 和测试上实施第 6 节策略；提取真正重复的服务准备步骤；同构建条件产物复用；完善失败分类和首次执行指标。对照检查 backend、SDK、示例、前端、文档和迁移的触发关系。

**验收：**planner 对 docs-only、共享代码、删除/重命名、空/异常 diff、未知事件等均有明确测试；非法选择不空跑；相同构建条件不重复构建；不同配置不错误共享产物。

### P9 — 影子验证与门禁切换

**依赖：P8。**

新旧策略在候选提交上对照；确认核心和专项的缺陷检出能力；切换 `Frontend E2E` 内部执行逻辑；同步实际调用 workflow 和分支保护配置。关键范围漏选即恢复保守全量选择。

**验收：**核心不依赖 retry 才通过；所有计划必需 suite 有结果；跳过原因可审计；全量与发布候选验证有明确入口；现有检查名称没有被意外破坏。

### P10 — 完整验收与关闭台账

**依赖：P0—P9。**

执行所有受影响验证、完整后端/前端/SDK/协议与文档检查、全功能 E2E 和必要渲染专项。记录 before/after：重复规则数、旧兼容入口数、关键模块职责、测试层级分布、首次通过率、失败类型、执行和准备耗时。

**验收：**第 10 节全部完成。不以“完成了大部分热点”结束，也不为追求删行数制造新抽象。保留项必须有明确理由，未完成项不能标记为完成。

## 9. 执行命令与记录要求

以下命令从仓库根目录执行，使用现有脚本。依赖、工作树配置与获准的测试服务须已就绪；这是实施入口，不表示本次审阅已经执行。

### 9.1 基线与只读检索

```bash
git rev-parse HEAD
git status --short
git diff --check
git ls-files apps packages scripts .github docs-site > /tmp/aap-source-files.txt

# 仅检索，不自动删除。
rg -n 'httpx_client_bound|_install_legacy_class_kwargs_shim|coalesce_legacy_into_tool_bindings' \
  apps/api
rg -n 'waitForTimeout|force: true|dispatchEvent|ERR_ABORTED' apps/web/e2e
rg -n 'v[0-9]+\.[0-9]+|逐字搬运|旧测套|历史:' \
  apps/api/app apps/api/tests apps/web/src apps/web/vite.config.ts \
  apps/web/vitest.setup.ts apps/web/playwright.config.ts .github/workflows

node --test scripts/plan-e2e-suites.test.mjs
```

`rg` 命中是人工审阅候选，不是违规证明；没有命中时其退出码也不能被误当成项目失败。

### 9.2 前端与文档

```bash
pnpm --filter @anno/web test src/pages/Projects/ProjectDataManagerPage.flow.test.tsx
pnpm --filter @anno/web test:coverage

OPENAPI_URL="$(pwd)/apps/api/openapi.snapshot.json" pnpm --filter @anno/web build
pnpm --filter @anno/web size
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint

pnpm format:check
pnpm lint
pnpm docs:build
node scripts/check-workflow-names.mjs --strict
git diff --check
```

代码提取时先运行受影响测试；最终验收再执行全套。不要在没有新变更或新风险时反复重跑已经完成的检查。

### 9.3 后端与 E2E：使用隔离工作树运行入口

```bash
# 先按仓库运行规范确认 test 模式连接的是允许写入的隔离测试库。
pnpm dev:worktree -- exec --mode test -- bash -lc \
  'cd apps/api && uv run pytest -q --durations=30 --junitxml=pytest-results.xml'

# 清单对比：配合对应矩阵环境变量分别收集，不把 --list 当成通过证据。
pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter @anno/web test:e2e --list

# 代表性专项；执行前确认 E2E 模式服务与数据库隔离。
pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter @anno/web test:e2e e2e/tests/video-issue-context.spec.ts --project=chromium

pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter @anno/web test:e2e:mask-readonly

pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter @anno/web test:e2e:mask-native

pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter @anno/web test:e2e:mask-ai-native
```

涉及数据库迁移、独立事务、worker 或模型服务的检查，以其当前运行手册为准。不要复制 CI 临时库操作到共享开发环境；不要把 mock backend 的成功记录为真实模型或 GPU 验证。

### 9.4 每个 PR 的最小记录

```text
改了什么业务职责：
涉及的现有模块及调用方：
合并/删除了哪些重复规则或过渡代码：
保留的业务约束：
测试处理：KEEP / MERGE / MOVE_DOWN / REWRITE / DELETE，及对应理由：
本地实际执行的命令、结果、HEAD：
浏览器实际验证的链路：
远程 CI 结果与首次失败分类：
未验证或未完成内容：
回退边界：
```

测试数量降低不是验收证据；“build 成功”也不等于授权或用户流程正确。报告分别标明静态检查、单测、真实集成、浏览器、远程 CI。

## 10. 最终完成标准

### 代码结构

- [x] 全仓台账覆盖所有受管理源码目录；非热点目录也有明确处理结论。— [26] §2/§12：TSV **1139 行 = 1135 可执行 + 4 测试支撑**，`git ls-files` 双向比对 0 缺口；`reason` 为具名/区域级保守保留理由。
- [x] 同一业务规则只有一个权威实现；确需跨语言实现的规则由共享合同/数据样例校验，而不是假装能直接共用函数。— [32]：`project_access.py` 单一授权源 + OpenAPI 快照合同；`export_openapi.py --check` 通过。
- [x] 工作台装配、领域状态、命令、副作用与视图边界清楚；主要调用链有文件/符号级说明。— [33]：model 8919→6646，六条规则下沉；`docs-site/dev/concepts/repository-map.md`。
- [x] `signals.py` 等封闭分派已去除确认的重复骨架，特殊终态语义保留。— [32]/[28]：`signals.py` 121 行 + `async_job_terminal.py`；`partial`/`rollback_failed`/`cancelled` 保留。
- [x] 无新万能 helper、平行权限系统、无必要包装层或循环依赖。— [33]：P5 逐行等价核对 + 有据 KEEP。
- [x] 无用途的旧 shim、别名、导出、fixture 和历史兼容已实际删除；保留兼容入口有受支持消费者证据。— [29]：ORM shim 与 `httpx_client_bound` 删除；`coalesce_legacy_into_tool_bindings` 因生产路由仍在用而保留在兼容边界。

### 测试

- [x] 每项关键业务约束均有对应测试；删测试有替代或失效依据。— [28]：C1–C8 无未映射项，P0 两处 GAP 已补。
- [x] 前端测试不再靠旧角色数据或大规模无关 hook mock 维持通过。— [30]/[41]：27 个旧角色文件修正，data-manager 改 MSW 边界；562 文件/5626 用例通过。
- [x] 后端测试直接使用现行模型，不全局替换生产 ORM 构造器。— [29]/[40]：43 处旧 kwargs 迁 `tool_bindings`，shim 删除；4428 collected / 0 failed / 0 errors / 15 skipped。
- [x] 安全、并发、事务、幂等、离线及渲染保护没有因精简丢失。— [29]/[31]/[32]/[42]：独立事务/锁序/撤权/离线测试保留；渲染资格 [42]。
- [x] 覆盖率口径、排除项和阈值可信；没有为绿灯自动调低门槛。— [30]：阈值 45/45/45/70 未变，删除逐版本流水账并记录排除口径。

### CI E2E

- [x] PR 核心、受影响专项、全量扩展职责明确；当前 7/9 条目默认策略已按验收结果替换。— [43] 最终：planned 选择 + 必需 suite 审计 + `core-flaky` 门禁；`E2E_SELECTION_MODE=legacy` 回退。
- [x] fixture 清理不误删其他测试资源；未完成隔离的部分仍保持安全串行。— [31]：seed owned 命名空间 + 邻居探针；`workers:1` 保留。
- [x] 请求取消判定集中且有负向测试；写入失败、意外 HTTP 错误不被宽泛忽略。— [31]：`apps/web/e2e/helpers/request-errors.ts` + `apps/web/scripts/video-request-errors.test.ts`（16 例允许/禁止双侧）。
- [x] 首次失败、flaky、超时、环境失败、取消和未执行分别统计。— `scripts/summarize-e2e-results.mjs` + 单测；**限制**：真实远程 CI 未运行，分类器仅在本地探针与单测上验证。
- [x] 同构建条件复用产物；不同 mode/环境不误用缓存。— `e2e-run.yml` 指纹 + `SHA256SUMS`；本地建/验/篡改拒绝探针；**限制**：远程 CI 未运行。
- [x] 汇总检查稳定；必需 suite 缺失不能通过；纯文档跳过有明确理由。— [36]/[43]：`audit-e2e-requirements.mjs` fail-closed（冻结 898 实跑审计 exit 1，composed 修正门 exit 0）。
- [x] 定时全量与候选发布验证入口明确，存在保守回退方案。— [43]：`E2E_SCHEDULE_SCOPE=full`、dispatch `scope`、`E2E_SELECTION_MODE=legacy`；候选发布记录见 [45]。**限制**：无远程 runner 观察。

### 注释与文档

- [x] 活动源码/测试/CI 中的无用版本叙事已清理；没有把它们从文档转移回注释。— [34]/[35]：§7.1 指定文件命中 36→0；`CLAUDE.md` provenance 指导收敛（历史放 Git/CHANGELOG/ADR，不放源码注释）。
- [x] session 绑定、覆盖率、渲染限制、迁移回退和信号兜底等说明与实现一致。— [29]/[30]/[39]/[42]/[28]。
- [x] API/协议版本、迁移元数据、锁文件、许可证和真实历史被保留。— [26]/[29]/[39]：迁移 `revision/down_revision` 与锁文件未改写。
- [x] 代码引用、运行命令、架构文档和共享仓库指引同步，文档构建通过。— P10：`pnpm docs:build` 实际通过；本计划附录的 `request-errors.ts` 路径已更正；三个临时 preview 配置已移除。

**只有上述结果落地、验证完成、台账关闭，才能称为本次优化完成。不能把减少文件行数、删掉一批测试或 CI 偶然变绿当作完成。**

> **P10 收尾（2026-09-21）**：上述 22 项按实际证据勾选；唯一跨项限制是**远端 CI 未运行**（无 push），`scripts/summarize-e2e-results.mjs` 的分类与同构建复用仅在本地探针/单测上验证；严格 WebGPU/WebCodecs 资格由渲染器通道 [42] 单独承担并已接受，不是本清单的缺口。台账与逐项证据见 `docs/research/45-repository-optimization-final-acceptance.md`。

## Outcome

- Landed commits: P0–P10 已集成到本计划的最终验收根 `13d274326`，最终文档与清理提交记录见 `docs/research/45-repository-optimization-final-acceptance.md`。
- Release milestone: 相关用户可见修复与工程变更已进入 `0.26.0`；本计划不单独定义版本范围。
- Developer documentation: `docs-site/dev/testing.md`、`docs-site/dev/concepts/repository-map.md` 以及 `docs/research/26-repository-optimization-baseline.md` 至 `docs/research/50-repository-optimization-workbench-root-edge-dock.md`。
- CHANGELOG: 用户可见修复与 CI / 测试行为变更已记录在根 `CHANGELOG.md` 的 `0.26.0` 段。
- Remaining work: 无计划内未完成项；远端 CI 因未 push 而未运行，硬件视频解码未测量，这两项是已记录的验证边界，不是隐含完成声明。

## 附录：本计划的主要核对入口

| 主题                         | 仓库内证据                                                                                                                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 当前工程规则、文档与计划命名 | `CLAUDE.md`；`docs/plans/README.md`                                                                                                                                                              |
| 已合入的项目角色体系         | PR #129；`docs/plans/1789807315_project-scoped-employee-roles.md`；`docs/adr/0075-platform-identity-vs-project-authority.md`；`apps/api/app/services/project_access.py`                          |
| 近期 CI 与测试契约调整       | 提交 `cbd8d62071a2b4efc60f1acfde88f160ef653ac8`                                                                                                                                                  |
| 近期真实业务评审修复         | 提交 `aa1c879cb8eae510737c5e05eab75332100af0eb`                                                                                                                                                  |
| CI 与套件选择                | `.github/workflows/ci.yml`；`.github/workflows/e2e-run.yml`；`scripts/plan-e2e-suites.mjs`；`scripts/plan-e2e-suites.test.mjs`                                                                   |
| 浏览器配置与执行入口         | `apps/web/playwright.config.ts`；`apps/web/package.json`                                                                                                                                         |
| 前端测试环境与覆盖率         | `apps/web/vitest.setup.ts`；`apps/web/vite.config.ts`                                                                                                                                            |
| 前端流程测试耦合样本         | `apps/web/src/pages/Projects/ProjectDataManagerPage.flow.test.tsx`                                                                                                                               |
| 工作台状态边界               | `apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx`；`apps/web/src/pages/Workbench/state/useWorkbenchShellModel.helpers.ts`；`apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` |
| 后端测试兼容与事务说明       | `apps/api/tests/conftest.py`                                                                                                                                                                     |
| 作业终态重复分派             | `apps/api/app/workers/signals.py`                                                                                                                                                                |
| E2E 请求错误分类             | `apps/web/e2e/helpers/request-errors.ts`（集中分类器；原 `video-request-errors.ts` 已并入）；`apps/web/scripts/video-request-errors.test.ts`                                                     |

外部方法依据为 Playwright 官方的用户可见行为、测试隔离、locator 与 flaky 配置说明。具体选项以仓库 lockfile 对应版本为准；本文不引入任何外部链接作为仓库导航。

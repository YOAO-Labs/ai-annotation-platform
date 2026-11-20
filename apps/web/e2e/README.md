# E2E 测试（Playwright）

完整跨服务的端到端测试，验证「前端 + FastAPI + Postgres + Redis + MinIO」整条链路。

## 何时写 E2E

- 用户关键路径：登录、创建项目、提交标注、批次审核、导出
- 跨页面流程：项目创建 → 任务分配 → 标注 → 审核
- 涉及 WebSocket / 文件上传 / 长流程的功能

**不要**用 E2E 测试组件细节或纯逻辑——这些用 vitest 单测覆盖。

## 本地运行

```bash
# 只需先起共享 PostgreSQL 和 MinIO
docker compose up -d postgres minio

# 从仓库根目录使用归属核验的 e2e 环境
pnpm dev:worktree -- exec --mode e2e -- pnpm --filter web test:e2e
pnpm dev:worktree -- exec --mode e2e -- pnpm --filter web test:e2e e2e/tests/auth.spec.ts
pnpm dev:worktree -- exec --mode e2e -- pnpm --filter web test:e2e --headed
pnpm dev:worktree -- exec --mode e2e -- pnpm --filter web test:e2e --ui
pnpm dev:worktree -- exec --mode e2e -- pnpm --filter web test:e2e:stress

# Mask native 独占入口自行准备并监督 e2e 环境，不要嵌套 dev:worktree exec
pnpm --filter web test:e2e:mask-native
```

首次运行需要 `pnpm exec playwright install chromium` 装浏览器。

CI 使用 `line` reporter 显示正在执行的用例及重试，同时保留 GitHub annotations、HTML 和 `e2e-results.json` 报告。Actions 摘要分别列出每个套件通过、失败、重试后通过（flaky）、跳过数量及执行时间。每次失败尝试保留截图和 trace；成功路径不保存截图或对比图片基线。报告缺失时应检查启动或超时日志。分片内按单 worker 串行运行；排查长时间执行时先查看当前用例和超时信息，不能只凭 WebSocket 断连日志判断测试卡死。

CI 核心 smoke 不重试，完整功能和压力用例最多重试一次。首个用例最终失败后终止当前分片，其他分片继续完成各自诊断。每个 Playwright 测试进程总限时 15 分钟，测试步骤限时 20 分钟，整个 E2E job（含安装和构建）限时 30 分钟。分层限时为清理和报告上传保留余量，并在测试进程无法自行退出时由 Actions 兜底。本地运行不启用这些 CI 早退限制。

## 功能、渲染与压力测试

默认 `playwright.config.ts` 排除 `@stress`；`playwright.stress.config.ts` 只选择 `@stress`，复用服务和数据隔离。压力集的一次诊断重试用于区分确定性损坏与 runner 资源饥饿，重试通过仍标记 flaky。通过可操作控件、布局尺寸、状态流转、实际像素采样和保存结果验证产品行为，而不是比较整页图。需要人工核对外观或提供演示时，按[按需截图与录屏](../../../docs-site/dev/how-to/update-screenshots.md)只操作本次相关流程，不增加采集套件。

Mask 场景用 `@mask-native`／`@mask-readonly` 标签按
`PLAYWRIGHT_RASTER_MASK_MATRIX` 选择，未指定矩阵时不选这两组标签；不要用条件
`test.skip` 把另一部署开关下的场景混入当前选集。`test:e2e:mask-native` 先构建
产品，再用 `playwright.mask-product.config.ts` 验证真实 API 和 Vite preview；
源码 Worker 使用 `playwright.browser-integration.config.ts` 独立报告，
不需要 API、认证或数据库，也不能代替构建产物的保存和恢复证据。
两阶段的 Vite 都由 Raster 启动器直接管理，配置不再自行启动 `webServer`；
只有准确 Node 子进程在绑定端口后发出的私有就绪握手才能接纳 HTTP 检查，
其他进程的 HTTP 200 不算启动成功。收尾通过私有控制管道等待真实
`server.close()` 和准确进程退出，不向整个进程组发信号。
Raster 原生／只读矩阵与源码 Worker 使用独占本地 `BrowserServer`，
不接入外部浏览器；关闭时检查真实 `ChildProcess` 退出结果。
Playwright 的关闭 Promise 成功并不足以通过验收：强制回退导致的信号退出、
非零退出以及关闭请求前已观察到的退出都必须失败。
该入口在启动 API、浏览器前准备专用版本化存储及故障夹具。测试进程仅持有本次
suite 的本地夹具通道和公开 manifest 路径；数据库 owner、连接器密钥与存储写入
凭据只进入启动器、受限 API 和私有夹具进程。通道限制为本 suite 的固定夹具
操作，不提供通用 HTTP 或 SQL；它不是同一系统用户或拥有 Docker 权限的进程之间
的操作系统沙箱。结束时监督整个消费者进程组，并先等待已接纳的夹具工作收尾；
进程或夹具操作未能确认停止时保留精确资源、通道记录和错误，不自动清扫。
故障注入阶段仅保留同一私有夹具控制 owner 及其原始提供方：先封闭夹具变更和提供方
HTTP 准入、确认全部真实请求及跟踪的工作结束并关闭数据库连接，再核对这些进程的准确
身份和完整后代清单；Routing 与健康观测 owner 必须先实际排空并退出。存在未知后代、
连接或未完成工作时停止注入并保留证据，不能把超时或信号当作物理停止证明。
普通 API 由专用启动入口等待已接纳的 HTTP 工作排空及 ASGI lifespan 关闭完成，再以
正常状态退出。API 自行崩溃、信号退出或未完成关闭均阻止后续退休；浏览器失败与后代
收尾失败同时保留。后代进程通过准确 pidfd 逐个发送停止信号；发送前已由父进程
回收的后代不误算为信号退出，实际收到信号却无法取得退出状态的后代仍保持未决。

损坏内容诊断和重试两例仍包含在所选 Mask 矩阵中，但必须先由隔离环境协调器在产品 API、worker 和浏览器启动前，使用专属数据库与版本化存储准备真实缺失对象和健康邻居。协调器通过 `PLAYWRIGHT_RASTER_FAULT_MANIFEST` 提供每个用例及重试各自的 run 与精确标注；缺少或不匹配的清单会直接失败，不会在线破坏共享内容或跳过用例。该串行夹具不代表共享数据库／存储的并发隔离已经通过验收。

布局矩阵在图片/视频/点云 × 标注/审核六种上下文中都保留真实拖动、画布身份、上下文隔离、紧凑模式、保存与刷新恢复断言。功能集执行 14 次重排，`@stress` 执行完整 54 次；新增长循环必须放入压力集，并在功能集中保留覆盖关键状态转换的短流程。

CI 调度规则由根目录 `scripts/plan-e2e-suites.mjs` 维护：

- 普通 PR 使用核心 smoke 加受影响的 Mask、视频、点云等专项；共享依赖、未知路径等高影响变更保守扩大选集。白名单内的纯文档变更显式跳过应用 E2E；`E2E_SELECTION_MODE=legacy` 保留固定全量功能分片和专项的回退入口。
- 主分支和 `E2E extended` 夜间运行（北京时间 03:00）使用全量选集。手动选择 `extended` 仅执行布局压力集；`full` 执行全量选集。
- PR 路径读取失败会使计划 job 失败；`Frontend E2E` 汇总同时要求计划和所有已选套件成功，不会把漏跑当作通过。

`seed` 只用于准备前置数据。核心创建用例必须验证真实 UI 操作、保存响应、API 读回和刷新恢复；不能在 UI 保存失败后调用 `seed.advanceTask` 等接口伪造成功。Canvas 指针坐标应依据当前媒体位置和尺寸计算，并确认没有被浮层遮挡。连续执行相互独立的边缘绘制时，通过真实 UI 收起前一个标注的浮窗，并检查拖拽起点命中画布。截图检查不能替代保存检查，存储状态检查也不能替代真实渲染检查。

Mask 设置统一通过 `fixtures/mask-toolbar.ts` 打开：等待胶囊展开及有限动画完成，再点击更多工具，防止展开期间滚动位置变化使按下和抬起命中不同位置。图片 Mask 重编辑使用 `helpers/mask-selection.ts` 选中准确标注，并通过选中信息卡的可访问按钮进入；不要按 DOM 顺序选择同名按钮或用强制点击绕过命中检查。预测精修按准确预测行和可访问操作进入，避免依赖组件内部按钮 ID。

修改测试分类后先执行各命令的 `--list --reporter=line`，确认标签不会造成漏选；修改 CI 路由时执行 `node --test scripts/plan-e2e-suites.test.mjs`。本地验证结束后按归属清理专用环境数据及本次生成的 `test-results/`、`playwright-report/`、`e2e-results.json`，按需保留脱敏的失败诊断。

本地 E2E 由 `dev:worktree` 创建并核对当前资源根的独立数据库、Redis、存储和
受限 API 账号，自动选择不冲突的 Web/API 端口。不能用一个带 `_test`／`_e2e`
后缀的数据库或手工覆盖 `PLAYWRIGHT_E2E_DATABASE_URL` 代替资源归属核验。
需要并行运行时使用不同工作树及独立运行 UUID；未完成并发资格的夹具族仍须串行，
单个 owned fixture 不代表整套测试可共享数据库或桶。CI 分片使用独立存储服务，
同样需要核验 owner 与 runtime 身份及真实资源归属。

## 数据准备

`e2e/fixtures/seed.ts` 通过私有 Python 客户端调用现有 `/api/v1/__test/seed/*`
router。已核验资源根和模式对应唯一私有 authority，其 Unix socket 与生命周期记录仅
允许 owner 访问；测试 owner 凭据不进入普通产品 HTTP API。authority 保留每个运行的
真实提供方句柄，并在每次请求后关闭数据库连接池。worker 退出只断开客户端，不释放
未退休的提供方；这不是独立 HTTP 部署验收。这组路由与夹具通道需要同时满足：

- `E2E_SEED_ENABLED=true`；
- 当前数据库名以 `_e2e` 或 `_test` 结尾；
- `PLAYWRIGHT_E2E_DATABASE_URL` 与 runtime `DATABASE_URL` 指向同一显式测试库；
- 夹具进程使用启动器注入的资源根，且其 identity、资源清单、数据库 owner 标记和受限 runtime LOGIN 均通过核验；
- `production` 环境永不挂载路由。

authority 的 Unix socket 使用 `/tmp` 下的私有随机短目录，避免工作树 `TMPDIR` 超过
系统 socket 路径长度限制；准确目录、socket 身份和原进程仍登记在该模式的私有记录中。
临时目录的位置不会改变资源归属，不能按目录前缀批量清理。

普通产品 API 使用已核验工作树的受限 `DATABASE_URL`，匹配的
`PLAYWRIGHT_E2E_DATABASE_URL` 只供私有 E2E 夹具进程使用。这些夹具用于功能测试，
不用于截图或录屏；两条连接仍需核对目标及身份。
真实创建／保存请求始终通过受限产品 API；夹具不能在保存失败后伪造结果。
邀请／接管场景先由私有夹具通道登记意图，再经受限产品 API 调用独立 Invitation
能力；原生回放使用同一可销毁数据库中已核验的邀请专用凭据，不给普通 API 连接补
Identity 私表或邀请 schema 权限。创建 owned suite 时，Routing 与健康观测 owner
必须保持存活直到该 suite 清理完成。

**默认使用私有命名空间 fixture**：`await seed.owned()` 按运行 UUID、spec 文件及测试标题
确定性派生命名空间（`^[a-z0-9]{4,12}$`，同次运行的重试稳定），由 `/seed/owned`
只创建该命名空间的标准工作台 fixture，测试结束后 seed fixture teardown 调用
`/seed/owned-cleanup` 精确清理（含存储对象与 mock backend 注册项）；清理失败会让
测试失败。`/seed/lidar`、`/seed/project-roles`、`/seed/filtering` 的命名空间参数
同样只触碰各自命名空间；filtering manifest 暴露 `display_names`、`invitation_emails`、
`search_keys`、`audit_scopes`，spec 必须用 manifest 值做全局列表搜索/文案/导出断言。
filtering 的无源 tracker 候选仅用于列表和预览验证；该夹具未准备已接受输入证据，
不能用于证明 tracker 结果接受或端到端执行。

需要真实大画布原图时，在创建时使用 `seed.owned(undefined, { imageCanvas: "720p" | "5k" | "8k" })`；首个任务的 PNG 字节与尺寸一致，其他任务仍使用默认尺寸。Raster 注入会核对当前 run 的项目、用户和原图，不能通过修改尺寸元数据伪造大图；共享的内容寻址 Mask 对象仍由原有存储与垃圾回收 owner 管理。

评论附件清理按本命名空间的精确项目 UUID 查找独立上传记录，沿记录保存的桶和准确键执行。已完成或已退休的上传可由测试清理明确销毁；未完成上传、未收尾读取、未知存储归属或删除失败会阻止清理，保留记录和原夹具以供重试。不要修改保存期限、清除访问账本或按当前桶手工扫键来让 teardown 通过。确认只是自己的可丢弃工作树环境后，可按 `pnpm dev:worktree doctor --mode e2e` 输出的准确确认值销毁整个 mode。

规范的 `comment-attachments/{annotation_uuid}/{upload_uuid}` 对象只能凭独立上传记录删除；旧前缀清理不会接管同键副本或无记录的规范键。此类残留会明确使清理失败。fixture 用户若仍被命名空间外的评论引用，也不能通过清理该用户来连带删除外部评论。

`/seed/filtering` 与 `/seed/project-roles` 必须显式指定命名空间；不存在重建共享工作台的 reset 入口。
需要跨命名空间存活/清理证据时，可用 `fixtures/storage-object-probe.py` 经真实存储
客户端读取对象字节。

正常结束时 `globalTeardown` 只请求清理本次 suite 清单。
suite 清理先完成全部子运行的原生执行退休，再逐个清理数据；不依赖子运行 UUID 的
排序来决定兄弟 Job 是否已退休，也不放宽未知 scope 的拒绝。
每次实际进入原件读许可等待时单独保留三秒上限；执行退休和物理回收耗时不会挤占
后续子运行的读等待预算。`globalTeardown` 不为整个清理操作设置客户端计时器，
而是等待同一 owner 的实际结果及确认；CI 总限时和外部中断仍可能终止调用方。
读者未结束、监听失败或最终守卫拒绝仍使清理失败，不因等待时间经过而视为已退休。
未结束的操作、未知物理 I/O 或未证明退役的真实 Jobs 会阻止清理并保留其归属证据；
不要按前缀扫桶或删除 Jobs 强行通过。强制中断可能使 teardown 无法执行，应保留
原错误并核对该 run 的实际 owner 状态，而不是重建共享环境冒充精确清理成功。
客户端超时、EOF、异常或中断不代表已接纳操作结束，也不会自动释放模型等待。
私有调用为每次操作分配 UUID；原 authority 在客户端断开后仍保留该 UUID 的进行中或
完整结果。发生传输故障时记录错误中的操作 UUID，重新连接同一 authority，以
`recover` 查询该 UUID，检查实际结果后才发送 `ack`；不要用新 UUID 重放尚未确认的
相同变更。普通 Node 客户端提供 `recoverFixtureOperation` 和
`acknowledgeFixtureOperation`；Python 私有客户端提供对应的恢复和确认方法。
全局 teardown 的可报告错误保留已取得的操作 UUID，即使清理和关闭通道同时失败；
硬中断前未取得 UUID 时仍属未决状态，不能据此推断操作未执行。
最终 Node／Python 客户端解析并验证响应后确认接收；确认通道失败时仍保留已收到的
原始结果与 UUID，不能自动生成新 UUID 重试。客户端进程丢失后，新 UUID 表示新请求，
不保证应用回调对旧结果恰好消费一次；原 authority 存活期间，确认后仍可按原 UUID 查询。
收到 socket 写入成功不构成调用方确认。未知物理 I/O 或未确认结果会阻止受保护的
authority 关闭；如果私有失败日志无法持久写入，原操作结果及日志错误分别保留在存活的
authority 中，后续变更拒绝执行，不能用重建 owner 消除该故障。
关闭或 Native suite 退休因未确认结果被拒绝时，私有 `failures.ndjson` 同时记录
阻塞操作 UUID、authority 的 `pending`／`completed` 状态及确认状态；Raster relay
未结算时在 `raster_shutdown.relay_snapshot` 记录其观察到的未确认 UUID 和响应状态。
relay 未观察到确认时标为 `unknown`，不把连接中断或已收到响应当作确认；这些诊断
不改变原始失败、退休守卫或恢复规则。
若真实 HTTP 操作已有状态与正文、随后数据库连接池释放失败，同一 UUID 的恢复结果
分别保留 `operation_response` 和 `disposal_error`；释放责任未证明结束时继续拒绝新变更，
不得重放原操作。完整原始结果只保存在私有 authority 和证据中，普通输出仍需脱敏。
Node 恢复接口的 `privateOperationResponse` 保存未脱敏的已知失败正文，且不会随普通
对象枚举输出；仅限私有诊断，不得打印该字段。
私有记录位于对应工作树模式的 `fixture-authority/`；隔离 Playwright suite 使用
`suite-<suite UUID 的小写十六进制>/owner.json`，Raster 独占流程保留根目录下的
`owner.json`。只有环境所有者可以读取这些记录。Raster 中断在封闭阶段时，恢复方先
核对原始进程与资源身份，再通过同一私有客户端执行 `owner_offline_status`。只有
`phase: sealed` 同时证明原提供方完成在途工作、每个 run 的原生 worker、Celery worker
和代理已实际排空，原生控制进程已退出且 seed 数据库连接池已释放；`sealing`、
`resuming` 和 `incomplete` 均不是完成证明。原生监听地址由同一 owner 保留，
恢复时重新启动并验证实际进程，不重建目标或改写地址。先恢复同一原生
Routing/Health 端点，再以记录中的封闭凭据显式请求 `owner_offline_resume`；
部分恢复仍不能报告为已封闭。Raster 的产品 API 在恢复后由该私有 authority 启动并
回收，沿用启动器分配的端口与受限业务凭据；必须取得实际退出与 lifespan 关闭回执，
再退休控制进程及 suite，不能以 PID 消失代替成功。
不要将记录内容或凭据打印到终端。完成准确 suite 清理后以其 UUID 请求 `owner_shutdown`；若还有其他
suite，返回的 `shared` 表示 authority 继续持有邻居，并不表示该邻居已退休。
最终关闭必须由持有 pidfd 的客户端证明原进程退出并完成精确记录收尾；若该客户端在
确认结果后、证明退出之前失联，则保留原 generation 和证据为未决状态，报告失败，
不得自动重建 authority、重放操作或销毁资源。最终退出后的原 authority 不再提供查询。
Raster 确认 authority 退出及 generation 关闭后，在独占 authority 锁内将该次
Native 回执原样归档到相同 suite 子目录；目的地冲突或部分迁移必须保留并拒绝重用，
不能覆盖或删除已有回执。
已收到最终关闭结果的调用方可由新的私有客户端直接确认原 UUID；该客户端仍须核验
同一 authority 的当前记录、精确进程退出及记录收尾，`shared` 确认不会结束共享 owner。
原生推理 owner 必须通过原始进程身份与私有控制通道核验；完整暂停的退出回执保留在
工作树 mode 的 `inference-paused.json`，文件名或缺失的进程本身都不等于完成证明。
仅在原控制 owner 已认证、同一组 Routing/Health 端点就绪后才重新开放 seed。
非 200 响应和异常原文追加到同目录的私有 `failures.ndjson`，authority 启动错误保存在
`startup-failures.log`；其中可能含凭据或 SQL，不要输出到日志或终端。

module-data 验收器同样先经过规范资源销毁检查；检查拒绝时保留运行所需的受限
存储凭据，不能先撤销仍在收尾的读者权限，再把数据库与存储留作“可恢复”环境。

清理重试区分原件的业务退休与物理回收：已删除的样本或辅助原件，只有其准确 Creation、
业务所有者、保存位置及内容事实与原生退休断言一致时，才跳过重新挂接；缺少或不匹配的
断言仍拒绝。跳过挂接不代表文件已删除，物理回收仍须经过原有 claim／attempt、
准确版本删除与缺失验证，不能重建已退休业务行或补造回执。
若回收执行失败，只有原生 attempt 已证明物理结束且 claim 已释放，后续明确的清理请求
才会重新执行原生预检，以 `retry_of_id` 创建新的已审批尝试；此前失败和两次尝试的归属
记录都保留。未结算、未知 I/O 或未释放的责任不会因此获得重试许可，不能修改状态或租约
代替真实结束证明。恢复进程也必须先通过规范准备入口重新取得本环境的存储配置与凭据。
地图及原始 LiDAR 辅助原件由私有 owner 的原生退休命令删除业务锚点，必须先匹配准确运行、
已结束的写入与读取、已删除的数据集及互斥身份的退休断言。普通直接删除仍被拒绝，
设置 restore 开关或经 `SECURITY DEFINER` 转发不会取得该私有权限。

Dataset 清理预检与 Job 删除使用同一份准确执行退休证明：私有推理／导出必须属于已封闭的
运行，并由原执行所有者证明物理工作已结束。Job 进入终态、任务类型可识别，或仅有取消
确认，都不能替代该证明；未知范围和未结算工作继续阻止回收。

私有原件的导出读取与回放共用 Media owner 准入，使用独立只读凭据及保存的准确
VersionId／ETag 核验字节。许可的独立事务不能提交调用者事务；源身份改变或读取未
收尾时仍拒绝回收。只验证这个读取通道，不代表独立 worker、完整导出交付或保留期
已经通过验收。

版本化 ZIP 保存真实上传确认及准确版本的 ETag／SHA256 读回证据。回收按保存的版本
删除并核验该版本不存在；增加 key 的删除标记不算物理回收，也不能删除同键的邻居版本。
私有交付在创建时冻结 15 分钟期限，验证等待真实数据库时钟，不通过回写时间戳缩短等待。

## 当前题 AI 的真实异步作业 E2E

`workbench-ai-inspector-phases.spec.ts` 需要与隔离 API 使用同一测试数据库和专用
Redis broker 的 Celery worker；确认后设置 `PLAYWRIGHT_AI_REQUEST_WORKER=1` 才会执行。
不能把 worker 连到开发数据库或复用开发任务队列。API 和 worker 同时覆盖
`DATABASE_URL`、`MIGRATION_DATABASE_URL`、`REDIS_URL`、`CELERY_BROKER_URL`，并设置相同的
`PREANNOTATE_GPU_QUEUE` / `PREANNOTATE_CPU_QUEUE`。worker 订阅这两个队列及 `default,audit`，
使用当前 checkout 的虚拟环境，不启动 beat。测试提供方不使用 GPU；关闭 GPU arbiter，
防止继承开发环境 GPU 调度：`GPU_ARBITER_MODE=off`、
`GPU_ARBITER_ROLLOUT_ENABLED=false`、`GPU_ARBITER_RESOURCES_JSON={}`。
API、worker 和私有 owner 使用 `ML_BACKEND_ROUTER_MODE=enforce`，并连接本套隔离的
Routing owner；不能关闭路由或改用无 scope 的执行来绕过受管提供方的准入检查。

模型夹具由私有 owner 为每个运行启动带认证的受管测试提供方；真实 API／worker 始终使用
该运行的 Registry，Node helper 仅通过 owner 通道控制暂停、释放和故障，不再替换后端 URL。
Routing、健康观测、模型执行与后端控制能力须属于同一套隔离运行拓扑。清理先拒绝外部引用，
再完成真实排空和拓扑核对；提供方退出或无法核实时保留失败与资源，不跳过检查。
业务与原件回收完成、run 退休已持久化后，owner 才关闭该运行的提供方并等待实际进程退出。
关闭失败保留句柄并报错；suite 清理还会重试本地仍有提供方的已退休 run，不仅检查数据库中的
未退休行。不能只凭 run 状态或不可访问的 HTTP 地址宣告进程已退出。
该提供方仅生成确定性模型输出、延迟和失败；预标派发、作业查询、取消、预测落库和候选采纳
均使用真实服务。`PLAYWRIGHT_ML_FIXTURE_HOST` 可指定 API／worker 能访问的非 loopback
IPv4；缺省解析本机主机名。若主机名仅解析到 loopback，必须显式指定可达地址，
不能用关闭路由或伪造健康状态替代。不据此声明真实模型质量、YOLO／SAM 或 GPU 执行资格。

```bash
PLAYWRIGHT_AI_REQUEST_WORKER=1 pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter web test:e2e e2e/tests/workbench-ai-inspector-phases.spec.ts --project=chromium
```

每例在输出目录保存不含凭据的 `ai-request-metadata.json`，记录项目与模型收到的任务 ID。
结束时先释放模型等待并等作业终态，再解除控制绑定、请求本 suite 的 owner 清理登记资源。
owner 只在准确运行的真实退休和所有自有工作均已确认后退出；未知或失败的清理保留
原句柄及失败证据，不按推断的 task 前缀手工扫桶。

## WebCodecs 精确帧 E2E

视频 Issue 的 180 帧夹具按测试 API 的 `VIDEO_CHUNK_SIZE_FRAMES` 生成完整真实分片，默认三块，每块 60 帧。测试开始前核对正式 manifest 与分片范围、就绪状态；不能依赖未启动的媒体 worker 补齐缺失分片。像素身份仍从实际编码视频验证。请求失败分类集中在 `helpers/request-errors.ts`：每条允许项都是「方法 + 精确路径 + 原因」的有类型规则，图片与视频套件按共享规则组组合；只允许明确端点的 `net::ERR_ABORTED` 生命周期取消（心跳、会话统计、bootstrap/任务上下文读、帧预览及分片元数据/样本），标注与 Issue 写入失败、其他网络错误与 HTTP 错误仍须报告。`pnpm --filter web test scripts/video-request-errors.test.ts` 同时验证这些边界的允许侧与禁止侧，CI 的前端单元测试同步执行。

刷新前的布局保存等待必须匹配当前交互产生的 workspace 快照，并等响应接收完成。初始化、切换预设和激活讨论面板会分别发送偏好 PATCH，首条成功响应不能代表后续保存完成。任务级 Issue 用例延迟最终讨论布局响应，验证刷新不会抢先取消偏好写入；偏好 PATCH 的取消仍按错误处理。

`e2e/tests/video-webcodecs-precise-frame.spec.ts` 用 `seed/video-webcodecs`
造确定性 H.264 fixture（baseline / 主 profile B 帧 / 短 GOP / VFR），验证精确帧
pipeline 的开关边界、精确解码或安全回退、pending→ready 切换。视频舞台容器暴露
`data-video-frame-source` / `data-video-precise-state` / `data-video-frame-index`
三个可观察属性供 spec 读取；`data-video-painted-frame-index`（精确绘制完成的帧号，
非 webcodecs 源恒为 `-1`）用于等待绘制就绪。导航触发的异步取帧在负载下可能
迟到：应用层已在播放控制器的帧号写入处加单调栅栏，迟到的旧帧回报不会再覆盖
更新后的帧号（Issue #114，组件级回归见
`useVideoPlaybackController.stale-seek.test.ts`），视频 Issue 套件的
`expectPaintSettled` 保留为冗余防护——等它与 `data-video-frame-index` 一致后再
继续或断言仍最稳妥。

### 时间轴定位契约与真实指针回归

视频 Issue/track 套件建立前置帧一律走 `e2e/helpers/video-timeline-seek.ts` 的
确定性通路：先等可见窗口包含目标（需要时展开时间轴并 zoom-reset，等窗口状态
实际更新），再把能精确表达目标帧的 range 归一化值（0..10000）写入「视频帧时间轴」
输入，经应用既有 `onChange → onSeek` 生效。映射与组件 `timelineCoords.pctToFrame`
同款，`rangeValueForFrame` 反解不精确即显式失败——本套件 180 帧素材在任意合法
窗口下逐帧可表达（`scripts/video-timeline-seek.test.ts` 逐帧验证）；更长素材出现
不可表达帧时应缩窗或扩粒度，不允许静默近似。导航失败时 `seek` 会在
`seek-navigation-state` 附件写入目标/选择/绘制帧、显示源、precise state 与时间轴
窗口的只读快照，帮助区分「目标未写入 / 呈现未完成」，且不覆盖原始断言错误。

像素采样（`fixtures/video-frame-pixels.ts`）对「属性断言已过、采样时文档恰好被
真实导航替换」的竞态做了有界重试：仅针对 `Execution context was destroyed`，
重试在当前文档上重新采样。帧身份断言逐帧特异，采错帧会照常失败，因此重试
不会把导航竞态放大成假通过。

不要回退成「裸坐标点击时间轴 + 有限键盘补偿」：播放浮层在指针离开画布约 2s 后
进入 `pointer-events: none`，裸 `mouse.click` 绕过 actionability 检查会被静默吞掉
（`default-two` shard 2/4 曾因此偶发失败：点击后停留网格吸附帧，补偿十次后断言
失败）。fill 不做命中检查，不依赖浮层可见。

真实鼠标路径由 `e2e/tests/video-timeline-seek.spec.ts` 单独覆盖：折叠态、展开态、
关闭 Issue 表单后、缩放窗口与首末帧端点。它以 `video-timeline-shell` 为定位器，
用 `locator.click({ position })`（带 hit-target 检查，被吞的点击会显式失败），
几何换算与组件 `frameFromPointer` 一致；点击前通过真实指针移动唤出浮层并断言
shell `pointerEvents` 恢复 `auto`，不用 `force: true`、不关动画、不用键盘补救。
端点点击内缩 2px 并容忍 ±1 帧的取整漂移（输入量化到整数像素的固有不确定
性，内部帧仍逐帧精确断言）；不要把端点断言绑定在个别边界像素上。

**能力门**：WebCodecs `VideoDecoder` 需 secure context。localhost 下 Chromium 暴露
构造器，但 headless 软解下 `isConfigSupported` / 实际 decode 可能不通过，精确帧会
安全回退。spec 据此 **capability-aware**：先观测 pipeline 实际解析到的
`data-video-frame-source`，精确帧成功才跑 corner_bits 像素断言，回退则验证 fallback
合同并通过 annotation 记录原因 —— 绝不把「回退」伪装成「像素已验证」，也不把
「能力不足」判成失败。

spec 已实现 Konva media canvas 的区域像素采样：按背景亮度与四角 bit 验证 key / P /
B / GOP / VFR 目标帧，并同步核对诊断中的目标 PTS。默认冒烟环境若明确缺少 H.264
解码能力，会把像素和 pending→precise 用例标为 capability skip。浏览器资格测试必须
显式开启严格能力门，此时缺少 WebCodecs 原语、codec 不支持或 `decode_failed` 都会失败，
不能用回退或 `ready` 属性代替像素证据：

```bash
cd apps/web
DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority \
PLAYWRIGHT_CHROMIUM_CHANNEL=chrome PLAYWRIGHT_REQUIRE_WEBCODECS=1 \
pnpm exec playwright test \
  e2e/tests/video-webcodecs-precise-frame.spec.ts --project=chromium --headed
```

GPU runner 应显式选择系统 Google Chrome；Playwright bundled Chromium 的 codec
发行配置可能与用户 Chrome 不同。`DISPLAY` / `XAUTHORITY` 按 runner 的本地 X11
会话调整，不能把不可用的 SSH 转发显示误记为浏览器能力失败。

## 文件组织

```
e2e/
├── fixtures/          # 共享 fixture（seed、authedPage 等）
├── global-teardown.ts # 正常结束时的 cleanup 兜底
├── tests/             # 实际 spec
│   ├── auth.spec.ts
│   ├── annotation.spec.ts
│   └── batch-flow.spec.ts
└── utils/             # 辅助函数
```

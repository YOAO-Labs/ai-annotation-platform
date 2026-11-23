---
title: 独立工作树开发环境
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-23
---

# 独立工作树开发环境

`pnpm dev:worktree` 为当前 checkout 管理独立的开发状态，不再只隔离 HTTP 端口。它保留 Alembic，不会把共享数据库降级、重新 stamp，或复制主目录的业务数据。

## 前置条件

- macOS 或 Linux、Node.js、pnpm，以及当前 checkout 独立的 `apps/api/.venv`。先完成 [Orca 工作树初始化](../tutorials/local-dev#orca-工作树初始化)。
- 本机 Docker 可用；共享 PostgreSQL、MinIO 已经启动。建议在主目录只执行 `docker compose up -d postgres minio`，不要为每个工作树重复启动默认 Compose 栈。
- PostgreSQL 配置使用本机 `postgresql+asyncpg` URL，不带 query 参数；运行连接与迁移连接必须指向同一个本机实例。两个账号都需要连接 `postgres` 库，启动器通过会话锁核对实例。迁移账号需要创建和管理新库，并具备 `CREATEROLE`，为每个模式生成两个隔离的 Jobs 函数账号；基础配置共用 owner 时还会生成普通运行账号，不会回退给 API 迁移凭据。MinIO endpoint 也必须位于本机，账号需要创建、标记和管理专属 bucket 的权限。
- 默认关闭外发 SMTP、Sentry、共享 ML 后端自动注册和 GPU 控制配置。不启动 GPU worker；`--with-worker` 的 Beat 只调度 Jobs 恢复，不执行其他日历任务。真实 GPU 调度验证仍使用专用验收环境。
- Orca setup 与 `init/up/exec/reset` 在 API／Alembic 导入前初始化缺失的固定 TrackEval 子模块；已有本地修改保留。SAM3 和 Grounded-SAM-2 不在普通 API 初始化中下载，构建模型镜像前另行执行 `git submodule update --init --recursive`。

这不是生产环境隔离或权限沙箱：基础设施进程和账号可能共享。启动器拒绝 staging/production 配置，也不会自动启动、停止或重建共享 PostgreSQL/MinIO 服务。

`doctor` 对缺失的 TrackEval 返回初始化指引，不下载源码；`stop/destroy` 也不会为了停止或清理环境发起子模块网络请求。

## 首次启动与资源归属

在工作树根目录运行：

```bash
pnpm dev:worktree
# 需要导入、导出、审计、派生媒体等后台任务时：
pnpm dev:worktree -- up --with-worker
```

首次启动会创建独立数据库、Redis 容器和十个 bucket，再运行本 checkout 的迁移。输入桶 `MINIO_INPUTS_BUCKET` 独立启用 versioning，不配置自动过期；派生媒体仍使用不启用 versioning 的 `media-cache`。已有且归属正确的资源会复用；已有但未标记或属于别处的资源不会被自动接管。

`test` 和 `e2e` 还会对归属核验通过的 `MINIO_IMPORT_BUCKET` 启用并检查 versioning，供测试导入原件固定版本使用；`dev` 不因此改变导入桶策略。该准备步骤不接管外部桶，也不把已受理输入桶或导入桶的设置推广到派生缓存。

八桶和九桶是用途清单的版本，不是另一套 owner 身份。同一资源命名和归属下，`doctor`、`stop` 和 `destroy` 按保存清单操作，不创建第十个桶。显式 `init/up` 才会增加缺失的训练桶或输入桶，并原子保存清单。使用 `aap` 资源名或旧归属标签的环境必须交回匹配的旧工具，不适用这项扩展。

若创建输入桶时中断，恢复记录在 `.worktree/<mode>/inputs-bucket-upgrade.json`。缺少归属标签的桶不会被自动接管或清空；管理员需核对精确桶名、归属记录和内容，确认可安全处理后再显式完成 `init`。未完成的输入桶升级会阻止旧清单销毁，以免遗留未登记资源。不要删除旧资源清单或恢复记录来绕过归属检查。

首次准备在创建训练桶或输入桶前保存当前资源清单；若中途失败，自动化验收会先按清单和标签尝试清理。清理无法确认归属时保留数据库、清单和恢复记录，供后续核对，绝不按桶名删除无标签资源。

| 资源            | 隔离方式                                                        |
| --------------- | --------------------------------------------------------------- |
| PostgreSQL      | 共享实例，库名为 `yoao_wt_<id>_<mode>`；数据库 comment 记录归属 |
| Redis           | 独立、带归属标签的容器；端口仅绑定本机，AOF 存放在工作树目录    |
| MinIO           | 共享实例，十个 bucket 分别使用环境前缀并记录归属标签            |
| API/Web         | 当前 checkout 的进程，自动分配空闲端口并设置 API/WebSocket 代理 |
| Celery          | 可选的普通、维护及 Jobs 恢复 worker；独立 Beat 只调度 Jobs 恢复 |
| DuckDB/临时文件 | 当前工作树、当前模式的独立目录                                  |

`.worktree/identity.json` 保存稳定 ID 和 checkout 路径；提交代码不会改变 ID。各模式下的资源清单和进程记录不包含数据库密码或完整连接串。
基础配置共用 owner 时，启动器为当前模式创建 `yoao_wt_<id>_<mode>_app` 普通 LOGIN，角色 comment 记录相同资源归属；随机密码仅保存在权限为 `0600` 的 `.worktree/<mode>/runtime-credentials.json`。API／普通 worker 使用这个账号，迁移与维护 worker 继续使用原 owner。已有分离账号配置不被替换。
`jobs-dispatch-credentials.json` 和 `jobs-publication-credentials.json` 同样只属于当前模式，权限为 `0600`。普通生产者只接收发布账号；领取账号不进入普通 API、普通 worker、维护 worker 或 Beat。工作树准备完成前核对两个 Jobs LOGIN 的实际函数权限。销毁检查这两个账号的归属、会话和外部依赖，绝不按名字接管。

该目录已被 Git 忽略，**不要复制、软链接或随手删除它**；丢失归属记录后不能靠资源名自动接管旧数据。凭据文件损坏、权限过宽、目标或迁移账号变化、已有角色缺少匹配凭据时都会拒绝继续，不自动改密或接管。移动工作树前，应先停止并按需销毁旧环境。

当前启动器只接受新身份。看到旧 `identity.json` 或继承的 `AAP_WORKTREE_MODE`／`AAP_WORKTREE_RESOURCE_ROOT` 时会拒绝，不自动认领。需要退役已获准销毁的旧环境时，在原路径、原配置用匹配的旧工具逐个模式 `doctor`、`stop`、按原确认值 `destroy`；核对原库及角色、Redis 和桶均已消失。保留清单与数据备份，再在全新工作树运行 `init`。外部 owner、活跃连接或未清责任必须先核清，不能通过改标签、删清单或强断连接绕过。

`.env` 仍可链接主目录作为基础配置，运行时仅向子进程注入独立目标，不修改共享文件。直接运行 `pnpm dev:api`、`uv run pytest` 或默认 Compose 不经过这个隔离入口，仍按原来的配置工作。

媒体上传下载地址统一使用同源 `/minio`，Vite 转发到已验证的本机 MinIO endpoint；远程浏览器只需能访问 Web 端口。应用临时文件按模式隔离，端口预留锁则统一保存在用户目录的 `.cache/aap-dev-ports`，供所有工作树和模式协调使用。

前端进程只接收系统和公开配置，不继承数据库或对象存储凭据。API 配置通过匿名管道交给监督进程后传给 API 子进程，不写入磁盘或命令行；API/普通 worker 不继承迁移账号连接。专用维护 worker 将同库的 owner 连接作为 `DATABASE_URL`，同时清空迁移和测试连接变量。进程身份同时核对 Linux `boot_id`、`/proc/<pid>/stat` 的 `start_ticks`、工作树范围和命令摘要，支持软链接入口和相对路径调用；`ps lstart` 只供人工查看，不作为身份条件，避免系统时钟校正造成误判。缺少出生字段的旧记录不能证明身份，不补写当前观察值或据此删除原现场。

API／普通 worker 的环境还会移除推理路由和健康观测 owner 的数据库连接及文件选择器；
调用方可接收 HTTP 地址和令牌，但不能继承这两类 owner 的专用数据库凭据。

所有 Identity 专用数据库选择器及文件选择器会在子进程中清空，避免从共享 `.env` 连接到其他环境。默认内嵌 Identity 通过固定 Native 能力读写，其私表与列权限在授权事务内撤销。

自有运行账号不持有角色成员资格；仅允许创建它的普通迁移账号保留 PostgreSQL 自动授予的 ADMIN-only 成员关系。权限、外部数据库依赖或归属变化会阻止继续。`doctor` 只核对角色与真实登录，不创建账号或凭据。

新库只有迁移创建的结构及必要数据，没有原来的账号、项目或媒体。需要管理员时，在下面的隔离 `exec --mode dev` 中运行现有的 `scripts.bootstrap_admin` 流程；凭据按[开发部署说明](/ops/deploy/development#_2-5-首个-super-admin)配置，不放进工作树身份清单。

## 手动验收筛选功能

在工作树根目录执行一条命令：

```bash
pnpm dev:worktree -- up --mode e2e --scenario filtering
```

它准备该工作树的独立 `e2e` 环境、生成筛选数据并启动 API/Web，终端会显示实际端口、账号、各场景链接和预期结果。初始化使用单独的夹具 owner 进程；普通 API 仍使用受限运行账号。首次要求库内没有账号（含停用账号）、项目或数据集。有数据但没有验收记录时拒绝覆盖，需你先决定是否重建该模式；已有有效验收记录时复用数据并保留操作。它不会把测试 seed 路由开放给 `dev` 库。

新建账号为 `admin-manualfilter@e2e.test`（管理员）、`anno-manualfilter@e2e.test`（标注员）、`rev-manualfilter@e2e.test`（审核员），初始密码均为 `Test1234`；已有验收记录以启动器显示的账号为准。

| 场景               | 初始预期                                                                          |
| ------------------ | --------------------------------------------------------------------------------- |
| 图片任务权限       | 管理员 6 题，标注员 5 题，隐藏批次不进入标注员列表或统计                          |
| 同对象属性         | 标注员查询 car AND blue 仅命中 SAME；CROSS 的红车与蓝色行人不会被拼成蓝车         |
| 必填与保存视图     | 缺失必填属性命中 MISSING；可修改嵌套 AND/OR、创建视图并刷新验证                   |
| 对象分页           | 总数 101，首次 100，继续加载后 101                                                |
| Scene 轨迹         | 标注员可见 101 条，管理员还可见隐藏批次的一条；跨任务逻辑轨迹去重                 |
| 视频 AI 待审       | OR 命中 V-1/V-2/V-3，已采纳或驳回的 V-4 不命中                                    |
| 视频显示和批量范围 | V-3 的检测候选位于 F0/F10，置信度阈值调到 0 后体验当前帧与已加载候选的区别        |
| 管理列表           | 使用 Filter 项目、数据集、模板及不同成员、邀请、审计、任务状态检查筛选与 URL 恢复 |

这套数据来自 `seed.filtering()`；手动入口额外给视频检测候选补齐可渲染几何，保留原有置信度、数量与预期成员集合，不启动真实模型推理。素材复用仓库内的小文件，不运行 demo seed 的大型素材下载。默认只启动 API/Web；需要后台任务时追加 `--with-worker`。

`Ctrl+C` 或 `stop --mode e2e` 保留数据；再次执行同一启动命令，会读取 `.worktree/e2e/data/filtering.json` 并复用原项目、视图和手动修改。表中预期指初始数据，手动修改后结果可能随之变化。项目或记录损坏时提示显式重建，不自动修复或清空。

保留手动验收记录时，同一模式的 `exec` 会被拒绝，因为自动化测试中的全局 cleanup 会删除这些数据。结束体验后先停止，使用 `doctor --mode e2e` 返回的 `resources.confirmation` 执行 `reset`（重建）或 `destroy`（删除）。重建后可重新启动手动场景，或运行自动化 E2E。

```bash
pnpm dev:worktree -- stop --mode e2e
pnpm dev:worktree -- doctor --mode e2e
pnpm dev:worktree -- destroy --mode e2e --confirm '<工作树ID>:e2e'
```

停止后 Redis 不运行，`doctor.healthy` 可能为 false；它表达运行就绪状态，不能用作提取删除确认值的条件。删除命令自身会重新校验归属、会话及数据库连接。保留 `.worktree` 身份和清单直到销毁完成，再删除工作树；共享下载缓存会保留。

## 三种模式与测试命令

- `dev`：日常开发，`up/init/doctor/stop/reset/destroy` 默认使用它。
- `test`：可丢弃的 API 测试环境，`exec` 默认使用它。
- `e2e`：另一套独立的端到端测试环境。

每种模式都有独立数据库、Redis、bucket 和文件目录。同一个模式同时只允许一个管理/执行会话，避免测试重置与开发请求交错。不同工作树、或同一工作树的不同模式可以并行。

```bash
# 只准备资源和迁移，不启动 API/Web
pnpm dev:worktree -- init --mode test

# API 测试：保持测试所要求的 apps/api 工作目录
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_alembic_drift.py'

# Playwright：统一注入测试库、存储、Redis，并预留互不冲突的服务端口
pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e

# 后台任务相关验收，可以给 exec 同时启动对应 worker
pnpm dev:worktree -- exec --mode e2e --with-worker -- pnpm test:e2e
```

`exec` 的命令从工作树根目录执行，`--` 后面的参数原样传给子进程。`TEST_DATABASE_URL` 和 `PLAYWRIGHT_E2E_DATABASE_URL` 由入口统一覆盖。入口自动注入 `YOAO_WORKTREE_MODE`；在 `dev` 模式中，pytest 和 Playwright 会在准备测试数据库前明确拒绝运行，且不导出其他模式的可用测试连接。需要同类测试并行时使用不同工作树，不要绕过模式锁或手工改写这个内部标识。

切换命名时，先用旧工具的 `doctor` 盘点旧模式；有待保留的数据先备份并在隔离目标演练恢复。旧工具只退役它能按完整身份验证且获准销毁的资源，再用新入口初始化。不要改写旧 manifest、重贴桶标签或重命名数据库来取得所有权。冲突的 `AAP_WORKTREE_MODE`、旧 owner 或遗留进程都不是新环境的授权。

Playwright 的迁移和数据库夹具使用隔离库的所有者连接；其 API 使用同库的运行账号，并在启动前清空迁移及夹具连接变量。

`exec --mode e2e` 在核验工作树数据库归属后撤销该库 `PUBLIC` 的临时表权限，
并保留普通运行账号原本已有的临时表权限；其他数据库不受影响。专用函数账号不能继承该权限。
该撤销不修改同库已有的显式授权；仅依赖 `PUBLIC` 的其他账号不自动保留该权限。
共享 PostgreSQL 的 `PUBLIC CONNECT` 可能允许这些 LOGIN 认证到其他库；此工作树隔离
不更改邻库的连接政策，也不是跨数据库认证沙箱。专用 Native 函数授权仅授予核验过的
本模式数据库，启动时仍以真实 LOGIN 核验固定函数集合和私表访问拒绝。

规范 E2E 启动同库的真实 Routing 与健康观测 HTTP owner，使用两个按工作树和模式创建的
函数专用 LOGIN；API／worker 只收到 HTTP 选择器，不收到这两个数据库连接。凭据保存在
`.worktree/e2e/inference-credentials.json`（0600），停止后保留，精确销毁时核验并删除账号。
启动就绪确认共用 30 秒总期限，单次健康探测使用其中的剩余预算；
有效的 Native 权限核验响应不因超过 1 秒便被反复截断，不另外延长启动期限。
私有输出代理由自己的 CLI 入口定位本 checkout 的 API 包，不依赖外层 `PYTHONPATH`；
这不向独立 Images／Media worker 添加 API 源码路径。
规范 Raster 启动器从本次已核验的私有 Docker bridge 提供模型夹具的可达地址，
不依赖宿主机名称能否解析到非回环地址，也不借用其他运行的网络。
故障前仍须封闭原生产者与消费者。私有 Redis 可保留只含路由信息的 Kombu 绑定集合，
待消费消息、未确认投递及未知状态仍阻止继续；不删除元数据来伪造空库。
排空需等待已接纳请求和连接清理完成、私有回执及实际进程退出；结果不明时保留
`inference-owner.json`、进程和凭据，`stop`／`destroy` 不会把它视为已停止。
原监督进程已确认退出、没有未证明的消费者时，持有模式锁的恢复命令会核对原进程、
私有控制 socket、Linux 进程启动身份及 pidfd。对仍在服务的同一个 owner，可执行
`pnpm dev:worktree -- recover-inference -- <恢复命令>`。恢复时只读核验原数据库、物理存储、
目标与创建回执、原 reader 凭据和连接器密钥／allowlist，并通过原 authority 的认证状态接口
核验完整配置；不会重新迁移、开设替代存储或轮换凭据。命令收到原有 HTTP 和 reader 选择器，
结束后 owner 仍保留；缺失原 authority 或任何身份不一致时拒绝恢复。
如果排空已完成但控制回执丢失，可用 `pnpm dev:worktree -- recover-inference --finish-drain`
检查原 owner 的认证排空状态，并在原件责任、authority 锁及进程退出均获证明后完成关闭。
已完成的离线暂停会先保存 `inference-paused.json`：该记录绑定原 owner、排空回执、
实际 pidfd 退出和原选择器，再移除运行标记。恢复可在核验这个回执后使用相同地址与令牌
重新启动；缺少回执不能仅凭标记消失推断暂停完成。
正在排空或身份不明时拒绝接管，不以连接消失、PID 复用或超时推断结算。

规范 E2E 中断时，监督器只协作通知实际命令，不在宽限期后强杀进程；退出码仍为非零。
无法证明消费者及后代结算时，`run.json` 保存已观察到的精确进程身份及未解决状态，
并阻止 `stop`、`destroy` 和自动接管；不能仅凭进程或端点消失清除该记录。
规范 E2E 在消费者启动前建立 Linux subreaper 边界。命令正常退出后，监督器释放端口再确认退出码；
无论退出码是否为零，仍需回收直接消费者及已接管的脱离进程，并核验所排除的 native owner，
才记录监督完成回执，并返回原测试退出码。缺少回执、消费者仍在运行或身份不明时保留记录，
不以超时或强杀当作结算。

运行级原件夹具通过独立的 `seed-request.py` owner 进程准备和清理。该进程先核对同一个可销毁数据库及 owner 身份，再显式启用托管原件维护协议；不改变普通 API／worker 或生产环境的默认开关。清理仍须满足准确根回执、对象版本、location 维护门和读者责任的结算要求，启用开关不等于已证明物理 I/O 排空。

受监督的 E2E 中，播放租约由普通 API 进程持有。seed 清理必须收到该进程的确认：本次运行的 Task 和数据集 Item 的播放租约已撤销，其他运行的租约不受影响。清理不依赖页面关闭或租约到期；已经开始的媒体响应仍可读完，撤销确认不等于强行中断读取。

若另一个运行已占用同一 namespace，服务端以 `409 fixture_scope_busy` 确认本次新 UUID 未建档。夹具不会为这个 UUID 请求清理，也不会清理占用者。若请求超时、响应不明或已有操作使用过这个 UUID，仍保留其清理责任；`fixture_run_unsettled` 不是可忽略的结果。

原生输出夹具的 `fixture_native_iam_<操作>_<原因>` 标明失败的是 MinIO 授权（`policy_attach`）、用户查询（`user_info`）还是策略查询（`policy_info`）。例如 `access_denied` 来自提供方明确的拒绝；`nonzero` 只表示命令失败，不能据此判断授权是否写入。错误不包含账号或凭据。授权失败后夹具不会重发写入，也不会出具就绪回执。

夹具 Audit 清理使用原生 `(id, created_at)` 复合回执，而不是按 namespace 或单独的整数 ID 删除。实际认证会话必须属于夹具 owner，叶运行已经封闭且所有准入责任已结算；共享原始 namespace 的两个运行仍各自保留准确归属。旧 UUID 回执不会自动转换或补造时间戳。

涉及后端注册项时，清理还依赖真实 Routing owner 的连续性和排空证明。不可达的展示地址、空作业列表或终态标记不代表后端可以安全删除；未满足前置时保留运行与资源，不绕过静默检查。

测试提供方也须经过真实认证的健康观测，普通运行账号不再因数据库具有测试后缀而取得直接设置夹具健康状态的权限。重新应用权限会撤销旧授权；历史 SQL 保持原件，由前向迁移退役该入口。

CI 的可复用 E2E 工作流同样从仓库根目录调用 `pnpm dev:worktree -- exec --mode e2e -- pnpm --filter web <套件命令>`，不再另起使用 owner 连接的 API 或维护第二套运行账号授权。PostgreSQL 的初始 `postgres` 连接用于协调器建库和迁移；实际普通 API 只获得本模式的受限连接。需要已构建 Web 的 CI 套件仍使用经过指纹核验的产物和严格端口 preview；Mask 的开发服务器与原生构建入口保持各自约定。

`--with-worker` 使用当前 checkout 的 Python 依赖。普通 worker 消费 `default,media,cleanup,audit,export,image-pyramid,ml.cpu`；维护 worker 只消费 `maintenance`，执行审计与预测月分区、审计归档和物化视图刷新；Jobs 恢复 worker 只消费 `jobs.recovery`，由同一环境的 Beat 发送定期唤醒。启动器在运行 Beat 前检查 worker 注册任务及精确订阅，允许 Celery 自动创建的节点专属队列。`doctor` 显示各进程；任何一个退出都会停止本次启动的 API/Web。

这些进程不消费 GPU 队列，不复用主目录的 Docker worker。媒体任务需要的系统依赖仍须在本机安装。修改 worker Python 代码后应停止并重启当前环境，Celery 不会热重载。

## 诊断、停止与重建

```bash
pnpm dev:worktree -- doctor
pnpm dev:worktree -- doctor --mode test
pnpm dev:worktree -- stop --mode test
```

`doctor` 不创建资源；它返回 JSON，显示环境身份、资源归属、数据库 revision、本 checkout head、Redis 连通性、进程与 HTTP 就绪状态。未初始化、资源不齐或检查失败时返回非零退出码。启动中的服务不会被当作已就绪。输出不包含连接凭据。

`Ctrl+C` 停止本次启动的 API/Web 和可选 worker，保留资源。`stop` 还会停止对应 Redis 容器，但保留 PostgreSQL、运行账号及凭据、bucket、Redis AOF 和分析文件。进行中的任务不保证能够原地恢复，重要任务应先等到完成后再停止。

需要清空当前环境时，先停止相关客户端，再从 `doctor` 的 `resources.confirmation` 复制精确确认值：

```bash
pnpm dev:worktree -- reset --mode test --confirm '<id>:test'
pnpm dev:worktree -- destroy --mode e2e --confirm '<id>:e2e'
```

- `reset`：删除该模式的数据库、自有运行账号、Redis、bucket、派生文件，再创建空环境并迁移；新账号生成新密码。
- `destroy`：只删除该模式资源，保留工作树身份；验证资源删除后清除该模式的目标记录，允许下次初始化选择新的本机基础设施。
- `--confirm` 不是泛化的 `--yes`；必须匹配当前工作树和模式。
- 有客户端或其他后台数据库连接、环境会话仍在运行、资源标记不匹配、角色存在外部依赖时拒绝删除。不使用强制断开数据库连接、全局删桶或共享栈 `down`。
- PostgreSQL 自带的 `autovacuum worker` 不计入阻止删除的连接，由普通 `DROP DATABASE` 处理；工作树清理与迁移验收采用同一规则。
- 基础设施地址变化时，先使用旧配置处理旧环境，不能让同一份清单静默改指另一个服务器。
- 基础设施地址变化时，先使用旧配置处理旧环境，不能让同一份清单静默改指另一个服务器。

销毁会先核对全部归属、连接和角色依赖，再删除本模式数据库及准确匹配的自有账号，不对其他数据库执行 `DROP OWNED`。凭据与目标清单保留到全部资源删除成功，失败后可用原配置重试。创建账号与归属 comment 在同一数据库事务里完成；若事务中断但凭据文件已保存，后续 `init` 复用该密码恢复，不自动轮换现有账号。

共享测试 bootstrap 已明确结束、但原环境仍待回收时，使用原工作树和原配置显式进入退役模式：

```bash
pnpm dev:worktree -- doctor --mode test --retire-finished-bootstrap
pnpm dev:worktree -- destroy --mode test --retire-finished-bootstrap --confirm '<id>:test'
```

该模式要求已保存的 test 资源清单与原 bootstrap 的运行、端点、网络及物理身份完全匹配。它只允许检查和删除原资源，不能初始化、重建、导出应用运行环境、启动 Redis、创建账号或升级 bucket。`--confirm`、活跃会话／读者、未结算责任及归属保护仍然生效；不要改写 owner journal 伪造运行状态，也不能用此选项接管未知环境。普通启动和环境导出仍只接受活跃 bootstrap。

销毁工作树前，分别清理已经使用过的模式。Git/Orca 删除工作树不会自动删除数据库和 bucket。

## 迁移冲突

启动前会检查重复 revision、多 head、缺失祖先及数据库未知 revision。数据库版本不在当前代码中时，恢复对应迁移代码，或在确认可丢弃后重建独立环境；不要手工修改共享 `alembic_version`。

`--skip-migrations` 只在数据库已经处于当前 checkout 的 head 时生效，不能绕过不认识的 revision。独立开发库不会自动解决 PR 合并后的迁移冲突；主分支仍应保持唯一 head，并对最终合并状态验证空库和已有数据升级路径。

## 验证启动器本身

```bash
node --test scripts/dev-worktree.test.mjs
apps/api/.venv/bin/python -m unittest discover -s scripts -p 'test_worktree*.py' -v
apps/api/.venv/bin/python scripts/test-orca-worktree-setup.py

# 真实基础设施验收：创建随机、带归属标记的临时资源并在结束时删除
apps/api/.venv/bin/python scripts/verify_worktree_isolation.py -v

# 共用基础账号验收：自动分离运行账号，实际在线状态任务及五项维护任务
apps/api/.venv/bin/python scripts/verify_worktree_maintenance.py
```

两条 `verify_worktree_*` 命令会创建和删除临时资源，不是无副作用的单元测试。隔离验收覆盖迁移、Redis 键与 Pub/Sub、停止恢复、对象隔离和重建；维护验收额外使用临时 PostgreSQL 容器，从共用 owner 的基础配置自动分离账号；验证私表与 DDL 拒绝、普通 worker 的真实 Identity Native 调用身份、五项维护任务、重建时凭据稳定及完整退出／清理。只在已核对的本机开发基础设施上运行。

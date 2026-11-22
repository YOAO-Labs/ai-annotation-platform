# 0185 — 交付工具的输入、运行身份与失败边界

- **Status:** Accepted
- **Date:** 2026-11-21（回填既有决策）
- **Deciders:** 平台交付、数据模块和文档站维护者
- **Supersedes:** —

## Context

仓库一部分文件同时被解释器、构建器、部署工具和文档生成器读取。脚本输出还承担合并门禁、资源所有权证明和运维恢复证据。维护这些入口，不能只看程序能否启动：漏掉校验输入可能让 CI 无证据通过；拿错凭据、存储地址或进程标识可能改写不属于本次操作的资源。本记录保存这些交付入口的约束，不改变当前部署拓扑，也不替代各业务模块的合同。

## Decision

[按源码路径查阅局部决策与理由](../evidence/0185-decisions.tsv)。编号对应原始实现位置，相关 ADR 仍拥有其领域合同。

### 部署、镜像与外部入口（部署维护者）

- 基础 Compose 是宿主机开发入口；整栈生产显式叠加 `docker-compose.prod.yml`，不使用会自动合并的 override。`--env-file .env.production` 是 Compose 的 `${VAR}` 插值源，服务的 `env_file` 则向容器注入配置；只配后者不能覆盖基础 worker/beat 的内联开发凭据。所有设备和职责队列都要用生产配置，否则会出现 API 正常、某条队列没有消费者的局部故障。生产基础服务从服务 DNS 直连；清空 PostgreSQL、Redis、MinIO 的宿主发布端口使用 `!reset`，不能靠叠加空列表。API/Web 向同机反代默认只绑定回环，异机反代只绑定审核过的内网地址；不能拿主机防火墙替代 Docker 端口绑定。Mailpit 是开发收件箱，清空宿主端口不代表生产停止创建容器；真实邮件中继及告警设置见 [#160](https://github.com/YOAO-Labs/ai-annotation-platform/issues/160)。
- `docker-compose.lan-prod.yml` 是另一套独立入口，不叠加上述文件，不接管共享数据库、MinIO 或模型容器。LAN 使用一趟独立迁移服务，迁移 LOGIN 不交给运行 API/worker；既有运行角色只保留选定的维护对象。整栈 API 的镜像入口是否迁移取决于 `ALEMBIC_AUTO_UPGRADE`，不把镜像默认行为当作每份部署的保证。共享模型的 LAN GPU 控制面只能观察，不能得到开发 collector 连接或成为第二个 enforce owner。[显存控制面的所有权](0049-cross-backend-gpu-memory-arbitration.md) 同时限定独立单并发控制队列和专有凭据文件；在需要校验 `0400` 的挂载上使用保留原权限的只读 bind mount，不能依赖 Compose 文件形式 secrets 的模式覆盖。
- Celery 普通、CPU/GPU、维护、导出和图像金字塔分别承载不同资源成本与权限。CPU 预标只接整条链明确为 CPU 的工作，未知或混合仍走 GPU；金字塔派生资产的单并发队列不能抢占普通媒体转换预算，派生失败也不能阻断源图。`inspect ping` 仅探测当前节点，以 `CMD-SHELL` 保留容器侧 `$$HOSTNAME` 展开，为冷启动及在途任务排空留出时间；beat 不响应该探针且不可扩副本，否则周期任务重复。共享 DuckDB 文件由 worker 写、API 只读，不搬进命名卷覆盖文件。
- 开发 bind mount 替换 `/app`，镜像依赖必须留在 `/opt/venv`，宿主 `.venv` 由独立挂载遮挡；业务源码修改要刷新实际消费者，改依赖或构建输入才重建。API 冷镜像也必须包含所有实际运行依赖和明确复制的私有 fixture 命令及资源，不能假定有源码 checkout。`.dockerignore` 隔离旧字节码、依赖、构建产物与本地凭据；根 `packageManager` 让 Corepack 使用与锁文件匹配的 pnpm，产品 Web 的发布说明在构建期消费根 Changelog，不能靠容器环境事后注入。私有运行目录和输出不得提交或跨 checkout 复制，本地配置、上游镜像 checkout、独立插件仓库不参与版本化；受跟踪的快照和本地生成的站点镜像是不同工件。
- Named volume → `docker-compose.hostvols.yml` 只改挂载，不迁移旧字节。`DATA_ROOT` 是 daemon 可见的既有绝对目录，各子目录要先创建；映射成功不是数据已复制和核验的证明。SFTP 的只读 `known_hosts` 用审过的外部主机公钥填充，空文件拒绝未知主机，不能在首次连接自动收录。
- 推理共享 Compose 只描述共同存储，各官方插件通过显式 overlay/profile、已接受的仓库和不可变镜像/模型资产选入；不把模型构建或测试塞回平台 CI。CPU 变体与显式 NVIDIA 请求不能互相冒充。YOLO 与训练同机可共享一份经核验的物理资产目录，异机分别准备相同不可变内容而非照搬路径；详见 [插件安装边界](../0180-installable-inference-plugins.md) 与 [模型身份和资产](0090-versioned-yolo-identities-and-content-assets.md)。Assets Source、Media 和 Cleanup 的原生金字塔 bind/profile 也须显式选入：只有 Source signer 持有受限对象凭据；读、写、清理的调用身份独立，无权回退到缓存凭据或别的 signer。Source-only 部署不打开该组合。Jobs、Labeling、Inference、Workspace 的专用开发 overlay 由对应 owner 装配现有服务，不接管基础设施或暗中另启一个业务 owner。
- 浏览器使用同源 `/api/v1`，Nginx 代理 API/WS，并用 Compose DNS 在服务重建后刷新地址。页面 CSP 用同一个请求 nonce 替换所有 Vite 注入位置并写响应头；API 自有无 nonce CSP（[0010](0010-security-headers-middleware.md)）。先挡 `/metrics` 和 `/api/v1/internal/`，再匹配对外 API；监控拓扑和项目标识不应经业务代理外泄。代理签名下载时可去除公开前缀，但必须保留签名查询与签名端点 Host；边缘只放行本部署桶。LAN 的 IP 证书要显式匹配无 SNI 客户端，信任安装属于客户端，不从 gateway 容器隐式修改。
- 生产使用独立监控配置；开发 Prometheus 从 API 的 HTTP SD 自动发现已登记推理实例，容器视角必须能经宿主 gateway 到达 API 与各目标，宿主只监听回环时探测不会通。只有 `disconnected` 退出告警集合；探活 `error` 仍应经过告警持续阈值报告。延迟计数与告警实例含 `instance` 而不是按可重名的 `service` 合并。Alertmanager 的分组/去重与 SMTP 是其自身配置，不读应用 `.env`；开发路径经宿主 gateway 到 Mailpit，Grafana 数据源和 dashboards 用自举配置，不依赖个人管理员手工导入。

### 数据库安装与调用者（数据库迁移维护者、各数据 owner）

- 以 [同库事务合同](../0092-transactional-module-data-capabilities.md) 为基础。`infra/docker/module-data-grants.sql` 的前置是当前迁移和已存在的角色，安装者是显式目标数据库的迁移 owner。schema/DDL 留给迁移者；业务实现由各独立 `NOLOGIN` 角色拥有，只取刚好的关系、列和跨域函数。随后按 profile 应用需要的 `lan-production-grants.sql` 或各域专用授予，Assets Media 必须已装 Catalog、Source、Import、Operator，Image 再依赖 Media；Notifications append 先安装 preferences。用 `psql -v` 明示数据库及相关角色，并让安装在一个事务内出错即停；专用 LOGIN 不靠继承疏通权限，不接受直接表/列或未声明的能力。某些安装器不修改 `PUBLIC`，数据库配给者先撤 `PUBLIC TEMP`，不能以扩大安装副作用来绕过。撤整表权限不撤独立列 ACL，必须分别清理、用有效权限而非角色名验收。
- `lan-production-grants.sql` 是嵌入式 API/Celery 的有界组合，不是任何独立 owner 的万能连接。现有维护、迁移、受控兼容读写与普通 runtime 必须分清；迁移角色可继承 runtime，不能反过来。嵌入式视频/图像只取得已发布的 workflow/control/worker/Labeling 合同，不取得 sweep、垃圾回收或私有 pin/receipt。Workspace、Identity 存储不能靠余留默认授予穿过边界。常规 API/worker 不因数据库后缀而得到 fixture、seed、恢复或清理权限，受信操作人员显式叠加窄 profile。详见 [Identity 消费边界](../0162-identity-read-relations-and-consumer-grants.md)、[Workspace 消费边界](../0179-workspace-consumer-credential-closure.md)、[原生视频](../0107-scoped-native-video-stages.md) 和 [原生图片 owner](../0106-independent-image-media-owner.md)。
- 精确授予不等于不持锁：PostgreSQL 的 `FOR SHARE` / `FOR KEY SHARE` 需要对至少一列有 `UPDATE`，不得因此赋予目标或代次的业务写权；要保护尚不存在的 connector policy 行，用授权的表级 SHARE 锁挡住并发插入。跨 owner 的 dataset → 当前权限 → association → Jobs/Labeling 锁序和业务事务不变。私有 MV 的并发刷新由固定 owner 创建过渡关系。导出物理回执、原生视频窗口、恢复与删除凭据由各自 owner 持有；业务 Job 完成或删除不是物理结算。
- 历史角色审计安装器允许在对应新 schema 之前运行；它不是 Alembic revision，不创建 LOGIN 或改业务行。由 PostgreSQL 自身反解析临时参考视图，对照现有视图，不为校验而替换其 OID。保留 `-- aap-statement-break` 这样的程序分句输入，历史迁移及被验证的冻结 SQL/函数体不随注释整理改字节；不向旧迁移回写 owner 修订。Worktree 从 LAN grants 中取精确 `DO $…_private_acl$` 边界以复用权限，不复制 SQL 实现；这些边界是语法而非说明。重新生成的验收证据须绑定本次 SQL 输入摘要、实际安装定义与角色，不能拿旧报告把新 grant 标作通过。需要对照旧安装时用第二个受控数据库，不为了比较而降级并重新授予已废弃写能力。

### CI、快照与供应链（仓库 CI、SDK 与发布维护者）

- 稳定的 required job 名是分支保护输入。后端以同一可复用 workflow 的四个静态 shard 启动隔离的实际 runner，按测试文件确定性分割 pytest，各 shard 自己准备依赖和服务。聚合 job 不运行测试，必须看到每个真实 shard 成功；跳过、取消、缺失或失败均不是成功。仅 PR 可取消已被取代的运行；main 的验证都保留。测试专用 router 在 pytest 首次导入应用之前开启；迁移纯规则测试从脚本目录显式接入 API venv。
- E2E 以 `scripts/plan-e2e-suites.mjs` 与测试注册表选择实际 scope，而不是从事件名猜。docs-only 是明确的文本白名单；可执行示例、工作流、主题、应用/fixture 下的 Markdown 不享受它。空 diff、未知路径、未映射运行依赖、跨域或含删除/重命名的变更应扩宽；用无重命名折叠的 NUL 路径列表保留删除和特殊字符。宽选择不仅有 Chromium 分片，还有需单独项目/开关的点云、Mask 和其它实际覆盖；core 必须首过，其它重试只提供诊断或受具体 suite 规则约束。手动 scope 明示，定时默认 full；`E2E_SELECTION_MODE=legacy` 恢复冻结选择，不重命名 `Frontend E2E`，每次保留比较数据。计划演进见[范围调度记录](https://github.com/YOAO-Labs/ai-annotation-platform/blob/main/docs/research/43-repository-optimization-p9-shadow-comparison.md)。
- Built suite 按提交、build mode、锁文件、OpenAPI 快照、Node 版本、`VITE_*` 和根 `.env` 的精确指纹共享一次构建。下载必须有匹配产物，校验的文件路径与随后解包的路径必须相同；当前清单只查文件名子串，尚未满足精确绑定，见 [#179](https://github.com/YOAO-Labs/ai-annotation-platform/issues/179)。构建失败让依赖 suite 跳过，但 required 聚合拒绝这类 skip；只有没选 built suite 才可少建。仅有限 suite IDs/策略经 GitHub 输出和环境传递，完整 changed-path/shadow JSON 落文件防止环境大小限制。完成工件含对应 suite ID、已知 outcome 与数字统计；已计划却无实际报告即失败，只有分类及原因明确的 docs-only 可跳过。报告保留首次失败、flaky、超时、中断、未运行及真实 skip 的区别，不能拿最后一次成功掩盖无覆盖。每个 runner 自有 fixtures；仅 coordinator 收到 owner 连接，产品子进程只得自己拥有的库及不同的 `_app` LOGIN。
- 使用账本固定的 MinIO 镜像摘要；上游 Quay 分发撤下时更换镜像地址但不能改成漂移标签（[IBM 镜像公告](https://www.ibm.com/support/pages/node/7289585)）。Actions service 配置不能给该镜像传 `server` 子命令的路径，通过显式容器启动并等待健康；CI 用同包集合的 HTTPS 软件仓，避免区域 HTTP mirror 耗尽 suite 限时。CPU 合同执行与选择分离：`scripts/test-suites.json` 拥有平台 suite 定义，每 suite 独立 venv，防止 stub 或模块缓存互相污染；不接受未知名或 shell 通配符，清理依赖记录在主 shell，而非为取路径在 subshell 创建。平台 CPU 测试不代表 GPU 或真实模型资格。SDK 的安装验收使用构建后的 wheel/sdist 和受控插件包，而不是用共享开发环境的 editable 源遮盖缺文件或漏声明依赖。
- OpenAPI 与 capability registry 都从运行应用的同一合同/序列化生成，受版本控制的快照是 codegen 输入。应用版本和标题也构成 OpenAPI 信息；快照更新规则覆盖配置，不仅是路由。前端词表从 registry 生成；API/Web 开发的局部构建不能静默使用旧快照。pre-commit 对关键源重生成并暂存对应工件，CI 再比较；全仓格式检查不重写第三方、生成物、锁文件和不可变历史，YAML 语法检查允许 Compose 标签。Node 基线由 engines 与 `.npmrc` 强制，不把低版本降级成安装警告。工作流名称来自静态标量，稳定 ID 和各自唯一的显示名都要保留。版本沿革前缀扫描对当前叙述文档是提醒，不把 API、依赖或数据格式版本当产品历史；历史存放位置另行校验。Codecov 的项目基线和变更行覆盖率检查彼此独立。

### 文档的程序输入与浏览器行为（文档站维护者）

- [文档信息架构](0016-docs-ia-redesign.md) 拥有内容分域。`docs-site/.vitepress/content.mjs` 共享物理路径到公开 URL 的映射，生成镜像、可运行示例、维护材料不被误当新正文。ADR/Changelog/Roadmap 由源重建，Accepted 归档仍可引用但不进入活动侧栏；没有生成侧栏的工作区也应可启动。ADR 校验同时看两个目录，编号不可因归档重用。保持归档路线的 URL、同域 anchor，并让镜像外的仓库计划、研究、维护材料跳回真源；历史 Markdown 的 JSX 形态会触发 Vue 插值，镜像转义须尊重 fence 和 inline code，不改作者原件。版本页按数值版本而非文件名字典序排；站点 sitemap 的 hostname 是源站，所有引用保留部署 base。`llms.txt` 与全文从同一公开正文生成，位于部署子路径，不维护第二份独立语料。
- 文档生成器消费受控清单而非运行应用入口：快捷键使用 TS AST 校验字面量、稳定 ID、场景与动作；常用列表是精选视图，不丢条目的原类别。设置生成读取展示字段、组与节及账号缺省树，隐藏项和无字段的共享面板不产空文档；本机逃生开关的缺省应与运行时一致，手写覆盖集合是单独跟踪的债务 [#161](https://github.com/YOAO-Labs/ai-annotation-platform/issues/161)。路由索引用文件及 decorator 反查来源，不取代 OpenAPI 契约。训练表格来自训练合同，Markdown 对齐空白不是单元格值；两个确定的 HTML 界标各自只能出现一次。代码生成 `--check` 把缺文件/内容漂移视为失败，而不是在校验中偷偷覆盖；先物化公开 OpenAPI 和 ADR 镜像再检查应存在的链接目标。
- 注释在以下明确定义的场合是输入：`.env.example` 里的分组标题、紧邻变量的说明及注释式可选赋值生成环境参考；`<!-- snippet:… -->` 打开代码镜像，整文件、Python 函数/类（含相邻装饰器）或旧行段和 Markdown fence 精确比对，改 echo 示例必须同步镜像，不能靠检查忽略注释；Excalidraw SVG 内嵌 scene 与 Virgil 字体，既是渲染也是可编辑源；训练参考与上述 snippet 的 Markdown 边界不是解释性注释。别删解释相似的 shebang、PEP 723 元数据、lint/格式指令、managed ignore 起止线或 SQL 分隔符。GitHub Actions `run: |` 是 shell 程序，`with:` 提示文本和 heredoc 数据不是；处理 shell 时应先识别续行、引用和实际注释边界，不整行正则扫 `#`。
- `.gitignore` 中 Orca 的 `.orca-dag/` 规则与它的两条固定起止注释是一个受管理块：独立 launcher 自动安装，卸载基于完整界标移除；原生插件则不改 `.gitignore`。普通分组说明没有这层合同，不能据此把整个 ignore 文件冻结不动。
- CI 把 frontmatter、快照、snippet 不一致等实际合同问题与维护提醒分开。正文标题取 H1，不把发布历史作为当前文档元数据；freshness、计划结项提示默认只警告，导航可达性/可读性工具可显式 strict。媒体检查从正文与主题组件收集实际媒体，路径解析错误继续报告，内容重复只按完全一致的路径集合精确放行。死链按源类型解析站点绝对路径、rewrites、镜像源、public 和附行号的源码引用；不从 vendor、构建物或不可变历史主动扫假死链，代码示例屏蔽时保留行号。已删除 service facade 的活跃引用故意连字符串都查，不仅静态 import；历史文档可叙述旧事实，但不应链接已移除文件。
- 首页品牌和正文阅读层共享本地字体与色源，不共享首页强背景和动效；正文保持低干扰的一栏阅读、稳固滚动条、明显焦点和高对比。明/暗成对素材依次为 light/dark；Scalar 通过带 base 的 iframe 隔离站点样式。动画仅在客户端装配、一次调度合并滚动/指针更新，离开首页清理状态；无 JS、reduced-motion 和移动端始终可读，前两者不先隐藏内容，精细指针设备才开指针/透视动效。固定全屏装饰不使用滚动重绘的 blend，导航精简滚动时的实时模糊。通用图片 zoom 委托且避免重复初始化，链接、禁用标记及小图标不拦截；视频自动播放被拒仍可手动播放。平台键映射只在客户端路由生命周期更新，比较 DOM 值，不用无条件 body observer 打出自触发循环。
- VitePress 序列化到客户端的配置回调应自包含；本地搜索索引不是异常大业务 chunk。Mermaid 客户端异步插图，以有有效 `viewBox` 的 SVG 初始化；用于 `svg-pan-zoom` 的宽高按容器以像素给出，控制按钮在 HTML 层，避免 SVG 内置按钮随图坐标放大成遮罩，容器变化后重新适配。Mermaid/Day.js 的 ESM 入口要与实际锁定依赖核对，不把上游包临时导入顺序当正式接口。可运行的协议示例单独测试、部署路径不依赖测试 extra；echo 只公开单条无交互能力，Mock 的旧变体归一化、多轴非笛卡尔组合、显式默认值及模拟不可用响应是 [多模型协议](0036-ml-backend-capability-protocol-v2-multi-model.md) 的示例向量，不能用作真实模型或性能资格。

### 工作区、运行证据及存储操作（运行工具及数据运维维护者）

- Orca setup 只准备环境，受控 launcher 才拥有资源生命周期；保留本地文件及悬空 symlink。分享 Node 安装须工作区依赖源与主 checkout 一致、且主 checkout 已安装锁指纹也一致；否则本地准备，不经共享 symlink 安装。Python editable venv 和生成客户端永远本地。Claude 工作区 hook 强制建议可写代理用 worktree，并限制该会话的编辑工具不得逸出，主会话、Bash 与无法解释的 payload 不视为强沙箱；Codex context hook 附加采集失败仍保留基础硬件上下文。
- Worktree 的数据库、Redis、bucket、端口和进程以已验证的所有权/出生证据为准，不凭名字接管。老清单可 `doctor/stop/destroy`，仅显式 prepare 可升级拓扑。写入前再比对实际目标，资源创建前持久化身份/补偿意图；未知创建结果不能假作已归属，清理只动本调用证实创建且仍为空/无标记的资源，不强制 DROP 或按重用名字杀容器。Alembic 前验证物理数据库和准确 head；创建后、数据库标注或连接释放之前就登记待清理对象。产品子进程走本 checkout 同源 Vite 代理，配置中过滤继承的所有大小写私有 DB selector；监听端口不等于 API/Vite 已初始化。开发重启按已验证 ID 操作；生产健康恢复先核实 CA、只在连续计划观察失败后记录一次重启动作，由下一次定时器等待并验证，暂停的容器由其 owner 决策，不凭旧失败积累无限重启。
- Linux owner 先建立 subreaper，记录实际 `Popen`、pidfd、出生信息和范围，不信 PID/标题或可被借用的进程组；Python 直接调用时 shell 组可能是用户的。清理失败或动作超时不意味着物理 work 停止；准确句柄留待恢复。主动发信号的退出状态必须核对，独立 docker stop 观察限时不杀仍在排空的 stop 客户端。锁文件保留 inode，避免另一个 owner 打开新 inode。Vite/ASGI 关闭由受控命令和真实回收证明，不把 stdin 意外断开、信号重放、最后一次应答遗失或 404 单独当结算。临时放开的 fixture 权限、存储通道和子进程证据只给私有 supervisor，普通产品用受限 LOGIN；退出回执、准确 pidfd 和控制目录收回应各有证明。重放站点 Agent 只交还已记录的不可变结果，不重复计字节，过期和 takeover 用真实租约及 checkpoint，不用 SQL 补造成功。
- 存储脚本延续 [保存位置/内容身份](0091-dataset-content-and-location-identity.md)、[受控存储注册表](0092-controlled-storage-registry.md) 和 [外部 S3 引用](../0098-strict-external-s3-references.md)。物理 I/O 不留业务事务，只有短锁保护持久意图的比较与提交；保留实际尚未结束的线程、读写 permit 与原生 Journal。Cutover 先持久化 B 的核验和 A fencing 责任；来源 ingress 一份 Journal，文件不沿祖先 symlink，CAS 失败或不明结果不准重开。Finalization 区分撤回公开状态、无回执与确证释放；先提交失效再解除原生 interlock，未知提交不得移除意图。真实业务已经无源或权限变化时仍保留恢复证据。不能把变更当前路由当历史回执、对象键或源 attempt 已移动；离线恢复只写受控当前 locator，历史 ZIP 签名、创建事实、失败和 pending 记录不重造或自动重发。
- 验收对比当前与保存位置、明确 `VersionId`、全字节 SHA/大小、当前身份和 scoped permit；只读 profile 验证真实权限，不只查事务默认。密钥、签名 URL、原始 SDK/SQL 消息与逐行物理映射留在私有有界证据，CLI/日志只报受控字段。相同名字、ETag 或声明元数据不是内容证明；空 S3 对象留显式 null 版本（若源已存在该版本），空 PUT 不附 `Body` 避免 botocore 的流式 `Expect: 100-continue` 影响后续连接。签名默认端口和 Host 表示保持一致，不按文件路径规范化 opaque key。标准/原生 signer、四条普通清理 lane、注册连接和输出 worker 的物理根与授权各自独立；对象列表需把所有页读完，未证明授权或普通清理不能扩展到原生根。验收注入只在私有 harness，不给生产操作命令加入故障开关；拿 transport sentinel、模块/浏览器探针或人工控制的 fixture 指标不能声称验证了实物 codec、GPU 或真实编辑 UI。

## Consequences

正向：换权限、包、镜像、代理路由或路径时可以定位实际消费者，未完成的门禁或物理操作不会变成假成功；脚本和包也不会依赖当前机器或作者记忆才能复现。

负向：同一变更可能要同时更新源合同、模板/生成物和实际部署配置。文档解释可以搬走，但程序界标、冻结字节和本次结算证据不能用改写文档替代。生产开发收件箱、本机默认值重复定义、构建归档与校验未精确绑定仍由对应 issue 跟踪；本记录不宣称已经修复。

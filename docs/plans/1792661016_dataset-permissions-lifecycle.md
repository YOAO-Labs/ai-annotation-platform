# EPIC-11：权限、删除与保留治理

> 状态：approved；2026-10-24 的 E11-B1/E09-B3 位置盘点、E11-B2 位置配置确认、E11-B3 数据集归档/恢复、E11-C1 逻辑位置访问维护、E11-C2-A 托管原件保留预演与 E11-C2-B 显式策略/声明要求已完成；整项 E11 继续活动，未分配发布版本。
> 晋升基线：`802e6e4df`；工作树干净，保留 3100/8100、两个 Worker 与 beat，不重建镜像。
> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 属性     | 草案约定                                                           |
| -------- | ------------------------------------------------------------------ |
| 优先级   | P0：原地目录上线必需的授权、只读边界和内容保护；P1：保留与回收治理 |
| 负责角色 | 后端、产品、运维、前端、测试；复用现有权限与审计负责人             |
| 依赖     | 与 EPIC-01/02/03/05 同步定义对象、位置和读取权限，不作为末尾补丁   |
| 协同     | EPIC-04 的并发任务；EPIC-08/09 的操作反馈；EPIC-10 的冻结引用      |
| 交付阶段 | M1 最小安全闭环；M2 完整影响预览、保留与回收；M3 站点授权扩展      |

## 本轮定稿：E11-C2-B 显式保留策略与备份/迁移保留要求

2026-10-24 复核基线`a9db710fb`，工作树干净，沿用整套实施、提交及独立子代理授权。使用现有保留断言的policy_revision/retain_until，不推定生产保留天数。当前单行期限判断在开放设置前须补齐同桶同键相关记录保护；这些记录可能来自不同端点，保守归组不宣称已证明同一物理副本。

超级管理员与全权Key可将一条已登记断言设置为无限期，或明确带时区、晚于数据库当前时间的期限；缩短无限期/既有期限也须单独预览、非空原因及修订CAS。新增独立hold表，锚定保留断言且RESTRICT删除，无业务/操作者级联外键；保存客户端生成并绑定预览的hold UUID、backup/migration类别、简短引用编号、登记原因/历史操作者/数据库时间、修订及解除事实。登记字段不修改；解除保留历史，只允许经原锚点、精确hold ID/修订和独立原因确认。无TTL或按任务状态自动解除，期限调整不解除hold，hold解除不改变任何期限。

所有相关断言先按完整bucket/object_key保守归组再分页：任一null期限即整组无限期，否则取最大期限；任一peer上的有效hold保护全组。返回单条自身策略与组内有效保护分别表达，计数仍为断言，已记字节仍可能重复。保持未知独占所有权、现场内容/副本、未登记备份/迁移覆盖和execution_permission=false；hold只是已声明要求，不能称真实恢复点证明、S3对象锁、仍活跃原件或全平台所有清理器的保留锁。

新增GET `/storage/managed-original-retirements/{id}/retention`与POST同路径`/retention-preview`、`/retention`。读取返回当前修订、自身期限、整组有效期限/peer数、有效两类hold数及有界的相关hold历史；不返回bucket/key/endpoint。预览接收set_retention/add_hold/release_hold、expected_revision、原因和对应候选，返回前后保护摘要、是否缩短、观测/五分钟有效时间及签名确认令牌。提交绑定当前操作者/Key权威、锚点/候选/原因/修订、平台配置及完整peer/hold指纹，再复验期限尚未来到；令牌过期/冲突/撤权需重预览。成功变更与安全字段审计同事务，不独立提交、读对象或发任务，失败不留半状态。

控制写入按User→Key→bucket/key事务advisory lock→锚点→hold的顺序，所有保留/hold修改共用组锁；不锁Dataset/Item或任意peer行。既有捕获仍在业务删除事务中追加默认无限期记录，不为本片增加跨组锁；并发新增只加强保护，展示的peer数和指纹是提交时重新观测，不是阻止捕获或授予后续物理执行的屏障。新hold UUID与数据库唯一键防止丢响应后产生同一请求的重复记录，提交修订防重放；其他锚点不能解除该hold。

```text
registered retirement assertion -> own policy + immutable anchored hold history
                  |                                |
                  +---- bucket/key conservative group ----+
                                      |
            full-group effective retention / active requirements
                                      |
                  SQL preview -> centered Web settings
                                      |
     actor/key + group lock + CAS + reviewed candidate -> same-tx change/audit
                                      |
              offline restore preserves anchor IDs + complete hold rows
```

本片超过8文件、跨DB/API/Web/恢复工具与正式文档。隔离ownership切片负责hold模型/注册、0217迁移和原件定位摘要索引、retention schema/service、既有storage_reclamation router接入及新合同/并发/迁移测试；隔离planner切片负责现有reclamation schema/service中的全组保护、关联hold/生效策略输出与对应回归。主树负责Web原件详情内默认折叠设置、有界hold历史及居中预览确认/测试、恢复fixture与实测、OpenAPI/TS/API索引/SDK覆盖、用户/API/架构/运维文档、README/DEV/CHANGELOG与执行队列。无新依赖、环境变量、服务或发布版本。

验证使用核实一次性test模式，覆盖时区/过去期限/确认期间到期、缩短/无限期与自身/整组区别、null与最大有限期限、不同身份保守peer、跨peer hold与新增peer、精确锚点解除、重复UUID/重放/旧修订/同数替换、当前角色/Key/配置变化、并发组写/锁忙和审计失败回滚；GET/预览纯SELECT、零对象I/O、分页及完整统计。恢复fixture包含多peer、有效与已解除hold，源销毁后实际隔离恢复逐字段保持，只按既有白名单重定位退休锚点；未知身份拒绝。API/Worker/beat只在核实owned空闲后刷新，保留3100/8100、既有业务/原件/冻结包，不重建镜像或改共享.env。

Web复用既有居中Modal、13px正文/11px次要提示、下拉标签上/说明下；设置按需读取，会话/锚点/修订隔离，编辑后丢弃旧确认，缩短明确显示，解除只影响选中要求，网络失败与撤权分别处理。真实临时托管样本验证期限设置、两类登记/解除、整组保护、冲突与审计，1440/390明暗、Escape/焦点/滚动/溢出及网络/控制台；完整合同、Ruff/tsc/lint/CSS tokens、文档检查和正常提交钩子。按owner清理测试业务/精确自有原件/hold/断言、test DB/Redis/九桶、截图/报告/构建缓存及已等价集成子工作树，保留人工环境。

回退保留策略与hold事实；存在hold或非初始策略时0217不得破坏性降级。完成后追加Outcome。真实恢复点身份/耐久性与迁移资格、原件独占所有权/现场副本、全物理读写屏障及可恢复执行仍是后续门禁，不能因本片或空hold列表推断已经完成，E11/E09继续活动。

## 本轮定稿：E11-C2-A 托管原件保留账本与回收预演

2026-10-24 复核基线 `dd2fdf428`，工作树干净，沿用整套实施、提交及独立子代理授权。C1的逻辑Location排空不提供物理回收许可；复核发现Dataset/Item硬删除保留托管原件字节，但Item→Asset→AssetLocation级联会丢失精确位置。先补齐这一不可恢复的证据缺口，不通过扫描桶前缀推断所有权，也不把现有缓存清理误作dry-run。

交付独立的`managed_original_retirements`账本，0216仅加表。DatasetService在既有删除锁和同一业务事务中，于级联删除前保存每条managed_original的精确内部locator、历史Dataset/Item/Asset/Location ID、可证明的配置/绑定修订及存储身份、已有摘要/大小与核验时间。无业务级联外键；失败/回滚不留下已退休断言，重复捕获幂等。历史未知位置、未验证内容或无法确认的绑定原样标为未知，不凭角色名、键格式或同SHA认定独占所有权，也不因这类未知状态阻断原有合法逻辑删除。外部reference、缓存及派生预览不进入此原件账本；已经丢失的历史locator不伪造回填。

每条断言默认`policy_revision=1`、`retain_until=null`，表示未指定保留策略、无限期保护。本片不引入保留天数默认值、缩短保留接口或生产恢复承诺。现有cache/input的retain_until只保护不确定写入的恢复账本，字节可能先被删除，不能当作内容保留锁。训练、预览、金字塔和临时输入继续由原owner负责，不把本账本描述为已经覆盖这些物理清理。

新增只读GET `/storage/managed-original-reclamation`，仅当前超级管理员与`*` scope，前后复验当前身份/Key及相关平台配置。以同一SQL观测时间和有界分页给出断言、已记录字节/未知数量与保留原因；统计单位明确是退休断言，已知同一物理locator不能冒充多个独立可释放对象。重新核对当前AssetLocation、旧Item/Task定位、适用Task关联和active-learning历史根，以及全部保留状态训练snapshot中的精确source_bucket/source_key；归档、失败/取消训练仍保留，未知/不支持snapshot结构明确阻断。缺生产恢复点/迁移保留登记、回源和最小副本证明时标为未覆盖，不能返回零引用或安全可删结论。固定`execution_permission=false`，不签发执行令牌、不改状态、不调用源/对象I/O或派发任务。

```text
Dataset/Item hard-delete transaction -> capture managed-original assertions -> business cascade
                                               | same commit / rollback          |
                                               v                                 v
                                  independent retention ledger <--- survives business deletion
                                               |
                     current exact-locator roots + all retained frozen snapshots
                                               |
                         pure SQL non-executing preview -> Storage Web disclosure
                                               |
                         offline owned recovery -> explicit physical remapping
```

范围超过8文件、跨数据库/API/Web/恢复工具及正式文档。隔离ownership切片负责模型注册、0216、capture服务、Dataset删除接缝和所有权/迁移测试；隔离planner切片负责只读服务、schema/router注册和API/DB回归。主树负责Web存储页折叠预演/分页/居中详情与测试、离线恢复白名单/fixture/回归、OpenAPI/TS/API索引/SDK覆盖、用户/API/架构/恢复说明、README/CHANGELOG及执行队列。无新依赖、环境变量或服务。代码回退保留退休账本；有断言时0216不得破坏性降级。

验收使用已核实一次性test DB：删除后locator/历史身份保留、外部/缓存角色排除、同事务失败与幂等、未知旧证据、共享locator及归档/训练全部状态/历史active-learning/畸形snapshot保护、配置/身份/Key前后变化、纯SELECT与零物理调用、固定查询数和分页边界。恢复测试覆盖新物理owner的精确重定位、未知/外部身份拒绝、保留历史断言和不启动队列。浏览器在保留3100/8100以任务自有托管样本真实删除验证原件字节仍在、断言/阻断原因可见；既有人工数据只读，1440/390明暗、Escape/焦点、分页及网络/溢出验证。迁移/所属API与Worker刷新遵循owned环境核验，不重建镜像、不改共享.env；清理临时业务、任务自有原件/断言、test DB/Redis/九桶、截图/报告/构建缓存与已集成子工作树，保留人工环境。

本片为C2-A，完整C2还须明确实际保留策略、可核验恢复点/迁移保留、独占所有权/副本与物理访问资格、最终执行互斥及可恢复幂等删除。未完成这些门禁不能将预演或C1状态提升为物理许可，E11/E09和整套Epic不因此完成或归档。

## 本轮定稿：E11-C1 位置访问维护准入与排空

基线 `40952845b`，沿用整套实施和提交授权。交付外部逻辑位置的访问维护：active→draining→stopped，或显式resume回active；drain/resume递增独立maintenance_revision，stop也递增以保护确认重放。它关闭本位置受控来源/缓存及持久连接复制导入与连接测试的访问准入，等待实际已登记源I/O收尾后确认停止；保留已有业务关系和独立冻结内容。另一Location、重叠根、候选连接验证、直接宿主机/外部客户端与平台自有副本清理不纳入源位置静止证明，不提供卸载挂载、退役或物理删除许可。

使用独立Gate和无过期Operation receipt；不以run lease、AsyncJob终态、心跳或TTL推断物理结束。Gate无业务Location外键，避免调用者未提交Location及外层锁导致独立登记事务自等待，同时保留业务删除后的访问责任。Operation指向Gate且直接保存位置、代次、目的、节点与开始时间，不保存路径/凭据；只在实际线程/句柄收尾后独立删除。进程中断或收尾DB失败保留未知记录并阻止停止，不自动清零，不开放无证据强制解除。

准入事务只锁Gate SHARE、登记receipt并提交；不回读或锁Location。控制端User→Key→Location NOWAIT→Gate NOWAIT，发布端在已有业务锁之后持Gate SHARE并核对代次，独立收尾只碰receipt。content_snapshot在取消/超时后等待真实executor Future收尾并处理迟到Snapshot，其他SDK/文件线程复用同一收尾原则。所有跨停止/恢复的Scan/Registration/Sync、Quality、预测/修复/采纳、媒体导出、新冻结和持久连接复制导入在委托/内容证明中携带代次；历史缺字段按1解释，新请求在恢复后的当前权限下重新建立证明，旧队列不重放。

新增0215迁移，历史位置为active/1。`STORAGE_MAINTENANCE_PROTOCOL_ENABLED=false`默认关闭维护控制；新代码访问登记始终执行。启用前必须核实该数据库命名空间全部旧API/队列消费者已退出并切换新协议，账本初始空不构成旧进程静止证明。人工环境只在确认所属服务空闲、刷新两个Worker/API与beat后单独启用，不改共享.env或重建镜像。

新增GET `/{id}/maintenance` 与POST `/{id}/maintenance-preview`、`/{id}/maintenance`，仅当前超级管理员及对应datasets:read/write。操作为drain/stop/resume，必须绑定expected_revision和五分钟impact_token；沿用位置影响语义和单SQL关系指纹，提交重查身份/Key/Location及Gate修订和状态。stop预览和提交均要求没有未收尾receipt。状态返回固定范围声明、无物理删除许可、未收尾总数及最多50条安全记录；不自动启动、取消或强制终止业务任务。居中位置详情中的维护折叠显示当前状态、明确范围、未收尾操作和预览确认，默认不额外请求；明暗/窄屏与会话/对象/修订隔离遵循现有组件。

```text
Location management -> reviewed drain -> Gate draining + revision
                                      | blocks new admission
all covered source I/O -> Gate SHARE -> durable Operation -> physical thread/handle -> settle
                                      |                                  |
final owner publication -> Gate SHARE + captured revision                 |
reviewed stop -> Gate UPDATE + zero unresolved Operations <---------------+
reviewed resume -> active + fresh revision; no replay of old delegations
```

范围超过8文件。隔离A负责models/storage_maintenance、0215、services/storage_access和storage_maintenance、独立schema/router、config及核心/迁移测试；B负责扫描/登记/同步、storage_location的probe/browse及Out、持久连接复制导入/测试及相关API与测试；C负责content_snapshot、dataset_content/references、reference_bindings与预测/修复/导出/冻结消费证明、original_cache及Quality与测试。主树负责Web API/维护组件/入口及测试、环境说明/SDK覆盖、OpenAPI/TS/API索引、用户/API/架构文档、CHANGELOG/README/计划、真实运行验收和最终集成。无新增依赖或服务；配置只为防止混合协议部署产生虚假排空证明。

验证使用核实一次性test资源：并发准入/关门与最终提交串行化、真实阻塞线程取消/超时不提前settle、跨Connection调用不提交业务事务、未知记录不随时间消失、代次与身份/Key/重放矩阵、停止/恢复后旧工作拒绝、缓存旁路和全部I/O接缝、平台位置拒绝及重叠位置范围声明。真实人工环境只用自有临时位置/来源完成drain/stop/resume与Worker拒绝/新轮，保留既有数据和文件；浏览器1440/390明暗、焦点/Escape/溢出、控制台/请求验收。迁移、目标Worker刷新、新签名/实际派发、后端/Web/SDK合同、Ruff/tsc/lint/CSS tokens、文档与正常提交钩子均须完成。结束按owner销毁test资源与任务产物；发生维护变更或未收尾Operation时拒绝破坏性降级，不清除已有维护事实。

## 本轮定稿：E11-B3 数据集归档与恢复

基线 `d591508b0`，用户持续实施和提交授权有效。最直接方案是新增可逆的数据集生命周期，不复用硬删除、来源暂停或位置停用；这些操作会删除业务关系或阻断已有Task读取。保留现有DELETE含义，但归档状态拒绝DELETE，用户须先明确恢复再使用既有删除合同。归档仅更改Dataset生命周期，样本、来源配置/启用、项目关联、Task、标注、外部字节及已冻结内容不删除。当前创建者或超级管理员可预览并确认归档/恢复；本片不把非目录旧CRUD顺便改为新的全局ACL。

新增迁移0214：`archived_at`、`archived_by`及`lifecycle_revision`（历史active/1）。新增POST `/{dataset_id}/lifecycle-preview` 和 `/{dataset_id}/lifecycle`，operation为archive/restore、expected_revision必填，执行须带五分钟签名impact_token。预览返回安全计数/关系指纹、统计时间及有效期，绑定当前actor/Key、Dataset配置/状态和候选；提交在User→Key→Dataset NO KEY UPDATE NOWAIT短事务重新验证。过期、跨身份/Key、配置或关系变化、同数替换、重放拒绝。盘点冻结只认持久sample.dataset_id，缺证据历史明确未覆盖；不提供物理回收许可。成功审计记录差异、版本、计数与时间；失败请求独立审计且业务回滚。

```text
List active/archived/all -> Dataset detail read-only banner + lifecycle action
  creator/superadmin + datasets:write -> preview -> token -> recheck -> Dataset transition
                                                   |               |
 managed import/task tickets + scan/registration/sync snapshots <--- lifecycle revision
             |                                     |
 each short commit: current authority + active Dataset + captured revision
             |
 current Task reads/annotation and retained frozen packaging use existing project rights
```

归档阻止Dataset配置、上传完成、托管/连接/ZIP导入、成员删除、项目关联/解绑、Scene结构/标定，以及来源创建、扫描/登记、接受修订/重新核验/重定位、规则/同步和来源生命周期写操作。已有Task读取与标注继续遵守当前项目权限；个人阅读视图仍可保存。新冻结在全部选择模式中拒绝包含归档数据集的样本，已有冻结打包/下载不追加归档读取限制。

不能只看archived_at：新托管导入/建任务授权与Scan/Registration/Sync执行快照捕获lifecycle_revision，历史缺字段按1处理；每个最终业务写检查点复核。归档再恢复会增加修订，旧排队任务、完成观测的旧登记许可和同步委托仍失效，必须明确创建新轮；恢复不回放授权、成员/Key、来源启用或同步启用配置。来源锁序保持Location→Source→Dataset NOWAIT，归档不反向等待Location/Source，不在公共Task读取上下文塞入写门禁。

既有缩略图、视频元数据等派生媒体回填属于维护例外，仍可更新派生字段以保持已有Task可读；不携带生命周期票据，不据此宣称所有后台写入都被暂停。本阶段不新增全owner维护停止协议。

范围超过8文件，跨生命周期/API、托管及来源执行owner、Web/SDK、文档/运行时五组件，共同交付后独立可用。准确owner分工：隔离工作树A维护Dataset模型/Out、0214、新生命周期schema/service/router、v1路由注册与生命周期测试；B维护dataset_scans/registration/references/source_revisions/revalidation/relocation/source_lifecycle/rules/sync，以及独立Quality/Review授权、执行检查点和必要source/qualityAPI/worker与测试；C维护Web数据集API/hooks、列表/详情/URL、归档确认和来源只读展示及测试。主工作树维护DatasetService序列化/列表/写入口、dataset_source_access授权票据、datasets API、托管import/create_tasks/pointcloud、Scene结构及新冻结检查、Python SDK/CLI合同与测试、正式文档和集成。新增文件均按所属现有owner就近放置，不新增服务、依赖或环境变量；OpenAPI/TS/API索引与SDK覆盖同步，README、CHANGELOG、用户/API/架构/运行说明及E11/执行队列同次更新。

验证使用已核实一次性test模式：状态/授权/Key矩阵、过期/重放/同数替换、归档读写矩阵、文件摘要与身份保留、恢复不复活权限、托管/引用/混合及点云分块、排队任务/扫描/登记/同步跨归档恢复的拒绝、并发最终提交锁、冻结全部选择模式与历史包保留。执行定向API/Worker、Web、SDK/CLI、OpenAPI、Ruff、tsc、lint/css tokens、文档和正常提交钩子。浏览器无mock在3100/8100用自有临时Dataset验证创建/预览/归档/过滤/深链读取/恢复，明暗/桌面/窄屏、Escape/焦点与网络/控制台；保留既有人工作业和原件。先向已核实dev应用纯加列迁移再接入新模型，以免重载期间出现缺列；源码集成后通过拥有该环境的服务刷新两个worker，核实空闲、端口、任务注册与新执行屏障，不重建镜像；beat仅代码受影响时刷新。

回滚不删除业务内容：代码可撤回，但含已归档或生命周期修订>1的数据拒绝数据库降级，避免丢失生命周期事实。测试结束销毁test DB/Redis/九桶并清理自有临时资源；dev数据保留，记录实际迁移/刷新证据与未完成的全owner回收门禁。归档/恢复完成仍不代表整项E11或整套Epic完成。

## 本轮定稿：E11-B2 位置配置候选预览与确认

基线 `a937c4446`。用户已授权继续实施和提交；本片沿用既有位置配置权限，保存入口统一经过候选预览。新增 `POST /storage-locations/{id}/change-preview`，接收现有候选字段，返回安全字段差异、B1盘点、统计时间、五分钟有效期和签名确认令牌；`PATCH /storage-locations/{id}` 必须带同一候选与 `impact_token`。旧的直接 PATCH 客户端须先预览，缺令牌返回422。连接身份/凭据仍由连接更新接口管理。

预览不修改位置、不读源、不发任务。复用候选校验，令牌绑定当前超级管理员、API Key、位置配置/连接修订、标准化候选和同一SQL快照内的关系身份/状态指纹与计数；提交沿用 User→Key→Location 锁序复核权限、有效期和配置，在新快照发现新增/替换关系或数量/状态变化时拒绝并要求重预览。不能仅以总数相同放行。禁止持有Location后阻塞等待Task/Dataset；这些owner并非都由位置锁串行化，因此此合同只承诺提交时新快照与配置CAS，不承诺所有业务静止、同步取消所有作业或允许物理回收。

成功审计保留安全字段前后值、预览统计/有效时间、确认影响摘要与修订，不保存令牌、路径、连接秘密。失败不得修改位置。UI复用居中Modal，在原使用范围和缓存保存入口展示候选差异、影响及有效期；确认前不发PATCH，取消、过期、修改候选、切换位置/会话或撤权均不能复用旧确认。原缓存清理保持独立确认语义，人工验收数据保持。

准确文件范围：API的 `schemas/storage_location.py`、`api/v1/storage_locations.py`、`services/storage_location.py`、`services/storage_maintenance_impact.py`、新增 `services/storage_location_change.py` 及 `tests/test_storage_location_change.py`；既有位置/缓存/Workbench/S3测试按显式预览更新。Web新增 `StorageLocationChangeButton{.tsx,.test.tsx}`，接入 `FilesystemLocationsPanel`、`StorageOriginalCachePanel` 与相关回归。同步OpenAPI、API索引、本地TS、SDK覆盖清单，更新README、CHANGELOG、用户/API/架构文档、本计划与执行队列/E09。无迁移、依赖、环境变量或发布版本变动。

验证使用已核实的一次性test数据库，覆盖有效确认/审计、全部可编辑字段、标准化授权、过期/篡改/跨身份/Key/候选/位置/配置复用、同数替换与主要关系变化、撤权和平台/连接限制；保留B1回归。Web验证候选确认、取消/失败/过期/修改和会话隔离，执行tsc、lint和语义颜色检查；生成合同、SDK合同、文档检查和正常提交钩子。真实浏览器在3100/8100以独立会话验证明暗桌面/窄屏、Escape/焦点、溢出和网络错误；写入验收使用任务自有位置并清理，既有业务位置只读。测试后销毁本片test模式，清理自有临时产物，不重建镜像。

## 本轮定稿：E11-B1 / E09-B3 位置维护影响盘点

用户已授权持续推进整套 Epic，并允许独立子代理或串行执行。重新核对当前 CHANGELOG、E02/E05/E09/E10/E12、0213迁移头、位置权限/更新、来源生命周期、严格读取和完整缓存 owner。受控根、任务级读取、来源暂停/注销的保留、内容修订与完整原图门槛已随核心链路交付，不重复建设，也不据此宣称整个E11-A完成。数据集仍有硬删除合同，归档/受控恢复尚未交付；不从旧草案直接生成状态表。

本片交付超级管理员使用的只读位置盘点，补齐后续维护的可解释依据。它不生成变更许可或impact proof；提交绑定预览、归档/恢复、退役及物理回收仍需独立实现和并发资格门。现有配置保存/读取合同不变。

```text
Location centered modal -> collapsed maintenance disclosure -> GET maintenance-impact
  super_admin + datasets:read -> fresh actor/key/location authority
                                  | one SQL statement / stored dependencies
                                  v
  source/history + members + projects/tasks/annotations + durable active attempts
  schedules + preview records + original cache/read leases + linked input leases
  frozen versions with persisted location lineage (explicit historical coverage)
                                  |
                                  v
  fresh authority -> timestamped counts + behavior/coverage -> current UI session
```

### 合同与边界

- 只支持外部来源位置（filesystem 或 connection-backed 位置），平台用途位置不提供此维护合同。连接的登记类型不因此获得原地引用能力；SFTP也只盘点已有数据库关系。GET要求当前超级管理员与datasets:read，前后复核身份和Key；未授权不可通过聚合推断私有关系。无源I/O、对象HEAD/LIST、探测、写入、任务启动/取消或审计写入。
- 聚合本位置全部保留来源（含暂停、注销和dataset已删除的历史）。业务样本按采纳修订和位置Asset记录去重；引用样本另列，数据集/项目/任务/标注按当前真实关系去重。项目包括已有数据集关联、即使没有任务；历史断开的关系不回填。全量SQL聚合，不逐来源查询，返回固定大小。
- 活动扫描与预检/登记按持久run状态计数，不依赖短期AsyncJob；同步按enabled配置计数。位置修订变化在下一检查点使旧轮停止写入，自动同步在下次检查进入阻塞；不把保存说成已同步取消所有后台任务。
- 预览只报告ready/failed记录及已记录字节，不证明对象存在/当前可读。完整缓存按scope_location_id统计全部状态和已记字节，包含无业务FK保留的孤儿账本；活跃读租约以数据库时间判定。任意位置修订更新会令旧完整缓存失配，后续受控回收仍受租约约束；预览可按当前策略和内容继续复用。
- 临时预测输入只统计通过当前Task仍可归属本位置的租约，分active/cleanup_pending；不是所有预测/修复作业，也不涵盖已失去业务关系的临时输入。冻结只统计snapshot中持久source_lineage.location_id可证明的版本，区分ready与pending/running；托管和缺少位置血缘的历史不计入。明确partial coverage，不以零数值宣称无引用或允许退役/回收。
- 文案解释停用拒绝新引用原图读取（包括缓存），人员使用权撤回影响来源管理但不代替既有Task项目授权，已就绪冻结内容按项目权限独立保留。新功能不提供停用/归档/删除按钮，不扩大已有写权限。
- UI 在现有居中位置详情内以“维护影响”折叠显示，默认无额外请求，展开/刷新只发GET。时间、刷新失败保留的旧结果、加载/空值与撤权隐藏明确。角色/会话/位置/修订隔离缓存和迟到结果，明暗与桌面/窄屏无溢出；不打散现有来源诊断和返回导航。

### 准确文件范围、兼容与验证

本片超过8文件、跨API/查询合同/Web/文档四组件，保持共同只读合同一次提交：

- 后端：`apps/api/app/services/storage_maintenance_impact.py`、`app/schemas/storage_location.py`、`app/api/v1/storage_locations.py`、`tests/test_storage_maintenance_impact.py`（后三者前缀均为`apps/api/`）；`apps/api/openapi.snapshot.json`、`packages/python-sdk/api-coverage.toml`及本地生成TS合同。
- Web：`apps/web/src/components/connections/StorageMaintenanceImpact{.tsx,.test.tsx}`、`StorageLocationsConsole.tsx`；必要既有Console回归覆盖集成，复用现有API client、Query与会话隔离。
- 正式文档：`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/api/guides/storage-locations.md`、`docs-site/api/guides/_routes.generated.md`、`docs-site/dev/concepts/storage-connections.md`、`README.md`、`CHANGELOG.md`。
- 计划与晋升引用：本计划、E09、整套执行队列，backlog索引、路线、基线和信息架构。只搬迁本E11文件并更新全部链接，保留原故事与未完成验收；不分配发布版本、不新增环境变量/依赖/schema。

后端使用owner已核实的一次性test模式：测试空位置、跨来源/历史/注销/孤儿、相同项目与样本去重、无任务项目关联、run/job独立、缓存/有效租约、临时租约归属、冻结血缘与缺血缘历史、平台位置拒绝、各角色/Key/前后撤权、零源I/O和固定查询数。`pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_storage_maintenance_impact.py tests/test_storage_source_summary.py tests/test_storage_locations_api.py -q'`；SDK只读新路由按现有Web专用覆盖排除并运行合同4项。

Web定向Vitest、tsc、lint/css tokens；生成OpenAPI/TS与API索引，Ruff/Prettier、文档frontmatter/导航/可读性/构建、diff与正常提交钩子。实际浏览器使用独立会话、无mock，在保留3100/8100查看既有位置；检查默认折叠、查询/刷新、原导航/弹窗Escape与焦点，1440桌面和390窄屏明暗截图、控制台及网络。无Worker代码变更不刷新服务、不重建镜像。测试结束按owner销毁test资源，清理报告/截图/缓存/浏览器会话，保留人工业务数据。

回退只撤销新增GET/UI/合同，不改变业务或物理对象。交付后追加Outcome与实测证据，E11-B2的版本化变更门禁、数据集归档/恢复及E11-C物理回收继续活动。

## 历史调研基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                                     | 已有能力、缺口与复用约束                                                                                      |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| [project_access.py](../../apps/api/app/services/project_access.py)                        | 平台身份和项目职责分离，复用当前权限权威；位置 use/manage、内容按用途授权是新增能力。                         |
| [dataset.py](../../apps/api/app/services/dataset.py)                                      | Dataset/Item 当前硬删，unlink 硬删除项目任务/标注等并返回 soft=false；已有影响计数，尚无绑定预览版本/有效期。 |
| [active_learning_pool.py](../../apps/api/app/services/active_learning_pool.py)            | guard_source 已保护主动学习历史来源；通用保留/回收不得绕过现有引用阻断。                                      |
| [training_storage_deletion.py](../../apps/api/app/db/models/training_storage_deletion.py) | 训练已有持久删除 intent/tombstone 和重试，不是 Dataset 软删除。现有数据集登录读取并不等于已有对象级 ACL。     |

候选修改入口：[deps.py](../../apps/api/app/deps.py)、[project_access.py](../../apps/api/app/services/project_access.py)、[datasets.py](../../apps/api/app/api/v1/datasets.py)、[dataset.py](../../apps/api/app/services/dataset.py)、[storage_connection.py](../../apps/api/app/services/storage_connection.py)、[training_storage_cleanup.py](../../apps/api/app/services/training_storage_cleanup.py)、[dataset.py](../../apps/api/app/db/models/dataset.py)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与当前依据

原地引用把文件生命周期留在外部目录，平台的“删除数据集”“移除样本”“解除项目关联”必须与“删除原文件”区分。多位置、多项目引用同一内容后，也不能按单个数据集删除动作直接回收共享文件。

当前数据集 API 已有角色和 scope 校验，写操作主要限制为 super_admin/project_admin；连接器具有 owner/global 范围，并将密钥加密保存。本 Epic 延用这些基础，补齐对象级使用授权与引用生命周期，不把新的能力模型写成已经存在的事实。[数据集 API](../../apps/api/app/api/v1/datasets.py)、[连接器模型](../../apps/api/app/db/models/storage_connection.py)。

当前删除和解除关联已有业务处理，前端具备影响确认；训练版本已经冻结内容。新治理应复用这些流程，并保护跨数据集、项目和版本引用。[数据集服务](../../apps/api/app/services/dataset.py)、[数据集页面](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)、[训练版本模型](../../apps/api/app/db/models/training_dataset.py)。

## 2. 目标、范围与暂不做

目标是使每次读取、创建来源、关联项目、变更位置和清理文件都有明确授权与影响范围，保证外部源不被平台业务删除动作破坏。

范围包括：

- 存储位置可使用与可管理的权限分离，来源与数据集权限串联。
- 受控目录只读、路径边界和读取时的内容修订核验。
- 已登记、可读原图、可编辑标注之间的明确状态门槛。
- 逻辑移除、归档、解绑、缓存清理、托管对象回收的独立语义。
- 项目、版本、任务和副本的引用保护；审计、保留与恢复。

暂不做：向外部源写回标注、删除外部原图、任意服务器文件管理器、跨所有系统的通用权限平台。仅为完成本轮能力定义必要权限合同。

## 3. 用户故事与切片

| 编号    | 用户故事                                                                         | 切片与完成结果                         |
| ------- | -------------------------------------------------------------------------------- | -------------------------------------- |
| E11-S01 | 作为管理员，我希望允许某人使用一个目录，却不把服务器全部目录和凭据交给他。       | M1：受控根、位置使用授权、配置管理分离 |
| E11-S02 | 作为项目标注人员，我希望读取被分配任务的图像，但不因此获得列举整个源目录的权限。 | M1：任务到内容的最小授权读取           |
| E11-S03 | 作为数据负责人，我希望源文件变化或仅剩缩略图时，不会误把旧标注提交到新内容。     | M1：修订与完整原图门槛                 |
| E11-S04 | 作为数据负责人，我希望移除样本或归档数据集不会删除外部原图，也不会破坏冻结版本。 | M1：逻辑操作与物理删除分离             |
| E11-S05 | 作为管理员，我希望清理无引用的托管缓存，同时保留被训练或项目使用的内容。         | M2：引用核验、保留、回收任务           |
| E11-S06 | 作为管理员，我希望停用位置或撤销授权前看到影响，变更过程可审计和恢复。           | M1 基础禁用与审计；M2 完整预览和恢复   |
| E11-S07 | 作为运维人员，我希望并发导入、解绑、冻结和回收不会互相破坏。                     | M1 核心互斥；M2 全量引用对账           |

## 4. 授权模型草案

以下定义能力，不要求新增同名角色枚举。现有角色、owner/global 连接范围、项目成员关系和对象归属应通过统一 resolver 映射到这些能力。

| 能力                   | 授权对象与约束                                                                |
| ---------------------- | ----------------------------------------------------------------------------- |
| location.use           | 使用指定受控根或前缀建立来源；不包含管理凭据                                  |
| location.manage        | 维护配置、授权、禁用和用途映射；需管理员或明确委派                            |
| dataset.read/manage    | 读取或管理特定数据集；不能仅凭知道 ID 获权                                    |
| source.scan/manage     | 触发已授权来源扫描，或调整本来源规则；仍受位置范围限制                        |
| asset.read/download    | 通过数据集/项目任务/冻结版本授予；按使用目的裁剪                              |
| project.link_dataset   | 同时校验项目管理与数据集可关联权限                                            |
| dataset.freeze         | 校验所选项目的训练版本创建权限、任务/标注读取范围、原图读取及目标托管写入策略 |
| storage.cleanup/retire | 仅限平台自有对象和授权维护范围；执行前核验引用与保留                          |

员工通过项目任务获得所需内容访问，不自动获得数据集全量浏览、源目录列举、原图批量下载或连接管理权限。前端按钮隐藏与后端校验使用同一能力来源。

位置使用授权不能突破管理员配置的根。owner/global 旧连接器的可见性映射必须保留，不能因为升级为 StorageLocation 就变成全平台可用。

跨数据集或不同授权边界的同 hash 内容，不自动合并授权或暴露“另一个项目也有此文件”。摘要去重只优化存储，不能替代访问控制。

## 5. 原地目录与内容保护

### 5.1 读取边界

- 首期由管理员配置服务端或 worker 可读的根，采用只读挂载；用户仅选择根内相对目录。
- 拒绝路径穿越、越界符号链接、非常规文件和未授权挂载；目录列举与实际打开都校验边界。
- 文件在扫描后被替换、根挂载变化或内容指纹改变时，读取层重新核验并返回明确状态，不静默把新字节作为旧内容。
- 位置凭据只在受控执行端使用；浏览器获得短期受限读取凭证或鉴权代理结果，不拿到存储管理凭据。
- reference 不强制保存原图副本，但允许按独立策略创建缩略图、受限读取缓冲、完整缓存和冻结副本；各类文件归属与保留明确。

### 5.2 可编辑标注门槛

“已登记”只表示存在索引。只有当前内容修订的完整原图经核验可读，并满足项目权限时，才能进入可编辑标注。

只有缩略图时允许只读查看，禁止创建/修改/提交标注。若源离线但经核验的完整原图缓存仍可读，可以按同一内容修订允许编辑；不能用低分辨率预览冒充完整原图。

标注提交携带或由服务端绑定内容修订与任务版本。必须阻止旧会话提交到不同修订；如果会话仍绑定旧 Asset、完整验证快照可用、任务版本与权限未变，可继续提交到该旧修订。否则提示变化原因并保留用户未提交草稿，不能自动套到新图片。

### 5.3 撤销权限

撤销后立即拒绝新读取凭证、扫描和修改请求。已签发的直接存储 URL 在有效期内可能继续有效；M1 采用短期 TTL 并明确生效窗口，要求即时失效的场景应走可实时鉴权的代理读取。

日志、任务错误与 UI 不记录明文密钥。已经由用户下载的字节不作为平台可远程收回的对象来承诺。

## 6. 删除、保留与恢复规则

| 操作               | 默认行为                                                            | 保护条件                                                |
| ------------------ | ------------------------------------------------------------------- | ------------------------------------------------------- |
| 从数据集移除样本   | 改变工作数据集成员关系或设为墓碑；不删外部原图                      | 活跃任务影响须处理；冻结版本仍保留固定引用              |
| 归档/删除数据集    | 先归档并停止新操作；按策略保留必要记录                              | 关联项目、版本、训练和未结束任务须有影响预览            |
| 解除项目关联       | 复用现有影响预览；任务/标注处理必须明确确认                         | 不隐式删除数据集原图或冻结版本；审计业务处理结果        |
| 清理预览或按需缓存 | 满足已验证回源/可重建与最小副本要求时，允许淘汰平台自有的可替换缓存 | 无活跃读取租约/保留锁；不可删除唯一受保护原图或冻结内容 |
| 清理托管原图       | 独立回收任务，先确认所有引用和保留条件                              | 不能仅用单个数据集成员数判断无引用                      |
| 禁用位置           | 阻止新接入/按策略停止访问，保留配置与历史                           | 明确现有读取影响；不能当作所有文件缺失                  |
| 退役位置           | 完成引用转移或明确保留不可用历史后退役                              | 有未处理引用或迁移失败时阻断物理清理                    |

清理默认区分“逻辑移除”和“物理回收”两个阶段。具体保留天数、缓存 TTL 和软删除窗口在配置中明确，不把草案日期写成硬编码承诺。

平台可以按策略删除自己拥有且无引用的缓存；外部源只读并不意味着平台缓存永远不能清理。冻结内容必须有独立保护，不能因与缓存位于同一桶就一起清除。

Asset 的业务引用保护的是应保留的内容，不会永久固定每一个可替换缓存副本。具体副本是否可回收由保留锁、有效租约、可验证回源和最小副本要求决定。缩略图是派生产物，不能充当“原图还剩一个副本”的依据。

回收前在事务或等价一致性边界内重新核验引用和操作版本；任务采用幂等键和检查点。冻结/读取/导入期间使用的内容必须有可核验保留标记或租约，防止先检查无引用、后并发新增引用的竞态。

## 7. 数据与 API 草案

| 合同          | 关键内容                                                                          |
| ------------- | --------------------------------------------------------------------------------- |
| 授权解析      | actor、resource、purpose、project_context、allowed_actions、policy_revision       |
| ImpactPreview | 操作对象、配置/数据版本、任务/项目/版本/副本影响、阻断原因、有效期                |
| 生命周期状态  | active、archived、pending_cleanup、deleted/tombstone；与来源 missing/offline 分离 |
| 保留引用      | asset_id、ref_type、ref_id、policy、expires_at/retain_until、active_lease         |
| 审计事件      | 操作者、授权目的、对象、策略版本、影响摘要、任务 ID、结果；秘密脱敏               |

建议维护操作遵循“预览—提交—后台执行—结果核验”，提交必须绑定预览对象版本，防止确认后引用已变化而继续使用旧影响数量。

只读外部位置不暴露删除原文件接口。位置详情、样本 URL、缓存读取、冻结和批量导出均经过统一访问解析，不依赖前端是否展示按钮。

## 8. 验收标准

| 编号     | 可验证标准                                                                                                                                     |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| E11-AC01 | 仅获某受控根使用授权的用户无法列举其他目录、读取越界符号链接或取得位置密钥；直接 API 请求同样被拒绝。                                          |
| E11-AC02 | 项目员工能读取被授权任务所需样本，但不能据此列举全目录、浏览未授权数据集或发起批量下载。                                                       |
| E11-AC03 | 已登记但未确认完整内容的样本不能可编辑标注；只有缩略图时禁用编辑与提交；完整已核验缓存按同一修订可允许读取。                                   |
| E11-AC04 | 原图在标注会话中被替换后，提交不能作用于新内容；旧 Asset 验证快照仍可用且任务/权限未变时可提交旧修订，否则拒绝并保留草稿。                     |
| E11-AC05 | 对 reference 数据集执行样本移除、归档或解除关联，源目录文件数量、内容摘要和修改时间均不被平台改写。                                            |
| E11-AC06 | 唯一受保护原图、冻结内容及被有效租约/保留锁占用的副本不能回收；业务 Asset 仍被引用时，满足已验证回源和最小副本要求的可替换缓存仍可按策略淘汰。 |
| E11-AC07 | 影响预览后新增引用或配置版本变化，原提交被重新校验并阻断/要求更新预览，不能按过期计数执行。                                                    |
| E11-AC08 | 位置权限撤销后新请求立即拒绝；已有短期直链行为符合记录的 TTL，要求即时失效的入口使用实时鉴权路径。                                             |
| E11-AC09 | 同一数据集同时导入、解绑、冻结和清理时，任务幂等且不会生成越权任务、丢失冻结内容或删除新产生的引用。                                           |
| E11-AC10 | 从软删除窗口内恢复数据集后，成员、权限和来源配置符合恢复策略；审计完整，恢复不自动恢复已被撤销的人员权限。                                     |

## 9. 迁移与回滚

1. 列出现有角色、数据集可见性、连接器 owner/global、项目任务读取和删除行为，形成迁移表。
2. 新目录来源先采用显式授权；旧数据记录保持可解释的兼容映射，不扩大可见范围。
3. 对旧数据集从平台可见走向更细权限的变更，先生成影响清单并评审，避免升级时无提示地让合法工作流失效。
4. 对原有对象回填 ownership、用途和引用证据；无法确认归属的对象不进入自动物理回收。
5. M1 只上线安全最小闭环，回收功能后置启用；权限与只读边界不能随 UI 功能回滚而取消。
6. 回收任务可停止，软删除可按窗口恢复；已物理删除的自有内容只有受支持的备份/版本恢复路径，不宣称通用撤销。

## 10. 测试与验证

- 权限矩阵覆盖超管、位置所有者、数据集管理者、项目管理者、员工和失效身份。
- 路径用例覆盖 `..`、符号链接、根替换、Unicode 名称和只读挂载，不允许仅在目录浏览接口校验。
- 内容用例覆盖源离线、只有预览、完整缓存、同名替换和读后修改。
- 删除用例覆盖跨数据集复用、冻结版本、运行中训练、失败迁移和回收重试。
- 并发用例覆盖预览后新增引用、冻结期间回收、解绑期间新导入。
- 验证日志、错误导出、审计和 UI 无明文凭据；只读原图前后做摘要与元数据对比。

## 11. 风险与待决策

| 风险/问题                   | 草案处理                                            |
| --------------------------- | --------------------------------------------------- |
| 历史数据集权限较粗          | 显式兼容映射与影响清单；不可默认扩大共享范围        |
| 解除关联的任务/标注删除策略 | 保留既有预览和确认，评审归档/清理选项；冻结引用不动 |
| 直接签名 URL 的撤销窗口     | 按用途设置短期 TTL；高要求路径走鉴权代理            |
| 引用对账与回收竞态          | 事务/版本核验、租约、幂等回收、失败可恢复           |
| 缓存与冻结副本同处一桶      | 以对象用途和保留引用区分，不能按桶名直接全删        |

## 12. 工程任务与完成定义

- [ ] 完成现有授权/删除行为清单、能力映射与新来源默认策略。
- [ ] 实现受控根校验、读取时校验和秘密脱敏。
- [ ] 将内容修订与可编辑标注门槛接入任务读取和提交。
- [ ] 实现影响预览、提交版本检查与统一审计事件。
- [ ] 明确 reference/托管/缓存/冻结对象归属，回填可证明的历史引用。
- [ ] 实现软删除/归档及受控恢复；物理清理通过独立可停止任务执行。
- [ ] 完成并发引用保护、回收幂等和定期对账。
- [ ] 通过负向权限、内容变化和删除验收后，才开放原地目录生产接入。

完成定义：M1 的授权、只读、内容修订与标注门槛必须随目录接入一起上线；M2 的回收治理逐步启用。任何逻辑业务操作都不能隐式删除外部原图或破坏受保护的冻结内容。

## 本项目推进切片与晋升条件

后端输出角色/owner/global/项目职责/读取和删除迁移表，产品/运维确认逻辑保留窗口、原件责任及签名实际期限。现有默认 3600 秒且对齐 600 秒，不是即时撤权；严格用途固定实际上界或实时鉴权代理。权限变化先预览，不无提示扩大共享。

| 切片  | 独立结果                 | 交付范围                                                                         | 验收 / 回退边界                                        |
| ----- | ------------------------ | -------------------------------------------------------------------------------- | ------------------------------------------------------ |
| E11-A | 随核心链路交付的最小保护 | 资源矩阵先定，实际校验随 E02/E05/E03 上线；源只读、修订提交门槛、基础停用/审计。 | 负向路径/API/撤权/覆盖通过；不为开目录入口而后补权限。 |
| E11-B | 可恢复逻辑治理           | 新增归档/软删除、版本化影响预览、恢复策略，保留解绑既有影响确认。                | 恢复不恢复已撤权人员；旧硬删合同变化有迁移/说明。      |
| E11-C | 受控物理回收             | 仅自有副本，经租约/保留/引用重查和幂等账本，先 dry-run 独立启用。                | 唯一原件、训练冻结、备份恢复点、迁移观察期都受保护。   |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_connections_api.py](../../apps/api/tests/test_storage_connections_api.py)、[test_connector_crypto_guard.py](../../apps/api/tests/test_connector_crypto_guard.py)、[test_dataset_delete_cleanup.py](../../apps/api/tests/test_dataset_delete_cleanup.py)、[test_dataset_link.py](../../apps/api/tests/test_dataset_link.py)、[test_training_storage_deletion.py](../../apps/api/tests/test_training_storage_deletion.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[index.md](../../docs-site/user-guide/datasets/index.md)、[storage-connections.md](../../docs-site/user-guide/datasets/storage-connections.md)、[training-datasets.md](../../docs-site/dev/concepts/training-datasets.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

### E11-C2-B 显式保留策略与声明要求

2026-10-24 在 `a9db710fb` 基线上交付；后端集成提交为 `4079e1314`、`d4812885b`、`16a56705f`，Web、恢复夹具、正式文档及本记录同次集成。完整C2、E11/E09及整套Epic继续活动，未分配发布版本。

- 保留合同：0217新增独立hold历史与有界定位摘要索引。超级管理员/全权Key可将单条断言设为无限期或明确未来期限，登记backup/migration要求及按原锚点显式解除；每次均有原因、五分钟预览、身份/配置/完整组指纹、修订CAS、组互斥及同事务审计。无自动到期、任务状态联动、对象I/O或删除任务；存在hold或非初始策略事实时禁止破坏性降级。
- 整组保护：完整bucket/key相关断言先聚合再分页，任一无限期、最晚有限期限和任一有效要求保护整组，身份不同也保守纳入。单条策略与有效保护分别展示，计数仍为断言，字节不能视作可释放容量。已声明要求不是可核验恢复点、对象锁、活跃对象全覆盖或其他清理器屏障，未知覆盖与execution_permission=false保持。
- Web：现有居中详情内默认折叠按需读取，单条编辑、相关要求有界历史、外来要求跳转原锚点并可返回，解除历史保留。预览居中确认，缩短明确提醒，先聚焦返回修改；会话/锚点隔离、过期/冲突清空确认，成功后刷新，失败旧观测禁止编辑。正文13px、次要提示11px，下拉选项简短，说明置于下方并关联aria-describedby。
- 本地检查：106个不同API/DB用例、58个Web用例、4个SDK合同与11个恢复纯测试通过，共179个不同用例。覆盖全组null/最大期限、跨身份/跨peer要求、完整组分页、时区/过去/确认期间到期、精确锚点/UUID/重放、角色/Key/配置/指纹变化、组并发/锁忙和审计失败回滚。首轮夹具错误尝试删除不可变审计记录，修正为仅并发夹具隔离审计调用并重建一次性test环境后，受影响项通过；旧0216直接降级测试按0217→0216顺序修正后通过，未弱化生产审计或外键约束。
- 真实恢复：合成源销毁后恢复149表76行、九桶20对象；新增两条同定位断言、有限/无限策略、有效备份与已解除迁移历史逐字段保持，完整行指纹继续核对。hold只跟随稳定锚点，不新增物理映射白名单；冻结、中断、外部原件丢失及同名替换拒绝继续通过，队列不自动恢复。随机源与目标均按owner销毁并验证清理。
- 人工验收：本轮自有Dataset通过现有服务入库两张PNG、三条样本，其中两条共享定位，正常逻辑删除后生成三条断言；通过真实API登记12条要求，Web历史10+2无重复。实际设置有限期限后相关记录仍无限期保护，Web新增/显式解除迁移要求后其他要求保持；跨peer导航/返回、并发新增后旧确认409拒绝、重新读取继续保护通过。既有冻结元信息、包SHA、manifest及包内图像/标签摘要与基线一致。
- 浏览器：独立agent-browser会话，无mock；1440×1000与390×900明暗编辑/居中确认截图均已检查。窄屏详情340px，内区可滚动，根及详情无横向溢出；缩短提醒、返回修改初始焦点、Escape仅关闭顶层及焦点回到预览/详情触发按钮通过。控制台9条仅debug/info，JS错误0；178条XHR/Fetch中158条记录完成状态，含唯一预期409冲突；其余20条未记录完成状态，不据此宣称成功。保留接口21条有状态记录中20条200、一条预期409。日期控件CLI fill未赋值及文字等待误用aria-label属于自动化操作问题，改用实际DOM输入事件/元素等待后完成验收。
- 合同与正式文档：OpenAPI、本地TS、594条API索引、SDK覆盖与用户/API/架构/恢复说明、README/DEV/CHANGELOG同步。Ruff、tsc、完整Web lint/CSS tokens、文档frontmatter197/navigation199/严格可读性198及构建通过；保留既有4条lint warning、两处样式建议与构建提示，未运行远程CI。
- 清理与运行：在核实owned命名空间及空闲后应用0217并刷新所属API、两个Worker与beat；3100/8100保持健康，无镜像重建或共享.env改动。验收后精确清理自有两原件、三断言及全部要求历史，保留不可变操作审计；8Dataset/12Item/8Source/12Task/7Annotation、三原件SHA及既有冻结包与基线一致。一次性test DB/Redis/九桶已按owner销毁，专用浏览器及已等价集成子工作树清理；截图/报告/构建缓存随收尾删除，人工环境保留。
- 剩余：可核验恢复点身份/耐久性与实际迁移资格、独占所有权/现场最小副本、全部物理读写屏障及可恢复幂等执行仍需独立定稿。下一片先复核当前原件内容/定位证据及独占性证明的可实施范围，不将本地声明或恢复演练直接升级为生产删除许可；E11/E09不归档。

### E11-C2-A 托管原件保留账本与只读预演

2026-10-24 在 `dd2fdf428` 基线上交付；后端及夹具修正提交为 `db7febc72`、`3bdcf5ca0`、`0af55ca02`、`b56864b29`，Web、恢复工具、正式文档与本记录同次集成提交。E11-C2尚未全部完成，E11/E09与整套Epic继续活动，不分配发布版本。

- 原件证据：0216纯加表，无业务级联外键；Dataset/Item在既有删除锁和同一事务内，于级联前批量捕获已登记managed_original定位及历史身份、内容和可证明的配置证据。回滚同时撤回，重复捕获幂等；未知身份保存unknown，不伪造历史locator或独占所有权，也不阻断原有合法逻辑删除。默认未指定保留策略、无限期保护，有记录时禁止破坏性降级。
- 预演：仅当前超级管理员与全权Key可读，前后复核身份/Key及本地、持久平台配置。单SQL按同一观测汇总完整计数和有界页，重查当前定位、Item/Task及关联、所有状态训练快照和主动学习历史；未知结构不能视为零。保护优先于保留，再到资格待核实，计数单位为断言，重复大小不是可释放容量。始终execution_permission=false、reclaimable_bytes=null，无源/对象I/O、任务或状态写入。
- Web：现有“平台对象占用”中默认折叠，仅展开时查询；独立分页、刷新时间和失败旧证据、撤权隐藏、会话修订隔离。详情使用居中Modal，13px正文/11px次要提示；清楚解释无限期保护、已记录引用和未知所有权/现场副本/备份/迁移条件，不提供删除操作。
- 本地检查：136个不同API/数据库用例、36个Web用例、4个SDK合同和11个恢复纯测试通过，共187个不同用例。API覆盖新增捕获/迁移、纯SELECT预演、事务回滚、幂等、全部训练状态/畸形历史、撤权/配置变化、固定查询数/分页及既有上传/生命周期/外部引用/Asset/任务关联回归。初次失败仅为测试夹具不符合现有约束，修正后原失败项通过，未重复累计；Vitest首次线程参数冲突未执行用例，改用明确min/max后通过。
- 真实恢复：合成源销毁后恢复148表73行、九桶20对象，完整核对内容摘要与数据库受保护列；七类物理owner共19处映射包括新保留账本。仅重定位bucket/storage_identity，原始assertion_digest、历史业务ID及无限期保留不变；未知身份拒绝全部映射。冻结/中断、外部原件缺失和同名替换拒绝继续通过，不自动恢复队列；源和目标均按owner销毁并验证清理。
- 人工环境：核实dev归属和空闲，应用0216并通过所属服务刷新API、两个Worker与beat，3100/8100健康，无镜像重建或共享.env修改。通过真实API上传11个任务自有PNG，待缩略图Worker完成后删除Dataset；11个原件逐项回读SHA/大小保持、11条独立记录无限期保留，接口及Web分页为10+1且无重复。既有业务数据和源文件未参与删除。
- 浏览器：独立会话、无mock，1440×1000和390×900明暗截图已检查；新预演区域与详情无横向溢出，窄屏详情342px宽、内容可滚动。默认零预演请求，展开/刷新/分页共7条GET、无写请求；159条XHR/Fetch无失败，JS/console错误均为0。居中详情、Escape及触发按钮焦点返回通过。自动化scrollintoview曾令旧shell产生横向滚动，窄屏重新加载并仅滚动main后布局正常；未改动无关shell。
- 合同与文档：OpenAPI、本地TS、591条API索引及SDK覆盖同步；用户存储指南、API指南、架构、恢复运行说明、README、DEV、CHANGELOG Unreleased与E09/执行队列更新。Ruff check/format、tsc、完整Web lint/CSS tokens、文档frontmatter197/navigation199/严格可读性198及构建通过，正常根提交钩子与diff检查通过；保留既有4条lint warning、两处样式建议及构建提示，未运行远程CI。
- 清理与保留：正常API已删除自有业务；在验证零Worker活动后，仅按manifest中的精确locator清理11个自有原件、11个缩略图及11条测试断言，保留操作审计。8Dataset/12Item/8Source/12Task/7Annotation、三原件SHA和已有冻结包/元信息与基线一致。一次性test DB/Redis/九桶按owner销毁；专用浏览器、报告/截图/构建缓存及已等价集成的子工作树清理，人工环境保留。
- 剩余：显式保留策略与可核验恢复点/迁移保留、原件独占所有权/最小副本、全部物理读写资格及最终互斥/幂等执行仍待独立定稿与验收。C2-A只补齐已登记原件在业务删除后的证据缺口，不覆盖此前丢失的定位、外部原件、缓存或其他owner的物理清理；预演、C1维护和策略候选均不能提升为物理删除许可。

### E11-C1 逻辑位置访问维护

2026-10-24 在 `40952845b` 基线上交付；后端切片及收尾修正为 `9b13e62ff` 至 `86c2e9cd0`，Web、正式文档、生成合同与本记录同次提交。E11/E09及整套Epic继续活动，本片不提供物理回收、卸载或整块共享存储静止许可。

- 维护合同：新增0215、独立Gate与无TTL未收尾receipt，支持active→draining→stopped及显式resume，每次转换递增维护修订。仅当前超级管理员及对应datasets scope可读/预览/提交，五分钟确认绑定身份/Key、位置配置、状态/代次及B1关系指纹；停止预览与提交均核对零未收尾。Gate不依赖业务Location外键；准入登记、最终发布和控制转换按既定锁序串行化，不提交调用者业务事务。
- 收尾与旧证明：实际线程/Future及文件、对象流、SFTP关闭后才settle；重复取消、逻辑超时、迟到结果、构造/关闭不确定和落库失败均保留正确责任，Timer取消后等待回调实际结束。进程丢失不靠TTL、心跳或Job终态清零，没有强制解除接口。目录浏览/探测/扫描/登记/同步、连接复制导入/已保存连接测试、严格引用/完整缓存，以及质量、预测/采纳、修复、导出和新冻结各自接缝携带/检查代次；旧缺字段按1处理，恢复后旧委托仍拒绝。既有Task新请求使用当前代次；独立已保留冻结副本继续读取。
- Web：既有居中位置详情新增默认折叠的“位置访问维护”，只展开时GET，展示状态/代次、时间和最多50条安全未收尾记录。未收尾时停止禁用，未启用部署控制时全部转换禁用；居中二次确认绑定预览，过期/冲突必须重预览，会话/位置/配置隔离，撤权隐藏证据、网络失败标明旧状态。另恢复数据集状态/类型的共享FilterGroup/FilterSelect，统一字号、焦点与窄屏布局，说明位于下拉框外。
- 本地API/DB：621个不同用例通过，去重合并39项核心/迁移、370项读取/缓存/消费者及来源批次的已通过部分和135项最终来源/同步回归；失败项修正后覆盖，不累计重跑。包含真实事务并发、阻塞线程取消/超时、关闭失败留账、前后撤权、旧代次发布拒绝、迁移往返/破坏性降级拒绝及既有消费者。修正原训练缓存测试遗漏第三个返回值导致快照未关闭，以及容量测试与可选NOWAIT缓存发布竞争混合的问题；保留四快照同时占满容量的实质断言。
- Web/SDK与检查：完整Web套件657文件、6541项通过，其中维护组件24项；SDK路由合同4项通过，合计7166个不同本地用例。OpenAPI、本地TS及590条API索引、209项环境变量参考同步；Ruff check/format、完整Web lint/CSS tokens、tsc、文档frontmatter197/navigation199/严格可读性198和构建通过，正常根提交钩子完成最终检查。保留既有lint、测试通知替身及构建提示，未运行远程CI。
- 实际运行：核实owned dev空闲并应用0215，确认所属旧进程全部退出，通过拥有的服务刷新API、两个Worker与beat；仅本工作树服务启用协议，不改共享.env、不重建镜像。核对该命名空间恰有两个队列消费者、任务注册和新增签名，实际Worker探测/扫描成功。独立临时Location/Source/Task读取真实原件，持有实际打开FD和receipt时drain至2，UI停止禁用、停止预览409、新读取409；真实线程/句柄关闭后记录归零，stop至3，resume至4，原Task新读200且SHA一致。旧扫描核验409，新节点探测后新扫描完成3文件；Source.enabled保持true。
- 浏览器：3100/8100、无mock、独立会话；1440×1000和390×900明暗截图已检查，维护/嵌套确认/未收尾及共享筛选均无横向溢出。取消初始焦点、Escape仅关闭顶层并归还触发按钮，归档筛选实际更新URL与结果；字号13px/提示11px、提示为次要色且在下拉框外。控制台错误0、453条XHR/Fetch无失败请求。已有冻结包下载摘要和冻结元信息保持；该包属于另一逻辑位置，不将共享根的验收冒充同Gate冻结范围资格。
- 正式资料：用户存储指南、存储位置API指南及生成索引、存储连接架构、README、DEV、.env.example、环境参考、SDK覆盖、CHANGELOG Unreleased和E09/整套执行队列同步。部署控制默认false，新代码始终登记；只有同命名空间全部旧API/队列消费者退出并完成实测后才可启用，初始空账本不是部署资格。0215遇到未收尾或非初始维护事实拒绝破坏性降级。
- 清理：自有临时Location/Dataset/Project/Source/Item/Task、两个扫描/终态Job、两条通知及零receipt的临时Gate按精确ID移除，保留不可变操作审计。原8Dataset/12Item/8Source/12Task/7Annotation、active/1状态和三原件SHA均恢复/保持；既有冻结内容未变。1822b025369a33f4:test数据库/Redis/九桶按owner销毁，独立浏览器关闭，报告/截图/构建缓存与已集成子工作树清理；3100/8100、两个Worker和beat继续运行。
- 下一步：先复核E11-C2的平台自有副本、引用/租约、保留窗口与恢复点保护，交付不执行删除的dry-run资格和可解释阻断。物理执行、退役、共享/重叠位置、候选连接与外部客户端的物理静止仍需各自独立门禁；真实跨站点Agent和后端迁移目标未指定，不据本机结果宣称生产资格，不归档整项计划。

### E11-B3 数据集归档与恢复

2026-10-24 在 `d591508b0` 基线上交付，实施、正式文档与本记录同次提交。完整E11继续活动，本片不交付物理回收或全owner停止协议。

- 生命周期与确认：新增0214迁移及独立预览/提交端点，保留样本、来源启用状态、项目关联、Task/标注及冻结内容。五分钟凭证绑定当前身份/Key、Dataset配置/状态/修订和单SQL关系快照；过期、撤权、关系变化、等量替换、重放与竞争均拒绝。审计保留安全差异与时间，不保存令牌或路径。归档状态拒绝既有DELETE，明确恢复后仍使用原硬删除合同。
- 写入屏障：托管导入/建任务、点云分块、来源扫描/登记/规则/生命周期/同步以及Quality/Review均捕获和复核生命周期修订，历史缺字段按1。归档再恢复不会复活旧委托；恢复只改生命周期，不回放撤销权限或配置。Scene结构/标定及各新管理写入受保护；新冻结只检查实际合格且选中的样本，全部选择模式共用。既有Task标注/读图、历史冻结打包/下载、个人阅读视图继续按原权限工作；取消/失败收尾及缩略图/视频元数据派生维护保留。
- UI与SDK：活动/已归档/全部列表筛选进入URL并保留返回链接；详情有归档状态和居中影响确认，管理操作禁用或隐藏，历史仍可浏览。确认绑定对象、修订和会话，冲突需重新预览，到期禁止提交，Escape恢复触发按钮焦点。SDK同步字段、两个显式预览/提交方法和覆盖清单；CLI新增list/archive/restore，非交互或JSON缺--yes时零请求，预览写stderr、最终JSON写stdout，不自动重试变更。
- 本地API/DB：413个不同用例通过，其中新增生命周期/托管/来源/质量132项、既有相关278项及迁移3项；重验重叠项未重复累计。覆盖权限/Key、票据/过期/同数替换、源I/O前后检查、各管理写入、跨归档恢复、分块与并发、冻结选择及读身份保留。迁移真实执行往返验证历史active/1，并验证已归档和已恢复revision3均阻止丢失生命周期事实的降级。发现归档后updated_at在共享ORM会话过期，已显式刷新并重验；旧删除测试替身已改为真实DB失败回滚，物理清理不提前发生。
- Web/SDK与检查：57个不同Web及112个SDK/CLI/契约用例通过，合计582个不同本地用例。移除新测试中不受当前Testing Library类型支持的exact选项后tsc通过；修正归档时来源被误称停用的文案并重验。OpenAPI、TS、587条生成API索引、Ruff/Prettier、完整Web lint/CSS tokens、正常提交钩子、文档frontmatter197/navigation199/严格可读性198与构建通过。仅保留既有lint、样式建议及构建提示；未运行远程CI。
- 真实运行：3100/8100无mock、独立浏览器会话；创建自有临时Dataset/Project/Location/Source，在真实Worker完成扫描/预检/登记，得到3样本、3Task、1标注。网页预览与实数一致，实际归档1→2、恢复2→3均200；归档时原图SHA和标注读取/修改成功，配置/扫描/新训练冻结被拒绝，Source.enabled保持true，既有ready ZIP下载SHA不变。恢复后旧扫描登记返回dataset_lifecycle_changed，新扫描在实际Worker完成，持久快照修订分别为1与3，审计各有归档/恢复记录。
- 视觉与交互：1440×1000与390×900明暗主题截图已检查；桌面确认居中宽600，窄屏宽342，内容与底部按钮可用，页面/弹窗无横向溢出。初始焦点为取消，Escape关闭后焦点还给归档/恢复按钮。归档列表过滤、深链和返回state=archived保持；来源历史可读，管理按钮受限。浏览器JS错误与失败HTTP为0；自动化曾在异步关闭前立即查询、以及等待折叠历史内提示超时，改为状态等待和真实可见区域核对，无应用缺陷。
- 环境与清理：先在核实owned dev数据库应用纯加列0214，8Dataset/12Item/8Source/12Task/7Annotation未变；再经所属服务刷新两个Worker及beat，原端口恢复、任务注册及实际派发通过，无镜像重建。自有临时业务经正常API删除，Source/运行/修订/终态Job/Location按receipt及FK精确清理，审计保留，已有Dataset仍active/1，三原件SHA不变。一次性test数据库、Redis与九桶按owner销毁；浏览器、报告/截图/构建缓存和已集成自有子工作树清理，人工环境保留。
- 后续：全异步owner维护/停止资格、受控物理回收、保留窗口与整项跨Epic矩阵仍待实施；E11/E09及整套实施队列不归档。

### E11-B2 位置配置候选预览与确认

2026-10-24 在 `a937c4446` 基线上交付；实现、合同、用户/API/架构文档与本记录同次提交，整项E11/E09和整套执行目标继续活动。

- API：POST change-preview复用无副作用校验，返回安全字段差异、完整B1盘点与五分钟签名确认；PATCH必须带impact_token。令牌绑定操作者/Key、位置及连接修订、候选最终配置、同一SQL快照内的关系身份/相关状态与计数。保留User→Key→Connection→Location锁序，新增关系、等量替换、候选/配置变化、撤权、到期或重放均拒绝。位置配置CAS和业务owner各自的屏障仍独立，未实现所有后台作业静止或物理退役许可。
- 审计：成功更新记录安全字段差异、前后修订、预览有效/测量时间及提交复核时间和计数；不记录令牌、路径或连接秘密。失败由独立请求审计保留状态，业务事务不提交。普通标注内容编辑、心跳与最近访问不纳入关系指纹，不承诺检测这些无关变化。
- UI：两个既有位置PATCH入口统一使用居中确认，展示候选差异、关系摘要、可展开完整盘点与有效期；确认前不写入，修改候选/位置/会话丢弃旧预览，失效/失败需重预览。到期禁用确认，撤权隐藏旧证据，保存与刷新失败分开表达。浏览器发现保存后本地提示随详情重挂载消失，已改用现有全局成功提示并补回归。
- API/DB：143个不同本地用例通过；新门禁35项、B1盘点26、位置API9、缓存40、Workbench14、S3引用19。一次138项、补充5项，合并结果为143；无重复累计。覆盖零源I/O/预览SELECT-only、全部候选、标准化授权、Key/角色、过期及聚合期间到期、同数来源/Task/Annotation替换、项目/输入/缓存/租约/冻结/预览/扫描新增关系、连接修订、重放和单条成功审计，无关心跳/访问保持可确认。
- Web/SDK：新确认组件14项、两处入口7项、B1/Console21项，共42个不同用例通过；成功提示修正后相关21项重验不重复累计。SDK合同4项通过；本片合计189个不同本地用例。tsc、完整Web lint/CSS tokens通过；正常提交钩子完成最终集成检查。未宣称全量套件或远程CI。
- 真实浏览器：3100/8100独立会话、无mock。既有人工位置只预览/取消，仍为修订1，全部22项关系计数和配置与验收前逐项一致。自有临时位置实际完成使用范围保存一次、缓存TTL保存两次，最终修订4、global、TTL86402，三条PATCH均200；另外六条预览POST200。确认层Escape只关闭最上层，焦点还给原按钮；重新预览、确认保存及全局成功提示通过。1440×1000和390×900明暗截图已检查，展开长盘点在对话框内滚动、固定底部按钮可用，页面及弹窗零横向溢出，JS错误与失败HTTP为0。
- 文档与产物：新增预览operation及PATCH必填令牌合同，OpenAPI、本地TS、SDK覆盖、585条生成API索引同步。用户/接口/架构、README、CHANGELOG Unreleased、本计划/E09/队列同步；文档frontmatter197、导航199、严格可读性198和构建通过，仅保留既有lint/语法高亮/大包提示。
- 清理：临时位置在核对零依赖和已验证dev所有权后定向移除，保留操作审计；专用浏览器关闭。按owner销毁1822b025369a33f4:test数据库/Redis/九桶，报告/截图/构建/缓存和已集成子工作树清理。人工源文件、业务数据、3100/8100、两个Worker及beat保持运行；无schema/依赖/Worker变更、不重建镜像。
- 后续：数据集归档/软删除、恢复时不复活已撤销人员授权、全异步owner维护协议、受控物理回收及跨Epic完整矩阵仍待实施；下一片先复核归档/恢复的现有硬删除与项目解绑合同。E11/E09暂不归档。

### E11-B1 / E09-B3 只读位置盘点

2026-10-24 完成 E11-B1 / E09-B3；实现、正式文档与本记录同次提交。本阶段交付只读依赖盘点，未改变既有配置PATCH或硬删除合同，不宣称完整E11或全owner安全停用完成，未分配发布版本。

- 已交付：超级管理员及datasets:read的GET，前后复核当前身份、Key与位置修订；单SQL汇总全部保留来源、采纳历史与Asset成员、数据集/项目/任务/标注、持久活动轮次和周期配置、预览与完整缓存账本/有效读租约、当前Task可归因输入，以及持久位置血缘可归因冻结版本。固定大小响应不返回路径、秘密、内部snapshot或证明，无写入及外部I/O；平台用途位置明确拒绝。
- 范围解释：输入按持久状态计数，包含到期待清理记录，不冒充当前作业/有效凭证；缺Task关联的历史输入、托管或缺少位置血缘的旧冻结仍未覆盖。位置修订使旧完整缓存失配，预览复用、Task项目授权和独立冻结内容各按原合同判断。响应execution_permission=false，不产生提交许可或无引用证明。
- API/DB：43个不同用例通过，其中新26、既有来源摘要和位置API17。首次42项及补充SFTP1项通过，S3重复核验不重复累计。覆盖45个来源跨页恒定查询、全部只读SQL、零文件/对象I/O、真实新旧采纳修订和注销、三条成员路径及无任务项目关联去重、AsyncJob删除后轮次保留、孤儿缓存及租约期限、预览源与平台目的位置、输入过期/孤儿和当前Task归因、跨项目冻结重复成员与未知/null/非数组历史、角色/Key矩阵、聚合后身份/Key/位置变化拒绝。S3/SFTP空外部位置均可盘点已有关系，不赋予新原地读取能力。
- Web与SDK：新增9个Web用例及既有Console12个通过，覆盖默认零请求、加载/刷新、失败保留时间证据、401/403/404隐藏、会话迟到结果、位置/修订隔离和配置已变化提示。SDK合同4项通过；合计68个不同本地用例，不宣称全量套件或远程CI。首次Web失败来自测试mock漏导出ApiError，修正后9项通过；tsc中的测试Array.at调用改为当前target支持的索引后通过。
- 实际浏览器：保留的3100/8100、独立会话、无mock；位置有8来源（启用7）、8数据集、12引用样本、7项目、12任务、7标注、9条预览记录7926字节，活动轮次/同步与缓存账本为0。展开前无新GET，展开和刷新各一条GET200，无变更POST；统计与真实API一致。已就绪冻结显示0并附部分覆盖说明，未将旧版本解释为无保护依赖。
- 交互与视觉：1440×1000及390×900明暗主题截图已检查。窄屏页面宽度390，居中弹窗342×852，内容在弹窗内滚动无横向溢出；Enter展开、Escape关闭并恢复“查看 人工验收受控目录 详情”焦点通过。页面JS错误和失败HTTP为0，控制台仅Vite debug/React DevTools info。自动化等待曾误命中窄屏隐藏导航drawer，改为当前Modal选择器后确认行为正常，无应用修复。
- 文档与检查：更新用户存储指南、存储位置API与生成路由索引、存储连接架构、README、CHANGELOG Unreleased；E11由backlog晋升根目录，四处旧引用、E09和整套队列同步。OpenAPI仅新增GET，无旧operation移除；TS生成、SDK覆盖、Ruff、Prettier、tsc、完整Web lint/CSS tokens、文档frontmatter197/navigation199/严格可读性198及构建通过。仅有既有4条ESLint warning、两处训练页样式建议和构建语言/大包提示。
- 清理与保留：按owner销毁1822b025369a33f4:test DB/Redis/九桶，退出0；专用浏览器已关闭，任务报告/截图/构建/缓存于记录后清理。dev人工业务数据与原始文件、两个Worker和beat保留，无schema/Worker/依赖变更、不重建镜像。
- 剩余：候选配置与版本化提交门禁、全部异步owner的维护协议、数据集归档/软删除与不恢复已撤销人员授权的恢复策略、受控物理回收和跨Epic完整矩阵仍须继续实施。E11/E09活动计划不归档。

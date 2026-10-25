# EPIC-09：存储控制台与页面职责去重

> 状态：E09-A、E09-B1/B2/B3/B4/B5/B6/B7 complete locally；B7已交付头像新写入位置受控切换；B8报告附件已集成，B9审计归档已完成隔离实现与本片验收、待主树集成，另外六用途仍锁定；B6已交付媒体重试共享任务详情；B5补齐九用途读写策略与真实位置修订；B4与E12-C2交付当前已采纳源索引规模，E12-C3交付平台当前对象六分类；位置影响、配置确认、逻辑维护与原件保留／核验沿既有 E11 交付。其余用途变更、实际迁移／物理退役尚未完成，完整 E09 继续活动，不分配发布版本。
> 转入日期：2026-10-22；A 切片验收：2026-10-23；初始调研基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。

## 当前集成状态

2026-10-25，B8及null槽位清理修复已合入主树；人工环境已应用0246，原3100／8100、两Worker及绑定Beat按原常驻服务刷新，原166表9,230行与24个对象／删除标记保持。C6万条辅助测量已停止，不再作为合入前置。下文隔离实施期间的源码冻结与待集成说明仅为历史记录，完整E09仍按原始范围推进。

## 本轮定稿：E02-B16／E09-B9 审计归档新写入位置切换

2026-10-25，基线 `a04678026`，继续原始E02-S04／AC05、AC10及E09-S05。下一项交付独立的audit-archive用途：管理员可沿现有用途映射选择已登记的全局S3目标，真实验证后预览并确认，只影响后续归档分配。复用AuditArchiveOutput及现有分区归档链，不改审计业务、不展开标注或训练；原3100／8100及人工数据保持，不重建镜像。annotations同时承载评论、指南、RLE和独立任务原件，不能仅开放其中一个入口就将整个用途标为可切换。

- 旧schema1和初始代次按原规则读取；新schema2保存实际位置、代次、endpoint／bucket／完整受控key，公开年月和逻辑归档键不变。切换前pending及ready继续保存位置，新分配才使用新目标。未知旧对象不按当前桶接管，也不新增历史修复门槛。
- 复用现有封闭S3目标／探针和五分钟预览确认合同；限定legacy或已登记全局S3，凭据与白名单沿连接。确认按原连接→位置→用途顺序检查当前修订、维护状态、操作者与原Key；变化时要求重预览。另六用途保持锁定。归档目标使用独立受控前缀，不允许请求指定任意桶或key。
- 历史归档从保存位置获取当前连接凭据；正常轮换不因旧配置修订而失效，端点／桶／前缀改向仍拒绝。所有状态的归档记录保护位置不被重定向或删除。保留HEAD／IfMatch／完整SHA与解压语义校验，不增加provider版本历史读取合同。
- 既有压缩生成、源分区核验、DROP／ready／Job同事务及真实I/O收尾原样复用；仅把保存目标交给既有物理读写。归档永久保留，不实现物理删除、清理策略或新归档状态机。现有九桶恢复对新定位在任何映射变更前明确拒绝，不将外部目标假装已备份；分类与未覆盖摘要按实际地址解释。
- 同轮修复一个直接相关回归：Creation目前仍把报告附件绑定作为原件配置依赖，切换无关用途会误拒绝在途原件。仅收窄到原件实际消费的datasets／annotations／import配置，保留旧快照完整摘要校验与真实目标变化拒绝；不扩大消费者资格。

实施涉及API的归档模型及0246后迁移、audit_archive_storage／target／binding_change、现有分区服务与认证读取、managed_s3_target、dataset_assets、storage_location及DTO／路由、分类、恢复映射及相关测试；Web沿现有storageApi、StorageBindingChangeButton、StorageLocationsConsole和测试增添第三用途；同步API、用户存储指南、架构／桶口径、恢复手册、README／CHANGELOG、本计划、E02及执行队列。复用现有owner和控件，不增加服务、依赖、环境变量或后台任务。超过8文件来自一个用途在分配、保存、读写和管理面的完整合同，不按每个调用者拆开额外项目。

验证只针对本片：真实隔离PostgreSQL／S3与所属maintenance Worker核对旧ready A、在途pending A、新归档B及凭据轮换／坏凭据拒绝，确认内容与源分区发布合同保持；补迁移、并发预览／分配、位置保护和恢复拒绝回归。复用既有分区安全与各消费者历史验收，不重跑整套资格、规模测试或模型训练。新用途控件做明暗／桌面／窄屏浏览器交互、Escape／焦点、溢出与请求检查。正常钩子与diff检查通过后合入，刷新人工0247与所属进程，按owner清理一次性资源；回退保留新事实与旧位置，通过同一受控流程切后续分配，不删除归档或强行降级。

## E09-B9 Outcome：审计归档新写入位置切换

2026-10-25，基于 `d519cb1b5` 在独立工作树完成本片，实现及正式 API／用户／架构／恢复文档与生成契约一并提交。0247保留旧schema1，增加固定实际位置的schema2和位置索引；三个审计归档绑定端点与居中确认 UI 沿既有封闭目标／真实探针。旧ready和切换前pending保留原位置，新分配才切换；当前凭据轮换不改历史定位，所有归档保护位置，另外六用途仍锁定。归档生成、源内容／OID复核和DROP／ready／Job事务原样复用，无新删除或版本历史合同。

本片273项不同检查通过：216项后端（含6项准确地址分类）、52项Web、4项SDK契约、1项恢复拒绝。最初执行发现并修复列表错误依赖当前端点与legacy路径额外提交两个兼容窗口；相关失败及新增场景已通过。永久归档夹具在所属数据库重置后验证，新增历史断言按基线差额，不要求生产空库。类型检查、CSS语义token、Ruff、正式文档构建及正常提交钩子通过；不重复既有全消费者资格或规模测量。

真实隔离PostgreSQL／S3／maintenance Worker完成旧ready A、切换时pending A、新归档B：A两份／B一份均完整读回，三个源月分区只在既有发布事务成功后移除。目标使用两个仅限所属桶的实际账号；轮换到第二个并撤销第一个后，三份归档继续读取，保存归属与SHA保持。坏凭据更新400拒绝且原修订保留，篡改和重放确认409拒绝，归档引用位置／连接删除拒绝；探针零遗留、活动访问记录为零。浏览器明暗1440×1000及390×844截图均检查，完成真实预览和确认切回后续A（代次3），旧B仍可读；Escape／焦点恢复通过，无横向溢出、控制台错误或失败请求。

所属test／e2e数据库、Redis、十八个桶与两个临时S3账号、浏览器和原始测试／截图产物按owner清理，保留有界结果。原3100／8100及人工数据未动，主树集成与0247应用由协调任务接续。另有父任务窄修 `c8924d4a0`：Creation只依赖datasets／annotations／import的实际配置，73项相关检查独立通过，不计入上述273项。本片未新增审计归档增量账目生产者，当前统计仍partial；外部目标不在九桶恢复范围，工具在任何映射修改前拒绝schema2，不宣称外部恢复已覆盖。完整E02／E09继续活动。

> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 属性     | 草案约定                                                           |
| -------- | ------------------------------------------------------------------ |
| 优先级   | P0：信息归属、位置入口、正确统计口径；P1：迁移与运维工作流         |
| 负责角色 | 产品/设计、前端、后端、运维、测试                                  |
| 依赖     | EPIC-02 的位置与用途合同；EPIC-11 的能力授权；EPIC-12 的统计与健康 |
| 协同     | EPIC-08 数据集工作台；EPIC-07 迁移任务；EPIC-04 既有任务扩展       |
| 交付阶段 | M1 完成职责去重和最小位置控制台；M2 丰富容量、迁移和维护           |

## 本次晋升复核与实施合同

### E02-B15／E09-B8：报告附件新写入位置切换

2026-10-25，基于干净`1e49033c1`，沿整套Epic实施／提交授权定稿，状态approved。对应原始E09-S05、E02-S04／AC05／AC10。九用途中头像已开放受控切换，报告附件已有独立pending／ready／retired及严格逻辑键读取，但分配／解析与数据库约束仍固定legacy位置／代次1；本片补齐bug-reports，另外七用途继续锁定。实际迁移、退役和完整E09收口仍为原有独立范围。

采用与头像同一控制台入口、候选分页、五分钟预览确认及权限合同；仅接受legacy或已登记全局S3位置。新增`/storage-locations/bindings/bug-reports/candidates`、`/change-preview`及绑定PATCH，凭证使用独立audience，绑定当前用途代次、目标／连接／维护修订、原管理员／Key及实际影响。真实受控条件PUT、完整GET／SHA与准确DELETE在SQL锁外、已有StorageOperation内执行，未知收尾不允许切换。身份／Key后按序连接、Location，再锁Binding；新分配共享锁与切换排他锁串行，锁后复核捕获事实。

保留公开逻辑键`bug-report-attachments/{owner_uuid}/{upload_uuid}.{ext}`。旧schema1完整解释不变；新schema2保存正代次、准确Location／endpoint／bucket／受控前缀完整物理键／storage_identity，取当前位置当前凭据，但不因正常密钥轮换否认历史定位。保存真实provider VersionId并用于后续HEAD／GET／DELETE；未知写入维持pending责任，不猜版本。独立pending在PUT前提交，其分配时owner固定：切换前已分配上传继续A，切换后新分配使用B，现有报告字段、绑定权限、期限、退休与pending不自动清理合同保持。

`read()`与首次报告绑定已先经`_for_key()`拒绝缺独立owner的键，退休／清理只处理已知上传；没有按当前绑定猜读旧附件的路径。因此不照搬头像历史缺口阻断：未知历史保持原拒绝，不能影响can_apply，不新增全库扫描或历史修复前置。所有状态的已保存附件加入位置引用保护；用途公共校验仅放开准确bug-reports新代次，避免阻断无关上传。分类仍按endpoint＋bucket＋完整key归属，现有平台A盘点不能包含外部B。九桶恢复在任何映射写入前拒绝未覆盖的新绑定、schema2或实际VersionId，不建设跨端点备份系统。

目标实现从头像模块抽取封闭`avatars | bug-reports`参数的窄S3目标／探针共用层，保留头像公开函数与错误类型适配；不另建配置或迁移框架。前端抽用现有居中Modal及分页／确认交互，保留头像包装及独立响应归属、失效确认和稳定焦点。跨API、DB、S3、Web与恢复工具，超过八文件，准确范围如下：

| 范围         | 文件                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 目标／用途   | 新`apps/api/app/services/managed_s3_target.py`、`bug_attachment_storage_target.py`、`bug_attachment_binding_change.py`；现有`avatar_storage_target.py`、`bug_attachment_storage.py`、`bug_attachment_uploads.py`                                                                                                                                                                                  |
| 数据／接线   | `apps/api/app/db/models/bug_attachment_upload.py`；新`apps/api/alembic/versions/0245_bug_attachment_location_bindings.py`；`services/dataset_assets.py`、`storage_location.py`、`storage_usage_categories.py`，`schemas/storage_location.py`、`api/v1/storage_locations.py`                                                                                                                       |
| Web          | 新`apps/web/src/components/connections/StorageBindingChangeButton.tsx`及测试；现有`AvatarBindingChangeButton.tsx`及测试、`StorageLocationsConsole.tsx`及测试、`apps/web/src/api/storage.ts`                                                                                                                                                                                                       |
| 恢复／合同   | `scripts/worktree_recovery_mapping.py`、`scripts/test_worktree_recovery.py`、`apps/api/openapi.snapshot.json`、本地生成Web类型、`packages/python-sdk/api-coverage.toml`                                                                                                                                                                                                                           |
| 后端定向测试 | 新`apps/api/tests/test_bug_attachment_binding_change.py`、`test_bug_attachment_location_records.py`、`test_migration_0245_bug_attachment_location_bindings.py`；现有`test_bug_attachment_storage.py`、`test_bug_attachment_uploads.py`、`test_avatar_storage_target.py`、`test_avatar_binding_change.py`、`test_storage_usage_categories.py`，`test_storage_locations_api.py`中的新增位置保护断言 |
| 文档         | README、CHANGELOG Unreleased、`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/api/guides/bug-reports.md`及`storage-locations.md`、`docs-site/dev/concepts/storage-connections.md`、`docs-site/dev/reference/storage-buckets.md`、`docs-site/ops/deploy/storage-recovery.md`、E02／本计划／执行队列                                                                             |

```text
Storage UI -> candidate / probe / versioned confirmation -> current Binding
JWT upload -> pending saved owner A or B -> exact S3 write -> ready receipt
report logical key -> saved owner -> current credentials -> exact saved object/version
retirement / cleanup --------------------^              -> precise deletion
```

0245增加schema2／可空实际版本及pending一次性回执约束，保留全部schema1事实；降级先锁上传表及Binding，存在schema2、实际VersionId或非1代绑定即拒绝，只有旧事实时恢复旧约束。功能回退采用同一受控流程将后续新分配切回A，保留schema与B历史读取，不改写旧owner或承诺旧应用读取新事实。

独立修改代理在当前本地HEAD创建的干净隔离树内实现、验证和提交；根C5／C6被测源码及六份C5工具保持冻结，两个测量终结前不集成应用改动。纯检查用该树本地API虚拟环境；DB用该树已核验可丢弃test，实际S3／浏览器用独立e2e，不复用根test／e2e。运行新增附件／迁移及受影响头像／分类测试、恢复纯测试、Web相关Vitest、tsc／lint／CSS tokens、OpenAPI与SDK合同、文档检查及构建。真实A→B核对旧ready／在途pending留A、新上传B、原逻辑键、下载和准确清理；另验证修订竞争、正常轮换、只读／坏凭据探针拒绝、位置保护、七用途锁定、未知历史不阻断及恢复拒绝零部分修改。仅新增控件做明暗／桌面／窄屏、Escape／焦点和请求／控制台核验，不重跑整套消费者或历史恢复资格。

分层命令在隔离树执行（新增文件随实现提供）：

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_bug_attachment_*.py tests/test_migration_0245_bug_attachment_location_bindings.py tests/test_avatar_storage_target.py tests/test_avatar_binding_change.py tests/test_storage_usage_categories.py tests/test_storage_locations_api.py'
PYTHONDONTWRITEBYTECODE=1 apps/api/.venv/bin/python -m unittest discover -s scripts -p test_worktree_recovery.py
pnpm --filter @anno/web exec vitest run src/components/connections/StorageBindingChangeButton.test.tsx src/components/connections/AvatarBindingChangeButton.test.tsx src/components/connections/StorageLocationsConsole.test.tsx
pnpm --filter @anno/web exec tsc -b --noEmit
pnpm --filter @anno/web lint
pnpm docs:build
```

集成后按现有owner刷新受影响人工API／Worker并应用迁移，保留3100／8100、两Worker／Beat及既有业务／原件；独立清理该树测试资源和中间产物，验证提交及patch等价后清理代理树。不重建镜像、改变依赖或分配发布版本；不以本片作为E01、E12或其他原始Epic验收的新前置。

### E02-B14／E09-B7：头像用途的受控位置切换

对应原始 E02-S04、AC05／AC10 与 E09-S05。实现真正的已登记位置A→B：仅后续头像分配采用新绑定；既有pending／ready／retired上传按保存的位置读取、发布和清理，不把同端点换桶当成全部交付。先开放avatars，其余八用途保持原有锁定合同，不追加Tracker、会话、审核或其他消费者改造。

目标复用legacy托管位置或已有全局S3连接位置；不另造配置注册中心。已有来源的list/read探针不构成托管资格。预览在SQL业务锁外，经既有StorageOperation登记真实条件PUT、完整GET／SHA核验与准确DELETE，探针和最终头像均位于受控命名空间；失败不切换、未知收尾保留原责任。确认凭证绑定当前用途代次、目标位置／连接修订、维护代次、当前管理员及影响事实，五分钟内锁内复核并审计；位置／连接锁先于用途锁，新分配与切换串行。仅超级管理员和既有datasets:write凭据可变更。

头像owner扩展保存准确endpoint、bucket、完整前缀key、location与代次，旧schema1保持原解释；新schema2允许真实B并复用其加密连接凭据、白名单和受控client关闭。凭据按当前位置取，物理定位按保存引用取，正常密钥轮换不因配置修订递增而使历史头像失效。provider返回版本身份时保存实际回执，后续读删固定该版本；不推断未知写入结果。增量迁移保留旧owner，含新代事实时拒绝破坏性降级。把尚存AvatarUpload纳入位置引用保护，防止切走后历史位置被重定向／删除。历史用户上传token缺少owner时按原AC10阻止切换并给出修复提示，不猜测接管；已分配pending上传无需排空。

公共绑定校验只放开头像的准确受控合同；原件创建配置比较排除无关头像绑定变化，避免新功能打断无关上传。原平台A盘点保留，其范围不得冒充包含外部B的全平台总量；同桶同键分类必须同时匹配端点。现有本地九桶恢复工具继续明确拒绝未覆盖的外部B，不把其对象重写为A，也不为本片另建跨端点备份系统。

存储“用途映射”头像行提供居中预览／确认，候选位置分页、独立label/hint、旧预览失效、认证修订隔离与焦点恢复沿用既有组件。验证原始A/B新旧对象读取／准确清理、并发旧确认拒绝、只读／错误凭据探针失败、历史缺口、正常轮换、其余用途锁定与无关上传不受影响；API／真实对象存储及浏览器明暗／窄屏完成后同步文档、运行环境和提交。人工3100／8100及原业务／原件保持，测试环境按owner清理；不重建镜像，不分配发布版本。

### E09-B6：媒体失败重试接入共享任务详情

对应原始 E09-S06 与 §11 的“保留媒体资产失败/重试功能并接入统一任务详情”。复用现有 `VideoDerivativeOutput` 执行归属和共享 `AsyncJob`／居中详情；不增加媒体算法、取消协议、Tracker、会话或审核改造。保留 chunk／frame 等待元数据时原有 `video_metadata_preparing` 响应。

重试准备与任务登记同事务提交。任务按发起人、输出和请求资产类型复用，保持本人／超级管理员可见，不随意绑定 Item 的某个项目，不返回另一人的私有任务。任务只保存安全 ID 与资产类型，不复制原件定位、凭据或产物清单；不把一个 Celery ID 写给多个任务。沿输出既有认领、发布、失败和过期收尾事务同步状态；元数据 ready 后仍按本次 probe／poster／frame_timetable 的实际结果判定，不以 Celery SUCCESS 代替业务成功。派发异常表示投递未确认，保留可重派的 pending 任务及明确提示，不伪造终态。

前端重试成功打开共享详情，关闭后刷新失败列表；同一任务也可从后台任务和通知进入。复用既有认证修订守卫、轮询、Escape 和焦点恢复。验证包含本人复用／跨人隔离、实际成功与部分失败／生成失败／租约过期、派发未知、原元数据准备分支，以及实际 API／Worker／浏览器明暗和窄屏。仅验证新增状态桥接，不重跑已通过的全部视频消费者验收；人工环境及原数据保持，测试资源按 owner 清理。

### E09-B5：用途映射的读写策略与配置修订

2026-10-25 基于 `ae0dd5c3f` 对照原始 E09-S05／AC06 与§4.4。现有九用途行、桶、绑定代次和配置匹配状态已交付；缺少读写策略，位置配置修订也未与绑定代次区分。本片补齐这项展示合同，不修改对象读写、用途绑定或迁移授权。

沿现有 `GET /storage-locations/bindings`、`storage_location.binding_rows` 和 `StorageBindingOut` 增加固定策略枚举与真实位置修订：读取由各业务保存的位置记录及既有兼容路径负责；新写入使用当前部署映射；用途切换尚未开放。`configured`仅表示与兼容部署配置匹配，不证明位置健康、历史归属覆盖或迁移完成；来源适配器的`write=false`不代表平台托管存储只读。以同次只读查询联取位置修订，不创建快照、逐项探测、初始化绑定或新增注册中心。

现有映射表增加紧凑的读写策略，绑定代次与位置修订分列说明，统一正文／元信息字号。缺少新字段显示未提供，不从当前角色或位置读取能力推断写策略；权限、九用途缺失／失配、共桶逐行、位置链接及返回上下文保持。复用当前设计系统和浏览器技能验收明暗／桌面／窄屏，允许表内横向滚动，不产生页面溢出。

准确范围为API服务／schema及现有位置API测试、现有Console及测试、OpenAPI／Web生成类型、README／CHANGELOG、存储用户／API／开发参考、本计划及执行队列；超过八文件，以共同只读合同交付。使用本工作树可丢弃test库验证只读、全部九用途、缺失／失配、真实修订与授权；Web验证已知／未知策略、角色隔离和链接。执行适用SDK OpenAPI合同、tsc／lint／CSS tokens、格式／文档检查与构建，保留3100／8100、两Worker／Beat及业务／字节指纹，清理所属测试资源。无迁移、Worker、依赖、环境变量、镜像或发布版本变化。

原始受控用途变更仍为独立待办：当前全九用途共同校验legacy位置／代次1，已有owner仍含旧配置约束，引用位置的读能力也不构成新托管写目标资格。不能只改一条DB绑定或下拉框；也不据此增加Tracker、会话或审核专项前置。媒体失败重试接入共享任务详情另按原始§11推进，本片不改视频执行链。

2026-10-25 随 [E12-C3](1792661016_dataset-storage-operations-recovery.md#e12-c3平台当前对象分项计量) 完成原始 E09-S04 的平台分项展示。沿现有盘点与授权 owner，展示六类实际字节、桶覆盖、采样窗口和未归类／冲突；外部源索引规模独立。旧分类未知、失败保留、刷新不列举，以及明暗／桌面／窄屏已核验。详细后端、真实任务和人工环境证据见 C3 Outcome；不增加消费者、Tracker 或审核改造前置，物理容量和生产目标仍按原始要求记录。

2026-10-25 基于 `7fe707bb6` 定稿 E09-B4，与 [E12-C2](1792661016_dataset-storage-operations-recovery.md#本轮定稿e12-c2--e09-b4-当前已采纳源索引规模) 同次补齐原始 E09-S04／AC04、AC05 的当前已采纳源索引规模，现已实现并完成本地验收。现有平台桶盘点不包含该量；位置内全部可见来源的固定采纳事实在单 SQL statement 内去重，区分已知部分、未知总量、历史观测与平台对象字节。真实双 Dataset 的 4 条采纳记录去重为 2 文件／226 B，冲突及空范围、分页、刷新和明暗／窄屏均通过，详见 [C2 Outcome](1792661016_dataset-storage-operations-recovery.md#e12-c2--e09-b4当前已采纳源索引规模)。不附加 Tracker、审核或生产站点前置，完整 E09 尚未收口。

2026-10-24 基于 `802e6e4df` 定稿 E09-B3，与已晋升的 [E11-B1](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-b1--e09-b3-位置维护影响盘点) 同次交付超级管理员的只读位置维护盘点。准确文件范围、单SQL/当前授权/历史覆盖合同与验证矩阵以E11本轮合同为准。现有居中详情内默认折叠，仅展开/刷新GET，无额外写操作；位置配置变更影响与全部owner安全执行许可分开。归档/恢复、版本化变更门禁和物理回收仍待后续实施。

用户已授权“完成 E10-A，再推进 E08/E09 页面整合”。复核基线 `74bd04c12`，工作树干净；E03/E04 已完成，E10-A 已支持引用原图严格读取后冻结独立副本。本节替代历史候选范围，仅实施 E08-A/E09-A 页面整合；保存视图、质量治理、异步容量统计和维护迁移仍属后续切片。超过 8 个文件，跨前端、API、合同和文档；不创建业务表、不改依赖/环境、不重建镜像。

本轮E11-B2补齐位置配置候选预览与版本绑定确认，范围与验证以[E11实施合同](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-b2-位置配置候选预览与确认)为准；使用范围及缓存保存入口共用居中确认，不将只读盘点误作全部owner静止或物理回收许可。

E11-C1在同一位置详情内补充默认折叠的访问维护，范围以[E11实施合同](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-c1-位置访问维护准入与排空)为准；关闭新访问、未收尾记录、停止与恢复各自明确，居中版本化确认。只表达本逻辑Location的受控访问，不能作为共享挂载/桶、候选连接或外部客户端的物理静止证明。

E11-C2-A在平台对象占用卡片内补充默认折叠的托管原件保留与回收预演，范围以[E11实施合同](1792661016_dataset-permissions-lifecycle.md#本轮定稿e11-c2-a-托管原件保留账本与回收预演)为准；只查看业务删除时保存的已登记原件断言和当前保护关系，不按位置统计或推算可释放字节。分页/居中详情、观测时间、无限期保护及未覆盖条件清楚表达，无物理删除操作。

E11-C2-B进一步在原件居中详情内提供默认折叠的保留设置与要求：单条期限与整组保护分开，已声明备份/迁移要求经预览登记或显式解除，相关历史分页和原锚点导航保留。范围、验证及未知覆盖以[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-b-显式保留策略与声明要求)为准；不提供物理删除许可，也不把声明编号解释为真实恢复验证。

## 准确文件范围

- E08 页面：`apps/web/src/pages/Datasets/{DatasetsPage,DatasetDetailPage}.tsx`、`datasetsUrlState.ts` 及同名测试；`apps/web/src/App.tsx`、`apps/web/src/api/{datasets,tasks}.ts`、`api/datasets.test.ts`、`hooks/useDatasets.ts`、`hooks/useFilterListCancellation.test.tsx`；`pages/Projects/ProjectLearningPage.tsx` 及测试、`pages/Training/trainingDatasetDraftStorage.ts` 及测试。独立详情保留上传、扫描、元数据、Scene、坐标系、关联/解绑，接入真实分页、来源/可用性、检查器、分项目进度和共用训练入口。
- E09 页面：`apps/web/src/pages/Storage/StoragePage.tsx` 及测试；`components/connections/{FilesystemLocationsPanel,StorageConnectionsPanel,StorageLocationSourceSummary,StorageLocationsConsole}.tsx`、适用测试和 `storageQueryState.ts`；`components/datasets/{ImportDatasetWizard,DirectoryReferencePanel}.tsx` 及测试、`DirectoryScanHistory.test.tsx`、`storageNavigation.ts` 及测试；`api/{datasetSources,storage,storageConnections}.ts`、`hooks/{useStorage,useStorageConnections}.ts` 及 `useStorageConnections.test.tsx`。统一位置配置、九用途和安全返回，保留 E04 来源/任务分页与视频维护。
- 集成修复：`apps/web/src/hooks/useVerifiedImage.ts` 及测试、`components/datasets/DirectoryRegistrationProgress.tsx` 及测试、`DirectorySyncSchedule.tsx`，连同上述 `DirectoryReferencePanel.tsx`。预览受控排队及容量繁忙有限重试；预检、生命周期与周期变化同步刷新工作台和单项检查器。
- 后端：`apps/api/app/api/v1/{datasets,tasks/list}.py`、`schemas/{dataset,storage_location,training_dataset}.py`、`services/{dataset,dataset_workbench,storage_location,storage_source_summary,training_datasets}.py`、新增 `apps/api/tests/test_dataset_workbench.py`；`apps/api/openapi.snapshot.json`、`packages/python-sdk/api-coverage.toml` 及生成的本地前端合同。沿用已有探测历史 API，不新增表或迁移。
- 文档：`docs-site/user-guide/datasets/{index,import-images,storage-connections}.md`、`docs-site/api/guides/{datasets,storage-locations,training-datasets,_routes.generated}.md`、`docs-site/dev/reference/storage-buckets.md`、`README.md`、`CHANGELOG.md`；E08/E09/E10 计划、backlog 索引/路线/基线/信息架构。交付状态、验证及剩余切片记入 Outcome。

### 数据流、权限和兼容

```text
/storage?location=ID <--- safe return ---> /datasets?location_id=ID&page=N
        |                                           |
existing config + nine bindings              /datasets/ID?tab=...&item=...
        |                                           |
source/job summary <------------------- source + sanitized item status
                                                    |
                                    authorized project /learning?tab=data
                                                    |
                                          existing E10 freeze composer
```

不新增 migration；仅扩展查询合同。可用性带观测时间，未实时核验不得称当前可读；预览与原图分别判断，缩略图不构成可标注证明。筛选按持久观测，页面只查询当前页。项目进度仅统计该数据集在该项目的任务，列表按既有项目权限裁剪；关联版本沿用训练模块。位置过滤沿用来源授权，按唯一 dataset ID 计数。新读取与异步回调绑定认证及资源 owner，撤权/切会话不展示迟到结果。

旧连接页签转唯一存储配置；向导仅选择已有连接，返回仅接受站内白名单路径，草稿按会话/数据集隔离且不持久保存秘密或 File。存储不再重复数据集目录，不调用逐桶全扫描；没有可靠测量合同时容量、配额、源规模和对象字节显示未提供/待统计。九用途只读，不伪装迁移。

### 验收门与回退

- 后端：worktree wrapper disposable test DB 下运行新增 workbench、storage source summary、storage locations、既有数据集/项目访问测试；覆盖分页、双项目、撤权、异常、唯一计数。同步 OpenAPI、TS、SDK route coverage 并检查合同。
- 前端：指定文件 `pnpm --filter @anno/web exec vitest run ...`，typecheck、受影响 ESLint、CSS token lint；覆盖 URL、会话、失败重试、分页/过滤、解绑预览失败阻断、九用途及导入返回。
- 浏览器：3100/8100 既有人工作环境，验证旧链接、独立详情、来源↔位置、分页网格/检查器、项目进度、引用冻结入口、九用途及深浅主题；检查网络/控制台。测试后清理临时文件和 disposable DB，保留人工环境。
- 回退提交只撤销 UI/查询扩展，保留来源、Asset、冻结版本和存储身份，不将 reference 键交旧 S3 拼接。最终检查 diff 并追加 Outcome。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                                                | 已有能力、缺口与复用约束                                                                   |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [StoragePage.tsx](../../apps/web/src/pages/Storage/StoragePage.tsx)                                  | 已有桶/重复数据集概览与视频失败重试；删除重复表时保留媒体维护入口及管理权限。              |
| [StorageConnectionsPanel.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.tsx) | 复用现有表单和 hooks，迁到位置详情；导入向导中的内嵌连接创建也需同步迁移。                 |
| [storage.py](../../apps/api/app/api/v1/storage.py)                                                   | 现有统计六类、health 只 HEAD annotations；新位置健康/九用途合同由 E02/E12 提供，E09 消费。 |

候选修改入口：[StoragePage.tsx](../../apps/web/src/pages/Storage/StoragePage.tsx)、[StorageConnectionsPanel.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.tsx)、[ImportDatasetWizard.tsx](../../apps/web/src/components/datasets/ImportDatasetWizard.tsx)、[DatasetsPage.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)、[storage.ts](../../apps/web/src/api/storage.ts)、[App.tsx](../../apps/web/src/App.tsx)、[storage.py](../../apps/api/app/api/v1/storage.py)、[storage.py](../../apps/api/app/schemas/storage.py)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与当前依据

当前存储页同时展示后端、桶、数据集数量、数据集存储概览和项目关联；数据集页又管理完整数据集与连接器，两页重复的信息和操作分散了用户注意力。[存储页](../../apps/web/src/pages/Storage/StoragePage.tsx)、[数据集页](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)。

存储页硬编码 MinIO 和 HTTP 开发环境；“总容量”使用 `total_size_bytes`，实际上来自对象大小汇总。存储服务包含九类业务用途，而存储 API 当前仅覆盖部分桶。后续 UI 应使用位置能力与服务端统计合同，而非写死产品名称和单一后端。[存储服务](../../apps/api/app/services/storage.py)、[存储 API](../../apps/api/app/api/v1/storage.py)。

既有 JobsBell 与任务详情已有统一任务入口，本 Epic 仅接入其上下文展示，不另造任务中心。[JobsBell](../../apps/web/src/components/shell/JobsBell.tsx)。

## 2. 目标、范围与暂不做

目标是让存储页成为位置、连接和平台占用的权威入口，并通过深链连接数据集业务工作台。

范围：

- 把连接器配置收敛到存储位置详情，迁移旧入口和可见权限。
- 位置列表/详情、用途映射、健康与统计、缓存维护、迁移/退役影响预览。
- 消除重复数据集 CRUD 表，保留必要的影响计数和定位入口。
- 统一平台对象字节、源数据逻辑字节、物理容量与配额的口径。
- 将既有视频资产失败与重试保留在合适的上下文中。

暂不做：在存储页编辑样本标签、项目标注任务管理、训练版本管理、代替各对象存储产品的完整管理员控制台。

## 3. 用户故事与切片

| 编号    | 用户故事                                                                     | 切片与完成结果                       |
| ------- | ---------------------------------------------------------------------------- | ------------------------------------ |
| E09-S01 | 作为管理员，我希望只维护一份存储位置和连接配置。                             | M1：旧连接器入口迁移，配置所有权唯一 |
| E09-S02 | 作为运维人员，我希望看到各位置真实类型、健康和统计时间。                     | M1：动态属性、未知/过期/失败状态     |
| E09-S03 | 作为数据负责人，我希望从来源问题跳到位置，再返回原数据集。                   | M1：带权限和上下文的双向深链         |
| E09-S04 | 作为管理员，我希望分清源数据规模、平台保存量、缓存、配额和真实容量。         | M1 正确口径；M2 分项统计             |
| E09-S05 | 作为管理员，我希望明确九类平台用途分别使用哪里，迁移时不会漏项。             | M1 展示映射；M2 受控变更工作流       |
| E09-S06 | 作为运维人员，我希望清理平台缓存、重试失败媒体资产，而不损害原图和冻结版本。 | M2：影响预览、阻断理由与异步结果     |
| E09-S07 | 作为管理员，我希望禁用、迁移、退役位置前知道哪些业务会受影响。               | M2：按引用范围预览，接入既有任务详情 |

## 4. 页面归属与功能规则

### 4.1 导航收敛

存储管理只保留“位置”和“用途映射”两类主入口，必要的运行状态直接融入位置详情。不新增与位置同义的独立连接器页面。

数据集页不再有完整“数据连接器”页签；接入向导只选择可用位置、填写数据集来源规则。如果需要新增位置，按权限进入存储配置，再返回原向导。

无权新增位置的用户看到“请选择已授权位置”及管理员操作说明，不以报错堆栈或服务器路径提示代替引导。

### 4.2 位置列表与详情

列表字段：名称、类型、站点、启用状态、最近探测健康、索引源规模、平台对象字节、配额和更新时间。

启用状态与运行健康分开。管理员主动禁用不能显示为网络离线；一次探测成功也不能宣称全部历史样本永久可用。健康详情应说明检测了连接、列举、读取还是写入能力。

只读外部源不能通过写测试文件来探活。允许写的托管位置采用受控测试前缀与清理策略，具体由 EPIC-02/12 定义。

位置详情包含连接配置、授权范围、容量/缓存、平台用途和维护操作。秘密字段只展示“已配置/待更新”，不回显明文。

### 4.3 数据集引用与跨页跳转

存储页不再重复完整数据集目录表。显示“受影响数据集 N 个/来源 N 个”及“查看数据集”链接，将 `locationId` 等上下文带入数据集列表过滤。

该计数与深链按用户权限裁剪；没有数据集读取权限的运维账号只展示经授权的运维摘要，不泄露数据集名称、样本或项目归属。

来源异常在数据集页显示摘要，配置变更在存储页执行；两页使用同一状态合同。跨页返回保留数据集筛选、页码和选中来源。

### 4.4 用途映射

映射应覆盖 annotations、datasets、bug-reports、media-cache、audit-archive、import、export、avatars、training 九种现有用途。用途是业务标签，不要求所有后端都暴露同名物理桶。

每条映射展示位置、桶/前缀或受控目录、读写策略、配置版本、生效状态。普通数据集管理人员不因此获得修改平台全局用途的权限。

修改映射属于受控配置/迁移流程，不能把一个下拉框保存成功当作数据迁移完成。旧数据读取与新数据写入的过渡期由 EPIC-01/07 统一处理。

## 5. 统计口径与错误语义

| 展示指标                        | 定义与要求                                                                 |
| ------------------------------- | -------------------------------------------------------------------------- |
| 已索引源数据量                  | 已登记源文件的逻辑字节；按物理引用去重，同一文件被多个数据集引用不重复累计 |
| 平台对象字节                    | 平台拥有的托管原图、缓存、冻结副本和其他对象的逻辑大小；明确是否含历史版本 |
| 托管原图/预览/按需缓存/冻结副本 | 按对象用途分类；多用途引用必须有稳定归属规则，不能在总计中多次相加         |
| 物理总/剩余容量                 | 仅使用后端或主机可信报告；对象字节不能推导磁盘容量或副本冗余开销           |
| 配额与已用额度                  | 平台策略值；显示计费/计量范围，不能冒充底层磁盘余量                        |
| 统计时间与范围                  | 提供 measured_at、完整/部分/估算状态以及数据覆盖范围                       |

不能将多个不同存储后端的“总容量”随意相加。共享物理盘、外部配额、重复挂载和未知值存在时，默认按位置展示，并说明汇总范围。

查询失败保留上次成功数据并标记过期。未知值使用“未提供/待统计”，不能变成 0；部分探测失败不能将位置下全部样本标为已删除。

“刷新状态”触发轻量查询或后台统计任务，前端请求不等待逐桶全量扫描完成。异步统计与对账实现归 EPIC-12。

## 6. 数据与 API 草案

| 合同                   | 关键字段或行为                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------------------------- |
| StorageLocationSummary | id、name、kind、site_id、enabled_state、health、capabilities、usage、quota、measured_at                        |
| StorageLocationDetail  | 脱敏连接属性、授权范围、能力测试结果、用途引用、allowed_actions                                                |
| StorageUsage           | metric、value、unit、scope、source、coverage、is_estimate、deduplication_basis、includes_versions、measured_at |
| StorageBinding         | purpose、location_id、container_or_prefix、generation、effective_state                                         |
| ImpactPreview          | 受影响引用数、允许迁移范围、阻断项、目标配置版本和有效期                                                       |

建议接口包括 `GET /storage/locations`、`GET /storage/locations/{id}`、`GET /storage/bindings`、位置维护预览与任务提交接口。EPIC-02 拥有最终配置合同；本 Epic 负责消费与 UI 体验。

健康/用量接口应读取最近成功采样及对账结果。接口返回可执行动作和拒绝理由，前端不以当前用户角色字符串自行推断全部权限。

## 7. 验收标准

| 编号     | 可验证标准                                                                                          |
| -------- | --------------------------------------------------------------------------------------------------- |
| E09-AC01 | 数据集页不再存在完整连接器配置页；旧入口可跳转到唯一存储位置，原权限不扩大。                        |
| E09-AC02 | 存储页不再复制完整数据集 CRUD 表；“查看受影响数据集”正确过滤数据集列表并保留返回上下文。            |
| E09-AC03 | 后端分别配置目录、S3 和 SFTP 类型时，界面显示实际类型和能力，不硬编码 MinIO 或 HTTP。               |
| E09-AC04 | 无物理容量数据时显示“未提供”，不绘制伪造磁盘占比；已索引源字节与平台对象字节分开展示。              |
| E09-AC05 | 同一目录文件被两个数据集引用，位置源数据汇总不重复计算；具有两个实际平台副本时按真实副本范围计量。  |
| E09-AC06 | 九类业务用途在映射视图中均可定位；未配置项明确提示，不能被缺省隐藏。                                |
| E09-AC07 | 健康或用量刷新失败后保留旧值和时间，标记过期；不会显示用量归零或所有样本已删除。                    |
| E09-AC08 | 外部源只读位置没有删除原文件按钮和写探测；平台自有无引用缓存可在影响预览后按策略清理。              |
| E09-AC09 | 禁用/退役含活跃引用的位置时显示阻断或影响预览；提交任务后展示实际任务状态，不提前显示迁移完成。     |
| E09-AC10 | 无位置管理权限用户通过直接 URL 或修改请求无法读取凭据和配置；运维摘要也不泄露无权查看的数据集名称。 |

## 8. 迁移与回滚

1. 先新增位置展示合同，把旧单一存储后端及连接器映射为位置，保留连接配置身份与所有权。
2. 修正文案与指标：总容量改为准确对象字节/占用描述；移除固定产品名和协议。
3. 将旧数据集页的连接器入口改为兼容跳转，保持用户工作流和授权。
4. 删除存储页重复数据集管理表，以受影响计数和筛选深链替代。
5. UI 回滚仅回退展示，不撤销已确认的位置配置或用途迁移。若旧页面不能正确表达新位置，保留新版只读入口，不把新位置伪装成 MinIO。

## 9. 测试与验证

- 覆盖单一旧托管后端、只读 NAS、外部 S3、SFTP、离线站点等位置组合。
- 使用已知重复引用数据核验统计口径，测试历史版本和缓存共用对象不被重复累计。
- 覆盖普通标注人员、数据集管理者、位置管理者、超管的直接 URL 与 API 访问。
- 测试未知容量、部分结果、缓存过期、探测失败与维护中状态。
- 验证存储页不触发全量数据集拉取、不在页面请求中同步执行全桶扫描。
- 执行旧连接器入口迁移、任务查看、失败媒体资产重试和上下文返回回归。

## 10. 风险与待决策

| 风险/问题                    | 草案处理                                                  |
| ---------------------------- | --------------------------------------------------------- |
| 用户习惯在数据集页创建连接器 | 向导提供有权限的跳转与返回，不保留第二份表单逻辑          |
| 不同后端的容量指标不可比     | 明确逻辑对象字节/物理容量/配额，未知值不上估计占比        |
| 位置名和来源名混淆           | 位置描述共享物理入口，来源描述某数据集的扫描范围          |
| 多个位置实际指向同一目录     | 配置层标注别名/共享物理范围；无法确认时不宣称全局去重汇总 |
| 存储操作影响大量业务         | 统一影响预览、配置版本校验和异步任务，禁止立即隐藏失败    |

## 11. 工程任务与完成定义

- [x] 确认信息所有权矩阵、导航和兼容路由，移除重复数据集管理入口。
- [x] 完成位置列表/详情与用途映射组件，复用现有设计系统。
- [x] 接入权限裁剪、实际连接属性与能力展示。
- [x] 接入分项用量、统计时间、未知/部分/过期状态；来源规模与平台对象分类分别见E12-C2／C3。
- [x] 完成位置到数据集的过滤深链及返回状态恢复。
- [x] 保留媒体资产失败查看和实际重试派发。
- [x] 将媒体重试接入统一任务详情；返回本人可见的Job，跟随真实资产结果，关闭后刷新失败列表，后台任务与终态通知可重新进入，见E09-B6 Outcome。
- [x] 完成已有维护影响盘点、版本化配置确认、逻辑位置访问排空／停止／恢复及真实结果展示，见E11-B1／B2／C1。
- [ ] 完成实际迁移／物理退役的受控提交与结果展示，按E02／E07原始合同实施。
- [x] A：通过页面整合验收和回归，更新页面归属、迁移说明及未知指标口径；运维统计和维护工作流另行验收。

完成定义：用户能明确区分“管理数据集”和“维护存储”，每项配置只有一个权威入口；所有指标可追溯口径，不依赖写死的后端名称或虚假的容量假设。

## 本项目推进切片与晋升条件

产品/前端确认字段 owner 和旧链接迁移表；后端提供脱敏 allowed_actions、覆盖与时间。位置配置/绑定归 E02，采样归 E12，本 Epic 不重复建 API。

| 切片  | 独立结果           | 交付范围                                                                     | 验收 / 回退边界                                            |
| ----- | ------------------ | ---------------------------------------------------------------------------- | ---------------------------------------------------------- |
| E09-A | 归属去重和准确指标 | 位置/九用途展示，迁移页签及向导连接入口，重复数据集 CRUD 换成计数/过滤深链。 | 唯一配置入口、权限不扩大、未知容量不画占比、视频维护保留。 |
| E09-B | 运维工作流         | 消费 E12 统计/健康、E11 影响预览、E07 迁移任务；不造第三份配置或任务状态。   | 刷新不实时扫描全桶；过期不归零；版本条件下提交维护动作。   |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[StoragePage.test.tsx](../../apps/web/src/pages/Storage/StoragePage.test.tsx)、[StorageConnectionsPanel.test.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.test.tsx)、[DatasetsPage.test.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.test.tsx)、[test_storage_event_loop.py](../../apps/api/tests/test_storage_event_loop.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-connections.md](../../docs-site/user-guide/datasets/storage-connections.md)、[storage-buckets.md](../../docs-site/dev/reference/storage-buckets.md)、[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

E09-A 归属去重和准确指标及 E09-B1 平台占用快照完成；E09-B 其余范围保留在本计划，未宣称完整 Epic 完成。

- 提交：`e2fe33d5f` 晋升复核，`525f79026` 查询/权限/训练范围合同，`36248a30c` 存储页面，`a683a824f` 数据集页面，`55d0c3051` 实际验收发现的观测刷新和预览并发修复。未分配发布版本。
- 后端：既有 129 项及最终新增 13 项共 142 个不同用例通过。首次组合运行 135 项；审查补充后 workbench 13 项全部通过，重复用例不累计。覆盖两来源共享成员、可见来源选择、旧差异不复活、扫描观测排序、并发删除、权限撤销、唯一位置计数、分项目进度、真实 MinIO 预览、数据集限定冻结和项目任务分页。
- 前端：集成分支 16 文件 160 项通过；观测/预览修复 5 文件 28 项通过；预览有限重试补充后该 hook 8 项通过。去重合计 20 文件 188 个不同用例。typecheck、受影响 ESLint、CSS token lint、完整 pre-commit 和 `git diff --check` 通过；CSS lint 仅保留既有训练比较页两处 z-10 建议。没有宣称全量 Web 或远程 CI 验收。
- 合同与文档：OpenAPI 一致性和本地 TS 生成通过，SDK OpenAPI 合同 4 项通过；新增 Web 专用接口在 coverage manifest 明确排除。文档 frontmatter 196 文件、navigation 198 页面通过。用户说明位于 `docs-site/user-guide/datasets/{index,import-images,storage-connections}.md`，接口说明位于 `docs-site/api/guides/{datasets,storage-locations,training-datasets}.md`；`README.md`、`docs-site/dev/reference/storage-buckets.md` 和 `CHANGELOG.md` Unreleased 同步。
- 实际浏览器：当前工作树 3100/8100，无 mock。列表名称筛选返回、旧 dataset/source/scan 链接直接跳详情、独立四页签、网格和单项检查器、来源↔位置保留扫描/列表上下文、按位置过滤数据集、关联训练版本深链、深浅主题截图和检查器 Escape 焦点恢复通过。独立存储页面网络没有数据集全量目录或 `/storage/buckets` 请求；真实节点探测成功，九用途和未知容量显示正确。导入向导往返后路径/匹配规则/递归选项恢复，未创建测试连接或数据集。
- 真实数据观测：D-7 专用来源只读扫描和完整预检显示 1 项已核验、1 项缺失、1 项内容变化，三个原成员/任务/标注保留；预检完成后工作台和检查器刷新，三张旧内容预览正常，异常项没有原图读取动作。训练入口预选 D-7，明确确认后实际 `/tasks` 请求带 dataset_id；单项预检报告 5,959 字节并正确阻断不足两张合格图片的创建。原 ready 版本的三张原图、标签和 ZIP 摘要再次校验不变，未新建模型训练。
- 清理：仅销毁经 owner/零客户端核对的 `1822b025369a33f4:test` 资源；测试日志、报告、缓存、截图和专用浏览器会话清理。两份任务工作树在干净且补丁等价已验证后由 Orca 删除；人工 dev 环境、九用途桶、演示来源、已有冻结版本和 `.worktree/manual` 保留。未重建后端镜像、未推送、未发布。

- 剩余范围：E12-A1 的平台异步对象盘点与过期快照已由 E09-B1 接入；外部源分项、物理容量/配额，以及 E11/E07 维护影响预览、迁移/退役提交仍待独立合同。当前用途映射只读；未知指标不估算，不提供伪迁移或源文件删除。来源摘要中的 dataset_count 是按当前权限去重的关联数量，不是源字节或磁盘容量。

### E09-B1 本轮范围

按 [E12-A1 实施合同](1792661016_dataset-storage-operations-recovery.md#本轮定稿e12-a1--e09-b1)接入平台九用途的异步对象盘点与成功快照：默认折叠的统计明细、采样窗口、未知/零/过期/失败/部分覆盖、独立刷新与启动、统一居中任务详情。配置与数据集导航不重建。E12 进一步分项归因、物理容量、配额，以及 E11/E07 的清理/迁移/退役仍保留为后续范围，不宣称 E09-B 全部完成。

E09-B1 已随 `0e85ce496` 完成，最终验收与清理见 [E12 Outcome](1792661016_dataset-storage-operations-recovery.md#outcome)。91 项本地用例及真实盘点、取消保留、重新盘点、居中弹窗、明暗/窄屏验收通过；不重复计为 E09 的另一套统计或任务体系。

### E09-B2 来源诊断

随 `58a9725cf` 和 [E12-A2 实施合同](1792661016_dataset-storage-operations-recovery.md#本轮定稿e12-a2--e09-b2已交付)交付位置内来源的原件/预览/扫描/预检只读诊断；只统计保留的原地引用业务样本，不混入托管复制或源容量。来源分页和任务历史独立，活跃轮次、最近失败及上次完整成功分别表达，继续使用现有居中任务详情和上下文深链。

59 个不同本地用例及明暗/窄屏真实浏览器验收通过，详细证据与清理见 [E12 Outcome](1792661016_dataset-storage-operations-recovery.md#outcome)。E09 仍保留 E11 清理与 E07 迁移/退役等维护影响工作流，不宣称整个 Epic 完成。

### E09-B3 位置维护盘点

2026-10-24 随 [E11-B1](1792661016_dataset-permissions-lifecycle.md#outcome) 完成只读位置维护盘点：现有居中详情默认折叠，展开与刷新GET汇总全部保留来源和业务关系、活动轮次、副本账本/租约及有证据的冻结版本，说明未覆盖历史和配置副作用。超级管理员与datasets:read前后核验，失败保留旧时间证据，撤权隐藏；不生成提交许可或新增维护写操作。

43个API/DB、21个Web和4个SDK不同用例通过，明暗/桌面/窄屏实际浏览器、Enter/Escape/焦点及零溢出/失败请求验收通过，详细记录和清理见E11 Outcome。正式用户/API/架构文档与OpenAPI同步；无迁移/Worker变更、不重建镜像，人工环境保留。候选配置、版本化变更、归档/恢复、回收和迁移仍属后续合同，整项E09继续活动。

### E11-B2 位置配置确认

使用范围和缓存配置共用候选预览与居中确认，绑定五分钟令牌、位置/连接修订、当前身份/Key及关系指纹；变更或到期要求重预览。189个不同本地用例、明暗/桌面/窄屏与真实临时位置三次保存验收通过，既有人工位置配置及22项计数未变。完整证据及清理见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-b2-位置配置候选预览与确认)。全owner静止、归档/恢复、迁移/退役与物理回收仍在后续合同中。

### E11-C1 逻辑位置访问维护

位置详情默认折叠展示当前维护状态、修订、未收尾访问及安全节点/目的，停止须零记录，drain/stop/resume均采用居中预览确认。控制开关默认关闭并解释全API/Worker升级条件；过期/冲突要求重预览，会话/对象/配置隔离及撤权隐藏旧证据。维护组件24项随完整Web6541项通过，实际持有FD的访问阻止停止，句柄结束后停止/恢复成功；明暗、1440/390宽度、Escape/焦点及零溢出/失败请求通过。后端、部署、621项API/DB、SDK4项、原件/业务保留及清理详见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c1-逻辑位置访问维护)。其余受控物理回收、迁移/退役和整项E09仍活动。

### E11-C2-A 托管原件保留预演

平台对象占用卡片新增默认折叠的保留预演，独立分页和居中详情；按断言展示完整计数、重复大小口径、观测时间及引用/保留/未知资格原因。默认不查询，撤权隐藏，会话修订隔离，未知期限为无限期保护，没有删除按钮。含新增16项共36项Web用例通过，真实11样本删除后10+1分页、1440/390明暗及Escape/焦点通过；后端、隔离恢复、187个不同本地用例和精确清理见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-a-托管原件保留账本与只读预演)。C2-A不覆盖所有物理owner，也不提供回收许可，E09继续活动。

### E11-C2-C 已登记原件完整核验

现有居中原件详情新增默认折叠的完整核验区，展开才 GET 历史，显式后台读取完整 SHA/大小，显示时间/预算、匹配/变化/缺失及取消结果；摘要另折叠，pending重派、取消请求与未收尾责任分别表达，继续复用通用任务详情。53个不同Web用例和1440/390明暗、真实分页/重派/取消/嵌套Escape/焦点验证通过。342个不同本地用例、真实Worker/MinIO、完整历史恢复与清理见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-c-已登记原件完整内容核验)。旧结果不提升为独占、最小副本或删除资格；人工环境保留，整项E09继续活动。

### E11-C2-D 新原件条件创建与直传隔离

新数据集上传使用暂存URL并由后端条件创建最终原件，旧URL重放不覆盖已接受内容；ZIP、连接与目录复制共用耐久创建责任和完整回读，扫描不补造证据。保持现有UI合同，实际上传及居中样本详情已验证；修复数据集Task文件链接的用途选桶。363个不同本地用例、四入口实际验收与完整历史恢复、人工环境保持及清理见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-d-新托管原件条件创建与直传隔离)。固定版本消费、生产独占及物理回收继续活动。

## 当前原始故事与验收复核

2026-10-25 基于当前实现与各已交付Outcome核对，不重跑已通过的历史验收，也不按旧切片的历史“剩余范围”重复建设。

| 原始故事                 | 当前结论                                                         | 实现与既有证据                                                                                                          |
| ------------------------ | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| S01 唯一配置入口         | 已实现                                                           | DatasetsPage旧页签跳转至StorageLocationsConsole；A实际导航／权限验收。                                                  |
| S02 类型、健康与统计时间 | 现有filesystem／S3／SFTP范围已交付                               | storage_location.to_out、分节点探测与Console状态；A／B1／B2既有验收。                                                   |
| S03 双向权限／上下文跳转 | 已实现                                                           | StorageLocationSourceSummary与数据集URL状态；A实际往返。                                                                |
| S04 准确分项用量         | 当前可支持范围已交付                                             | StorageSourceUsage／StorageUsagePanel，E12-C2跨Dataset物理引用去重、C3准确对象六分类；物理容量／提供方配额保持未知。    |
| S05 九用途与受控变更     | 展示与头像受控变更已交付；报告附件B8已验收并集成，另外七用途锁定 | B5展示策略与修订；B7／B8通过真实托管探针及版本化确认切换头像／报告附件，历史对象按保存位置处理；B8已集成，原GET仍只读。 |
| S06 缓存维护与失败媒体   | 缓存影响确认／准确回收及媒体重试与共享任务详情已交付             | StorageOriginalCachePanel与StoragePage.retry；B6按实际输出收尾同步本人Job，含派发未确认和部分派生失败。                 |
| S07 维护影响             | 盘点、配置确认与逻辑维护已交付；实际迁移／物理退役未交付         | E11-B1／B2／C1的影响、确认及drain／stop／resume和真实结果；不声称已提供迁移／退役执行链。                               |

原始AC01–AC03及AC06复用唯一入口、过滤／返回上下文、动态类型和九用途展示；AC04–AC05由E12-C2／C3的分列和去重证据覆盖；AC07复用快照／探测／来源摘要的失败保留及过期显示；AC08复用只读源与受控缓存清理；AC10复用位置／来源权限裁剪及超级管理员映射路由。AC09仅在当前禁用／配置确认／逻辑维护范围内有已交付影响与真实状态证据，实际迁移／物理退役任务仍未交付。完整E09继续活动，不归档；读写策略补齐没有新增模型、Tracker、会话或审核验收条件。

## E09-B5 Outcome：用途映射读写策略与位置修订

2026-10-25补齐原始E09-S05／§4.4。沿原绑定GET增加`consumer_owned_compatibility / deployment_mapping / legacy_mapping_locked`三策略及实际`location_config_revision`；既有九用途、共桶逐行、缺失／配置失配和绑定代次保持。位置修订在原只读查询中outer join取得，无逐用途查询、初始化、源I/O或写入。策略如实表达已有业务读取和部署新写入，不将来源只读能力套用到托管存储，也不声称历史位置全覆盖或迁移完成。

现有映射表展示两行一致正文的读写政策，绑定代次下单列位置修订；旧响应缺新字段保留未知。1440×1000与390×844、明暗主题实际查看；窄屏表格保持内容宽度，横向滚动限于309px容器，表格746px而页面仍390px，链接不逐字换行。位置链接能打开原居中详情，Escape关闭后可返回映射；刷新正常，九用途与锁定说明可见。

10个API／真实DB、13个Web和4个SDK合同用例通过，覆盖空配置不初始化、共桶九行、代次2与位置修订7独立、失配不被修复、GET零对象存储调用、非超管拒绝及旧字段未知。OpenAPI／本地Web类型已更新；SDK端点仍保持原有明确排除，不虚增覆盖。tsc、完整Web lint／CSS tokens、Ruff／格式、文档frontmatter／导航／可读性及构建通过；Web仅既有4个lint警告及训练比较页2处样式建议，构建仅既有语法高亮回退。

正式说明同步README、CHANGELOG Unreleased、用户存储指南、存储位置API指南和桶参考。本轮不改对象路由、Worker、迁移、依赖或镜像；API按原reload进程更新，人工3100／8100保留。完整E09的受控用途变更、媒体重试共享任务详情和实际迁移／物理退役仍按上述原始矩阵推进。

现场控制台无error／warning、页面异常为零；捕获网络无HTTP错误，未完成／无状态记录不计成功。专用浏览器会话已关闭。一次性test库、Redis及九桶按准确owner销毁并独立确认不存在，资源清单移除；本轮截图、原始网络／登录状态、报告和检查缓存清理，仅保留有界核验摘要。人工两Worker／绑定Beat健康，12表155行、24对象及12源文件完整指纹不变；正常提交钩子与最终差异检查纳入提交验证。

## E09-B6 Outcome — 媒体重试共享任务详情

2026-10-25 完成原始 E09-S06／§11 的媒体重试任务详情接入。沿 `VideoDerivativeOutput` 保存的认领、发布、失败与既有过期事实同步私有 `video_asset_retry` 任务，本人对同输出／资产类型的活动请求复用，其他管理者得到自己的可见任务。任务不关联任意项目，不保存原件／物理位置／凭据，不借用共享 Celery ID；不改变媒体生成算法、原授权、取消合同或维护资格。元数据发布仍可伴随海报／时间表失败，任务按所请求资产判定；派发未知保持等待。未知未收尾访问仍沿现有输出维护处理，未新增立即判定进程崩溃的承诺。

重试后使用共享居中详情；后台任务和终态通知均可重新打开。关闭后刷新失败列表并将焦点返回稳定的“视频资产失败”标题，避免成功行消失导致焦点丢失；旧元数据准备503分支保留。相关113个不同API／DB用例全部通过，含本人复用／跨人隔离、晚加入running、五类资产、部分失败、生成失败、派发未知、既有过期与真实ffmpeg／MinIO生成；37项Web及4项SDK合同通过。首次新夹具的同session ORM更新时间刷新和旧consumer stub已修正，没有放宽生产校验。

扩展通用任务回归另有21项通过、1项既有批量预测夹具失败：`test_retry_failed_batch_predict_items_queues_recorded_failures` 无Task／执行快照，现行严格重试合同返回409。用 `a92d59a38` 的五个实际导入生产模块只读覆盖，在同一隔离测试入口单独复现相同失败；本片没有修改该重试路由、服务或旧夹具，也不把其整改追加为本轮前置。API snapshot与本地Web类型同步，类型检查、Web lint／CSS tokens、Ruff／格式、文档frontmatter／导航／可读性／生成索引和完整构建通过；仅有原有4项ESLint警告及2项无关样式建议。

真实TCP API→独立Celery→MinIO的四次UI重试得到三成功／一失败，四条终态通知与失败列表同步。1440×1000和390×844明暗截图、成功／失败详情、Escape、成功行移除后的焦点、后台任务及通知重入均核验；页面无横向溢出、控制台error／warning和页面异常为零，1169条捕获网络记录无HTTP错误，10条无状态记录不计成功。正式说明更新README、CHANGELOG Unreleased、用户存储指南和视频派生API指南。

人工3100／8100、两个73任务Worker与绑定Beat已通过所属监督服务刷新，真实空媒体任务派发成功；12表155行、24对象及12源文件完整指纹不变。test／e2e各自库、Redis和共18桶按owner销毁并独立确认不存在，资源清单移除；专用浏览器、截图、登录状态、原始网络、报告和任务缓存清理，仅留有界核验摘要。不重建镜像，不推送或发布。完整E09仍保留原始受控用途变更及实际迁移／物理退役；下一步核对受控用途变更的存储绑定合同，不展开Tracker、会话或审核专项。

## E09-B7 Outcome — 头像用途受控切换

2026-10-25 完成原始 E09-S05 的头像用途变更，与 [E02-B14](1792661016_dataset-storage-locations-connections.md#e02-b14-outcome--头像新写入位置受控切换) 同次交付。现有用途映射提供“切换头像位置”，复用居中 Modal、分页候选、独立标签与灰色提示、真实托管探针、历史影响预览和五分钟确认。目标、用途代次、配置、权限或历史变化使旧预览失效；上传历史缺少定位时显示具体阻断。其余八用途保持原策略与锁定状态，不用只读来源能力推断托管写入资格。

旧头像和在途上传继续使用保存位置，新头像才使用新绑定；外部头像目标不在当前部署端点的盘点范围内时明确显示未覆盖，小计不冒充所有位置总量。同桶同键按端点和准确归属分类。成功提交刷新用途映射及占用覆盖提示，稳定外层触发器引用修复绑定更新后焦点丢失；Escape 关闭和成功提交均已实际核验。

47 项不同 Web 用例全部通过；后端、迁移、SDK、恢复、真实 A／B 与独立 Worker 证据及一项既有 input 夹具失败准确记录于 E02-B14 Outcome，不重复累计。实际明暗／1440×1000 与390×844验收无页面溢出，控制台和页面异常为零，捕获请求无 HTTP 错误，10 条无状态记录不计成功。人工3100／8100、两 Worker／绑定 Beat及原业务／对象／源文件指纹保持；test／e2e资源、专用B容器和本片隔离树已按归属清理。

用户／API／开发／恢复说明及 README、CHANGELOG 同步更新。九桶恢复对新头像代次、provider版本回执及外部目标明确拒绝，不宣称完整恢复覆盖。本轮仅推进原始用途绑定要求；完整 E09 的其余用途变更和实际迁移／物理退役继续活动，不新增 Tracker、会话或审核验收前置。

## E09-B8 Outcome — 报告附件新写入位置受控切换

2026-10-25，基于`79cca82c7`的独立树完成原始E09-S05／E02-S04、AC05／AC10的报告附件切片，与[E02-B15](1792661016_dataset-storage-locations-connections.md#e02-b15-outcome--报告附件新写入位置受控切换)同次提交。本节是隔离实现与本地验收结果；根C6仍运行，未集成或刷新人工环境。头像公开适配器保持，窄共享S3目标／探针仅接受头像与报告附件，另外七用途锁定，完整E09仍活动。

报告附件分配按连接→Location→Binding共享锁捕获当前目标，pending独立责任在PUT前提交；确认按同序复核代次、修订、权限、维护和已知上传影响。schema1保持原解释，schema2固定Location、endpoint、bucket、受控前缀完整物理键与代次，公开`storageKey`不变。provider实际VersionId一次性记录，读删固定该版本；当前位置提供当前凭据，正常轮换不重解释旧地址。旧未知附件继续在首次绑定／读取时拒绝，不进入准入扫描或阻断can_apply；全部pending／ready／retired保护原位置。分类按端点、桶及完整键匹配，当前部署A盘点不认领外部B。

0245保留旧行并增量约束schema2和版本回执；旧事实可往返，有新schema／版本／非1代绑定时拒绝破坏性降级。九桶恢复在修改映射前拒绝这些尚未覆盖的事实，失败保留全部原映射；本片不授予外部目标的备份／恢复资格。

| 验证层级                  | 已完成结果                                                                                                                                                                                                            |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 本地API／真实隔离DB与迁移 | 309个不同用例全部通过、无跳过；含新附件与受影响头像、权限／修订、未知历史不阻断、全部状态引用保护、准确分类及迁移往返／拒绝。                                                                                         |
| 本地Web组件               | 53个不同用例通过：原头像20、报告附件19、用途控制台14，含异步响应归属、权限变化、失效预览及稳定焦点。                                                                                                                  |
| 恢复纯检查／SDK合同       | 分别50项、4项通过；SDK三新端点准确排除，不虚增覆盖。                                                                                                                                                                  |
| 实际TCP API／独立B MinIO  | A上传后切换B，B新上传保持原逻辑键；旧A／新B、同键新增版本后、正常凭据轮换后共4次TCP下载核原字节，准确退休只删除已保存版本并保留较新版本；旧确认409。                                                                  |
| 真实在途／失败目标        | 额外ASGI鉴权／DB请求先保存pending A，切换B后仍实际写A、发布ready并原字节下载；坏凭据实际S3探针拒绝，配置API拒绝且原绑定保持。已知只读、忽略条件、内容篡改、未知收尾等分支由共享探针定向测试覆盖。                     |
| 实际浏览器                | 独立会话在1440×1000／390×844、明暗四组合逐张检查；目标选择、真实预览、B→A确认、Escape及成功后的焦点返回通过。页面与弹窗无横向溢出，控制台／页面异常为空；捕获预览POST、绑定PATCH和刷新为200，其他捕获请求无HTTP错误。 |

实际B提供方为MinIO`RELEASE.2025-09-07T16-13-09Z`（`07c3a429bfed433e49018cb0f78a52145d4bedeb`），boto3／botocore均`1.42.96`。显式请求`VersionId=null`时允许提供方省略等价响应头，非null仍必须准确回显，并保留条件ETag和前后HEAD核验；没有扩展其他提供方资格。本地test／e2e分别使用核验owner的库、Redis及九桶，与父树仅共享宿主PostgreSQL／MinIO服务，不是生产或多租户安全隔离结论；本片没有启动Worker，不声称独立Worker或远端CI验证。

OpenAPI／本地Web类型／API索引及正式用户、API、架构、恢复说明、README和CHANGELOG已同步；tsc、完整Web lint／CSS tokens、Ruff／格式、文档frontmatter／导航／可读性和构建通过，仅保留4项既有ESLint警告、2项无关样式建议及既有语法高亮回退。旧上传夹具按真实共享锁读取顺序补齐stub；生命周期夹具补真实NoSuchLifecycle读取并验证既有移除规则，没有修改生产生命周期实现或放宽生产锁／归属约束。

专用浏览器及API／Web已停止。test／e2e两库、两Redis和18桶已按准确owner销毁，并独立只读确认不存在、资源清单移除；自有B容器及其独占匿名数据卷已删除并确认不存在。检查后的四张临时截图、登录状态、密钥、原始网络／日志和本轮缓存清理，仅保留本有界结果。根3100／8100、两个Worker／Beat、业务与原件及C5／C6资源未触碰；本片未应用根迁移或刷新根运行栈。正常提交钩子与最终diff检查纳入提交；主树合入、人工0245升级／刷新由父任务按测量冻结边界继续，整项E09不归档。

### E09-B8 补充修复 — 显式 null 槽位退休

独立复核发现本片新删除路径把字符串`null`与非null provider版本一同直接删除；Suspended桶的null槽位仍可被同key写入替换，因而可能删除替换内容。现改为null在删除前走保存SHA／大小、条件GET和前后HEAD核验，观测到替换即拒绝物理删除并保留retired owner及重试责任；非null仍按准确保存VersionId删除。该内容核验不把可覆盖槽位变成不可变版本，也不宣称封闭并发外部写入。

仅相关存储／上传责任两文件的180个不同用例通过、无跳过，包含新增5例：同长度替换null拒删、匹配null正常清理、非null准确删除，以及真实DB中替换时保留责任／匹配时清理责任。额外所属test上的真实MinIO三场景全部通过：Suspended null替换字节和retired owner均保留，匹配null删除成功，Enabled非null旧版本准确删除而较新同key版本保持；不是模拟S3结果。延续先前提供方版本及共享宿主隔离边界，不重跑历史资格或UI矩阵。产品API说明和CHANGELOG已同步，Ruff／格式、文档frontmatter／可读性及正常提交钩子纳入本次修复提交；原`3e24a78c`保留可审查。

本次新建的自有test库／Redis／九桶均按owner销毁并独立确认不存在；三场景夹具、原始日志及检查缓存已清理。根C6、人工环境及根源码未触碰，根集成／0245应用与相关进程刷新仍待父任务；本修复不新增Epic门槛。

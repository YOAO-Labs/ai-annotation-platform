# EPIC-12：运维观测、容量口径与备份恢复

> 状态：approved；E12-A1/A2/A3、E12-A4 四类本地来源故障去重与恢复观测、E09-B1/B2、E12-B1 离线隔离恢复试点、E12-B2 外部原件保护责任、E12-C1 本地目录扫描基线、E12-C2／E09-B4 已采纳源索引规模、E12-C3 平台对象分项、E12-C4 原图读取与 E12-C5 增量同步本地基线、E12-B3 首个生产者的当前对象增量账目与周期对账、E12-B4 托管原件集中新增、E12-B5 四类媒体缓存受控新增、E12-B6 独立导出媒体受控新增、E12-B7 导出 ZIP 写入净变化及 E12-B8 报告附件受控新增已完成；A3／A4 限本地来源故障观测，B2 为声明及本地恢复保留，完整E12仍为部分交付，其余切片待独立复核，不分配发布版本。
> 晋升基线：`5b764eaa5`；工作树干净，保留 `1822b025369a33f4` 的 dev 人工验收环境，不重建后端镜像。

原始§5／工程任务第2项现由E12-B3交付临时输入的真实写删增量及跨盘点对账，B4接入集中托管原件新增，B5接入四类媒体缓存新增，B6接入独立导出媒体新增，B7接入导出ZIP写入净变化，B8接入报告附件受控新增；其他生产者及媒体缓存／导出移除增量仍未完整覆盖。原有LIST快照、额度预约与Creation／Retirement责任账本分别保留，不能将其中任一累计或部分覆盖冒充全平台物理增量；完整工程任务仍未勾完，也不是其他切片的新前置。

## 本轮定稿：E12-B8 报告附件受控新增增量

2026-10-26，基于`32c5bfe3f`继续原始E12-S03／AC01、§5和工程任务第2项。报告附件已有BugAttachmentUpload UUID、保存的物理地址、条件PUT回执、按返回版本完整SHA／大小读回，以及`storage_access.run`的permit／原生线程排空；本片只补共享`bug_attachment_uploads.upload`→`bug_attachment_storage.write`的容量事实，不改报告、上传授权、图片格式／10MiB／像素预算、绑定、180天期限或清理。

新增生产者`bug_attachment_uploads`、owner为BugAttachmentUpload.id、分类`other_platform`，复用现有Recorder／Mutation与调用者Engine上的独立短事务。每次Python条件PUT调用前保存一个耐久意图；意图失败不开始PUT。原SDK配置和重试保持，记账写入使用所属独占client，schema1及schema2默认A沿已有owned选项，外部目标保持原client策略。普通读取和探测不改变，不替换共享实例，不新增条件写能力门槛或上传器。

公开SDK参数／before-send事件仅核对冻结Bucket、Key及IfNoneMatch=\*并数实际发送，回调返回None且不修改请求。不解析签名URL；没有hook、参数不能核实、多次发送或回执未知均保留unknown。只有恰好一次真实提交、明确成功ACK、返回不可变非null版本、既有完整读回的准确版本／SHA／大小一致、当前HEAD仍为该版本，且线程／Body／client和原permit均真实收尾后，才能确认正增量。只有明确单次条件失败412可确认本次零；409、自动重试后412、无版本或关闭不明不推断零，更不解释早期未决窗口。条件写ACK本身证明提交时当前槽不存在，无需引入ZIP的历史版本LIST要求。

物理事实在原异步上传owner中独立发布，后续权限／配置／ready提交失败或业务回滚不抹去已发生写入。复用原guard、revision与取消排空；取消和其他BaseException先收尾账目再按原语义传播。普通记账发布异常不改变成功业务或原始错误，留下pending／unknown。每次实际调用为独立窗口，不按ready状态、报告绑定或同SHA补算历史。现有删除／退休、历史对象及其他生产者继续未覆盖，整体coverage=partial、projected_total_bytes=null，不增加GC或删除许可。

准确范围：新增私有`bug_attachment_accounting.py`及专项测试，现有`bug_attachment_storage.py`、`bug_attachment_uploads.py`、`schemas/storage.py`与相关附件／target／账目回归；共享Recorder或target仅在确需复用窄接口时调整，B7不顺带重构。Web现有StorageUsageAccounting及测试；必要OpenAPI／本树生成类型和SDK合同；README、CHANGELOG、用户storage-connections、API storage-locations、开发storage-buckets、本计划与实施队列。后端在当前HEAD隔离树实现，主树负责Web／文档与集成。无迁移、新公开端点、请求字段、配置、依赖、镜像、权限或格式变更。

仅使用核实owner的一次性test资源：准确保存地址、独立意图先于PUT、真实小图片生产上传与schema1／schema2默认A、重复条件失败、SDK重复发送／丢ACK后412、无hook／null／错误版本／当前替换、完整读取／关闭失败、ready发布失败、重复取消与账目发布异常。检查已改caller的真实线程／PG收尾，相关已有回归、Ruff、OpenAPI／codegen、SDK、Web类型／lint／CSS tokens及正式文档；真实摘要UI只验新增覆盖文案、折叠与明暗宽窄，不重做报告产品或其他消费者资格。主树集成前核实人工3100／8100／0257与所属任务空闲，刷新确需加载的进程，保存原业务／对象／文件并清理本片资源，再正常提交。完整E12及整套Epic继续活动，未覆盖项不新增为本片前置，不推送／发布或执行训练测量。

## E12-B8 Outcome：报告附件受控新增增量

2026-10-26，后端来源`cba929799`与本树Web、正式文档及交付记录同次集成。共享报告附件上传接入`bug_attachment_uploads`生产者，owner为BugAttachmentUpload.id、分类other_platform；每次条件PUT前独立保存准确地址、内容回执和上传意图。保留原SDK配置／重试、上传器、图片／大小／像素限制、授权、绑定、期限与清理；仅该写入使用所属独占client，不改共享实例、读取、B7、通用Recorder或Permit语义。

公开参数／发送事件仅旁路核对Bucket／Key／IfNoneMatch=\*和真实发送次数，回调返回None。单次明确成功、不可变非null回执、原准确版本完整SHA／大小读回、当前HEAD匹配且线程／Body／client及实际permit均收尾时确认新增。仅单次、条件明确且HTTP确认为412的失败计本窗口零；重试后412、409、无hook、丢回执、null／错版本及关闭不明保持unknown。后续撤权或ready发布失败不抹去写入，普通账目发布异常保留原业务结果；新到达的取消在账目排空后仍传播，并继承原收尾不明标记。中途复核还补入captured permit的实际\_settled状态，避免共享许可只报告错误码而未带异常属性时误确认；两处均只在B8窄修。

新增29项专项与223项相关原回归按JUnit去重为252项最终通过，0错误／失败／跳过。首轮一项null版本夹具错误地保存actual版本却回执null，已修正为真实null槽；只重跑该项及新增普通发布异常用例，并首次执行相关旧组225项，真实退出0，不重复已通过的27项。相关组报告18条SQLAlchemy测试事务警告，未扩修其他消费者。固定Ruff、OpenAPI重导／合同、正常提交钩子及diff检查通过；重导无跟踪差异。主树25项Web、4项SDK合同、codegen、Web类型／完整lint／CSS tokens、207项frontmatter、208篇可读性、209页导航与标准文档构建通过；构建40.39秒，远端CI未运行。

真实PG／MinIO在生产`uploads.upload`入口验证小PNG：schema1与schema2默认A均得到ready、准确owner／ACK版本和完整字节，首次正增量；同key第二次条件写得到明确单次412、确认零。schema2保存定位及目标由受控接线构造，物理SDK／对象I/O真实；重试、丢ACK和其他故障用受控SDK事件替身，不冒称真实外部B或完整报告业务资格。真实上传owner还覆盖重复取消、BaseException、ready提交失败、撤权及两类permit收尾失败；后两类保持written／unknown而不改原业务错误。

人工0257及原3100／8100保留；核实任务空闲后经准确所属监督服务刷新API、两个Worker与绑定Beat。最终两个Worker各注册75项任务，API／Web及doctor健康；真实GET声明九个生产者，coverage仍partial、projected_total_bytes仍null。浏览器完成1440×1000／390×844明暗四组合及披露开合，报告附件文案正确，提示11px／弱化前景色，无横向溢出、页面异常、控制台错误或已观察HTTP错误。582条请求中4条无状态不计成功；唯一非只读请求为本地应答的无关在线心跳，未修改周期、账号偏好或业务。初次自动化目标在视口外，显式滚动后完成原生点击与状态确认，没有修改产品控件。

刷新前后177张业务表9,234行、24个对象版本／删除标记及16份媒体文件完整指纹保持。隔离树仅创建test模式；所属数据库、Redis及九桶已销毁，主树独立核实test／dev／e2e均无相应资源或manifest。私密认证、原始请求、截图、日志及构建／测试中间产物按归属清理，仅留有界脱敏摘要。正式说明同步README、CHANGELOG、用户storage-connections、API storage-locations与开发storage-buckets。

本片只补报告附件受控新增；附件退休／清理、其他持久小对象、审计归档、训练与直传等尚未覆盖入口，以及移除增量继续按原始统计范围推进。应用回退保留已有流水，不将这些原始缺口新增为B8或其他功能前置。无迁移、公开端点／字段、依赖、配置、镜像或版本变更；完整E12与整套Epic继续活动，不推送或发布。

## 本轮定稿：E12-B7 导出 ZIP 写入增量

2026-10-26，基于`1d4872f97`继续原始E12-S03／AC01、§5及工程任务第2项。ZIP已有独立ExportOutput、准确保存地址、完整SHA／大小及真实线程收尾，但`upload_file`隐藏PUT／CompleteMultipart的返回版本；仅将成功ZIP大小相加会在同键重试时重复累计。普通导出经`export_outputs.write`，独立媒体导出经`workers/lidar_export.py`的原`_io`直接调用`export_storage.write`，本片覆盖这两个既有异步入口，不按导出格式拆专项。

沿已有SDK managed upload保留64GiB上限、默认分段阈值／并发、重试、上传参数、业务授权、缓存／发布、期限和原清理行为。不为记账强制条件写能力，不用单PUT替换大文件上传，也不改全局共享client或增加部署条件。记账使用所属独占client及[Boto3公开事件](https://docs.aws.amazon.com/boto3/latest/guide/events.html)旁路捕获实际提交／返回；读取和普通签名继续原工厂。缺少版本、历史读取权限或观测不完整时保留unknown，原本成功的ZIP仍成功。

新增生产者`export_zip_outputs`，owner为ExportOutput.id，分类temporary，复用现有Recorder和Mutation表。每次managed-upload调用前独立短事务保存一个意图及观测窗口，覆盖在Complete前失败或未触发事件的上传；公开事件只观察PutObject／CompleteMultipart真实发送次数及最终ACK，不改SDK重试。恰好一次实际final发送且有明确ACK时才可确认；多次发送、丢ACK或不透明上传路径一律unknown，不让后来的成功或412解释早期不明提交。原始Epic不要求逐wire账目，不新增这一协议。before-send回调只返回None，不修改请求；用前置参数事件核对冻结Bucket／Key，不解析签名URL，不在线程共享Session或创建跨循环Engine。

单次final提交前及完整读回后，用HEAD→有界完整[ListObjectVersions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html)→HEAD保存同一准确key的当前状态与版本／marker集合。两个HEAD和LIST唯一IsLatest必须一致；不存在／marker状态亦须互相印证，不能从单独404推断新建。集合的稳定身份排除IsLatest，不按VersionId或时间排序猜先后；有界截断、重复、null或任意观测异常保持未知，不阻断业务。

仅在旧集合无删改、后集合恰好新增本次明确返回版本、全SHA／大小的实际GET响应也是该版本、当前HEAD及所有原生资源均已收尾时，确认`新当前大小－旧当前大小`。首次新增为正，同键等长覆盖为零，可证明缩小为负；这反映当前可见内容字节变化，不是物理版本删除。复用当前业务完整读回，新增观测失败不改变导出结果。ZIP不得先使用共享Attempt.verified自动推断全大小新增，再事后清零；明确净变化须受完整证据门控制。后续业务回滚／授权拒绝不抹去物理事实，发布失败留下pending／unknown，取消先排空再传播。

准确范围：新ZIP观测helper、`export_storage.py`、`export_outputs.py`、`workers/lidar_export.py`、共享target的独占client选项、`schemas/storage.py`及相关存储／输出／Worker／记账测试；Web现有StorageUsageAccounting及测试；必要OpenAPI／本树生成类型、SDK合同；README、CHANGELOG、存储用户／API／开发说明、本计划与执行队列。没有新表／迁移、公开端点／任务参数、依赖、环境变量、镜像、导出格式或上传器替换。GET继续partial、总占用推算null；其他生产者、清理、lifecycle及外部变动仍按原始缺口推进。

验证仅用有owner证明的一次性资源：真实PG／MinIO及实际SDK单PUT和分段ZIP、同键重写不重计、新旧保存目标、准确版本／完整正文与净变化；两个异步入口的原guard和原生排空，SDK重复发送／丢ACK、Complete前失败、无版本、缺历史权限、并发替换／marker／分页异常、意图失败及发布失败。64GiB合同用有意义的边界检查保持，不创建64GiB测量或训练夹具；不重跑全格式／消费者／备份资格。完成相关检查和真实摘要UI明暗／宽窄屏后，确认人工0257及原3100／8100所属Worker空闲并刷新，保存原业务／对象／文件，按owner清理所有测试资源和私密材料，再提交交付记录。

## E12-B7 Outcome：导出 ZIP 写入净变化

2026-10-26，后端来源`1f01f8932`与本树Web及正式文档同次交付。普通`export_outputs.write`和独立媒体`lidar_export._io`两条入口均接入`export_zip_outputs`，以保存的ExportOutput及准确物理地址登记独立上传窗口。保留原managed upload、64GiB上限、分段阈值／并发、SDK配置和重试；不引入条件写准入或新的上传器。公开事件只观察实际final发送次数和明确回执，回调不修改请求。

一次实际final提交、有不可变版本ACK，且HEAD→完整有界版本集合→HEAD的前后状态、旧集合保持、唯一新增版本、实际完整SHA读回与当前对象全部一致时，确认新旧当前内容大小之差。首次新增为正，同键等长覆盖为零，缩小可为负；负量只说明当前可见内容减少，不表示历史版本被删除。重复发送、丢ACK、null、无hook、历史权限不足及前后观测不完整保持unknown，Complete前失败也有耐久意图。关闭／取消仍按原native排空和permit顺序收尾后发布；普通账目发布异常不改变成功导出或原业务错误，取消继续传播，未决记录保留。

213项相关回归、最后39项专项／账目／OpenAPI及2项发布边界检查均真实退出0；按JUnit classname＋name去重为233项，0失败／错误／跳过，其中30项ZIP专项。固定Ruff、正常提交钩子及diff检查通过；OpenAPI重导无跟踪差异，无迁移、依赖、配置或镜像变化。主树24项Web、4项SDK合同、codegen、类型检查、完整Web lint／CSS tokens、207项frontmatter、208篇可读性、导航与标准文档构建通过；构建40.50秒，保留4项既有ESLint及2项既有样式层级提醒，远端CI未运行。

真实PG／MinIO验证物理writer：schema1单PUT的123字节和schema2默认A的9MiB默认managed multipart，分别连续同键上传两次，结果均为`[size, 0]`，实际final发送次数为1，完整读回、ACK和当前版本一致。两条异步入口用真实线程／PG和受控guard／permit验证重复取消、真实关闭先于发布。未执行完整导出Job、外部B或64GiB测量，不将其扩大为本片前置。

人工0257及原3100／8100保留。确认两个所属Worker空闲后，经原监督服务刷新实际代码与绑定Beat；最终两个Worker各注册75项任务，API／Web及doctor健康。第一次就绪检查发生在启动期间，等待同一所属进程后健康；最终发布异常窄修后再次按原服务刷新，没有重建镜像。真实GET声明八个生产者、coverage仍partial、projected_total_bytes仍null。浏览器检查1440×1000／390×844明暗四组合、披露折叠和ZIP实际文案；提示11px、弱化前景色，无横向溢出、页面异常、控制台错误或已观察HTTP错误。335条请求中4条无状态不计成功，仅无关在线心跳本地应答；未修改周期计划、账号偏好或其他业务。

刷新前后177张业务表9,234行、24个对象版本／删除标记及16份本地媒体文件完整指纹保持。隔离树仅创建test模式；所属数据库、Redis及九桶已销毁，主树独立核实test／dev／e2e均无数据库、Redis、九桶或资源manifest。测试私密登录状态、原始请求、截图与构建／测试中间产物按归属清理，仅保留有界脱敏结果。应用回退保留原账目事实；ZIP退休、清理、其他生产者和全平台增量仍未补全，完整E12与整套Epic继续活动，不启动训练测量、Tracker或其他消费者专项，不推送或发布。

## 本轮定稿：E12-B6 独立导出媒体受控新增增量

2026-10-25，基于`04a9e128d`继续原始E12-S03／AC01、§5与工程任务第2项。图片、视频、LiDAR与NuScenes独立媒体均经过`export_media_transfer.copy_source`→`export_media_storage.write_object`，已有准确保存地址、条件PUT、返回版本相关的完整SHA读回及真实资源收尾。本片统一将这一个共享写入入口接入现有账目，不改导出格式、原件读取、业务权限、清理、模型或训练流程。无需真实生产迁移目标，不等待E02-AC10范围答复或完整E07资格。

新增生产者`export_media_outputs`，owner为`ExportMediaBundle.id`，分类`temporary`。复用`media_cache_accounting.Recorder`，仅增加可选显式purpose参数，现有四类默认`media_cache`保持。每次实际条件PUT前在所属Engine的独立短事务登记endpoint／bucket／完整key、owner、内容回执和独立尝试；SDK线程通过既有线程安全桥回到所属事件循环，不共享Session或创建跨循环Engine。保留本次返回版本与已有完整读回，当前对象仍匹配且原生线程、Body、client、借用FD和源stage真正收尾后才确认新增。现有`write_object`／`_read`返回值、guard顺序、许可和取消排空保持，不从业务成功推导物理事实。

独立媒体client沿已有同源限制和保存目标；仅记账写入显式选择单次提交，schema1沿已有独占单次client，schema2默认A传递`single_attempt=True`，外部目标继续其现有单次提交client。不能让旧SDK丢应答后的自动重试412解释更早的未知提交。意图失败时对应PUT不开始；多对象部分失败、后续权限拒绝、业务回滚或取消仍保留已发生事实。账目发布异常保留pending／unknown，不改变已经取得的业务结果；发布期间取消排空后继续传播。无版本、null、应答丢失、内容／版本替换或关闭不明均不冒充已确认变化。

GET摘要增加该生产者，页面仅在响应声明时解释独立导出媒体受控新增，`coverage=partial`和推算总量null保持。ExportOutput ZIP仍使用现有无返回版本的`upload_file`，本片不为遥测改造上传算法，不把ZIP、清理／退休、业务行删除或外部变化算作已确认新增／移除；完整export用途及全平台增量均未完成。

准确范围：后端共享Recorder、`export_media_storage.py`、`export_media_transfer.py`、`schemas/storage.py`及相关导出媒体／记账测试；Web `StorageUsageAccounting.tsx`及测试；OpenAPI／本树类型（如有差异）、SDK合同；README、CHANGELOG、存储用户／API／开发说明、本计划与执行队列。后端在当前HEAD隔离树实现，主树负责Web、正式文档、集成及人工环境。无迁移、公开端点／任务参数、配置、依赖、镜像或生成算法变化。

验证只用核实owner的可销毁资源：准确保存位置、逐对象意图与字节、schema1／2及默认A、真实生产`copy_source`与小型PostgreSQL／MinIO链路；版本／完整读回／关闭不明、丢应答后独立412、业务guard失败、多对象部分成功、取消排空及账目发布失败。复用相关旧回归，不重新资格化每种导出格式或消费者，不加规模、GPU、模型、Tracker、版本GC或全恢复门槛。固定Ruff、OpenAPI／codegen、Web类型／lint／CSS tokens、SDK、正式文档及正常提交钩子后，检查真实摘要文案、明暗／宽窄屏与请求／控制台。

集成前核实人工原3100／8100、0254、Worker／Beat的归属和空闲状态；刷新实际执行导出媒体的所属Worker，保留人工业务及原对象／文件，不重建后端镜像、不重新迁移或改周期配置。按owner清理本片数据库、Redis、九桶、私密浏览器／探针／测试输出和已集成工作树。E12及整套Epic继续活动，未验证的能力明确保留，不推送或发布。

## E12-B6 Outcome：独立导出媒体受控新增增量

后端`5fd1f86bf`与本树Web、正式文档及本Outcome同次集成交付。共享Recorder保留媒体缓存默认purpose，独立导出媒体显式使用`temporary`及`ExportMediaBundle.id`；共享复制入口逐实际条件PUT持久化准确保存地址和独立尝试，保留返回版本、完整读回与当前HEAD，原生线程、client、Body、借用FD、源stage和许可收尾后发布。后续guard拒绝、业务回滚或部分失败不抹去物理事实，发布失败保留pending，取消先排空再传播；schema2默认A记账写入显式单次提交。没有迁移、公开任务参数、配置、依赖或镜像变化。

最终395项不同后端用例中366通过、29失败、0跳过，18项新增回归全部通过。原传输58、视频传输8、物理存储73、共享记账24与bundle87项通过；两个未修改领域查询替身套件的29项失败均为`KeyError('id_1')`，在修改前`b6a256855`源码实际重跑127项，失败testcase集合逐项完全相同，源头为`tests/test_export_media_bundles.py:126`固定读取INSERT参数。未将这些旧测试问题扩成产品修复或新增前置；开发中仅校正测试的既有传输选项接缝、异常类型和配置字段。固定Ruff、OpenAPI快照核对及正常提交钩子通过，快照重导无跟踪差异。主树36项Web、4项SDK合同、codegen、类型、完整Web lint／CSS tokens、207项frontmatter／209页导航／208篇可读性及标准文档构建通过；4项已有ESLint与2项已有样式层级提醒保留，远端CI未运行。

真实PostgreSQL／MinIO通过生产`copy_source`的schema1及schema2默认A两例，各核对一份对象的准确owner／完整key、SHA、大小、不可变当前版本及增量。业务guard和permit使用受控Runtime替身；证据不代表完整导出Job、真实外部B、逐格式、GPU或规模资格。ZIP仍沿旧`upload_file`且无可关联返回版本，未改上传算法；删除、退休和GC未接移除增量，整个export用途及全平台覆盖仍不完整。

人工环境保留0254和原3100／8100。核实所属服务及两个Worker空闲后只刷新原监督服务一次，恢复绑定Beat；两个新Worker均注册73项任务且无活动／预留／定时任务。初次启动观测为未就绪，首次浏览器连接被拒绝；等待同一服务后Web／API、doctor及Beat恢复健康，没有重复重启或重建镜像。真实GET返回七个生产者，覆盖保持partial、推算总量null。1440×1000／390×844明暗四组合的截图、11px弱化说明、13px正文和折叠交互通过，无横向溢出、页面异常、控制台错误或HTTP错误；8条无状态请求保留未知，不计成功，仅无关在线心跳本地应答。周期保持关闭，未修改账号偏好或业务配置。174表9,235行、24条对象版本／删除标记、0项multipart与16个保留媒体文件的完整指纹均保持。

所属`23278de448839af0:test`数据库、Redis和九桶已销毁，主树独立确认资源及运行清单缺席。35个子树缓存目录、15份日志／XML／doctor输出、基线源码快照及主树私密登录状态、截图、探针、原始检查输出和新生成文档输出按归属清理，仅保留有界摘要；已集成干净子树及临时分支清理。README、CHANGELOG和存储用户／API／开发说明同步。本片不追加训练测量、Tracker、全消费者或全恢复条件；完整E12与整套Epic继续活动，未推送或发布。

## 本轮定稿：E12-B5 媒体缓存受控新增增量

2026-10-25，基于`eb778b2cf`继续原始E12-S03／AC01、§5及工程任务第2项。C3已交付六分类LIST快照，B3／B4已接入临时输入及集中托管原件；本片统一补齐media-cache四类既有生产者的实际新增，不重新建设分类盘点。沿用户整套实施／提交授权执行，E02-AC10待答的历史定位范围不阻塞本片。

| 已有owner             | 生产者标识                 | 接入范围                                        |
| --------------------- | -------------------------- | ----------------------------------------------- |
| ThumbnailOutput       | `thumbnail_outputs`        | 目录预览、Dataset与Task普通缩略图的共享写入入口 |
| OriginalCacheEntry    | `original_cache`           | 完整原图缓存的条件PUT及现有完整校验             |
| ImagePyramidOutput    | `image_pyramid_outputs`    | 全部瓦片、overview与manifest                    |
| VideoDerivativeOutput | `video_derivative_outputs` | poster、playback、frame、chunk及metadata结果    |

最小方案是四处分别追加字节累计；这会遗漏真实写入后业务回滚、多对象中途失败和应答未知。因此复用B4的`begin_in_transaction`／`publish`和B3的窗口对账，用一个私有媒体记账helper承接四处实际写入。每次实际条件提交前，在同一调用者Engine的独立短事务持久化精确endpoint／bucket／完整key、producer、owner与实际尝试意图；不从今日Binding猜测旧地址。重试实际PUT产生独立attempt，完整逻辑身份继续幂等，412只表达本次零增量，不能解释早先未知尝试。

仅在条件PUT成功、返回不可变非null版本、完整内容读回与该返回版本一致、当前HEAD仍是同一版本和大小且原生资源真实收尾后确认正增量。优先复用现有完整读回结果，新增当前HEAD仅用于账目，不改变原有读写结果或扩大签名范围。无版本／null、返回旧版本、后来替换、丢应答、完整性或关闭失败保留已观察物理事实与unknown；未知不是零。后续业务发布失败、取消或回滚不抹去已发生写入；部分生成失败时，已经提交的对象仍有独立意图及可证明事实，不按整个owner成功才一次累计。意图持久化失败时不开始对应PUT，发布失败不改变已经发生的业务结果，沿现有恢复／盘点核对未决意图，不加新重试或清理许可。

同步物理helper通过显式传入的局部记账对象把短事务提交回所属事件循环，不共享AsyncSession到线程、不创建跨循环Engine、不包办读取或替换全局S3 client。复用现有线程排空、BaseException／取消和client／Body收尾；同步桥接采用[Python线程安全协程提交](https://docs.python.org/3/library/asyncio-task.html#asyncio.run_coroutine_threadsafe)的既有仓库模式。条件写只针对当前版本，沿[S3条件写合同](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)，不把历史版本或提供方磁盘字节纳入当前对象增量。

旧默认目标的共享SDK client会自动重试，首次提交丢应答后重试412不能记作唯一一次零增量。因此仅在这些记账写入中显式选择独占单次提交client，schema1和schema2默认A都覆盖；普通读取及其他用途仍沿原工厂。该client在既有owner范围内真实关闭，不替换共享实例。记账意图失败发生在PUT之前，必须保留已知未开始语义，不能误标未知物理I/O并让缩略图永久滞留；发布阶段取消要先排空记账和已有owner检查点，再继续抛出取消。

```text
saved media owner -> per-object durable intent -> existing conditional PUT / full readback
                             |                               |
                             |                  returned version + current HEAD
                             v                               v
                    existing mutation table <- true native close/drain
                             |
                     existing LIST reconciliation -> partial summary
```

GET摘要的producers列表增加上表四项，覆盖文案只显示响应实际声明的生产者；`coverage=partial`和`projected_total_bytes=null`保留。现有缓存、金字塔与视频清理按key删除，并不证明不可变版本已移除；本片不改删除算法、不补造负量、不扩展版本GC或为已有清理增加前置。历史、其他生产者、提供方lifecycle及外部写入继续由既有盘点表达未解释变化。无需新表、迁移、API端点、命令、配置、依赖或镜像。

准确候选范围超过8文件：新`apps/api/app/services/media_cache_accounting.py`及其专项测试，现有`storage_usage_accounting.py`仅按共享接口需要调整；四个物理接点`thumbnail_outputs.py`、`original_cache.py`、`image_pyramid_storage.py`、`video_derivative_storage.py`，`managed_s3_target.py`的独占单次client选项及`video_derivative_objects.py`的窄参数传递，两个所属Worker `image_pyramid.py`／`media_derivatives.py`，`schemas/storage.py`及相关已有media-cache／accounting检查；Web `pages/Storage/StorageUsageAccounting.tsx`及其测试；OpenAPI snapshot与本树生成类型；README、CHANGELOG、用户storage-connections、API storage-locations、开发reference/storage-buckets、本计划及执行队列。原件／解码／渲染算法、消费者业务、清理控制和SDK能力分类不变；新增文件必须服务同一记账接点，不顺带整理其他模块。

使用核实owner的一次性test／e2e验证：四个生产者的准确地址与真实字节、schema1／2及旧位置、条件失败与重复实际尝试、无版本／旧版本／替换、应答与读回失败、多对象部分成功、业务回滚、取消和真实关闭顺序、记账发布失败及盘点对账；有意义的回归覆盖实际生产调用者，不只验证私有helper。真实PostgreSQL／MinIO以小图片、少量瓦片和小视频产物验证存储接线，原生生成器不可用时明确限定证据，不增设训练规模、GPU或渲染性能资格。同步快照／类型、SDK合同、Ruff固定版本、Web类型／lint／CSS tokens、正式文档与构建、正常提交钩子及diff检查。浏览器检查真实摘要文案、明暗和桌面／窄屏，不重跑原周期计划与历史消费者验收。

主树集成后核对人工环境所属服务及空闲任务，再刷新实际消费改动的Worker，保持原3100／8100、Beat与schema0254。记录数据／对象保持与新生产者接线证据，按owner销毁全部本片一次性资源和私密中间产物，清理已合入隔离树。回退应用保留0246账目事实，不删除对象或历史流水。此片只交付该生产者组受控新增，不宣称完整E12或整套Epic完成，不推送或发布。

## E12-B5 Outcome：四类媒体缓存受控新增增量

后端`cc3eae1da`与本树UI、正式文档及本Outcome同次集成交付。共享Recorder／Attempt将四类生产者的每次实际条件PUT与独立持久意图、返回版本、完整读回和当前HEAD关联，原生线程／Body／文件／client收尾后才发布确认增量。部分生成失败、业务回滚或取消仍保留已发生事实；未知不会由后来的412或owner终态改写。schema1／2默认A的记账写入使用独占单次client，普通读取、共享client、清理算法与权限保持；意图拒绝不冒充未知物理I/O，发布期间取消会排空记账及缩略图回执检查点后继续传递。无迁移、依赖、公开任务参数或配置变化。

最终按JUnit testcase的classname＋name去重，367项后端检查和2项OpenAPI检查均通过，含26项新回归；重跑及fixture统计不重复累计。开发期间修正新测试错误的对账字段、与既有能力链接不符的URL断言及覆盖掉原静态图片方法的客户端替身；未为通过测试放松产品合同，也未将未单独复现的旧断言失败标为已复现基线。固定Ruff、正常提交钩子及diff检查通过，OpenAPI重导无跟踪差异。主树35项Web、4项SDK合同、codegen、API快照核对、类型检查、完整Web lint／CSS tokens、207项frontmatter／209页导航／208篇可读性和标准文档构建通过；保留4项已有ESLint及2项已有样式层级提醒，远端CI未运行。

真实PostgreSQL／MinIO四组均为默认A的schema2保存地址：缩略图1、生产OriginalCache.store 1、金字塔3、视频派生2个对象，分别核对当前字节、不可变版本与增量。金字塔经过真实\_build_generation至上传／读回，只有本地编码使用预生成文件；现有原生Video Worker的metadata／frame／chunk生成也已核对完整inventory与Mutation。未验证真实外部B或libvips编码，不据此声称规模、GPU、迁移或全平台增量完成。

人工0254保留原3100／8100；刷新前后两个所属Worker均无活动／预留／定时任务，各注册73项任务。按原监督服务刷新实际消费者，绑定Beat恢复健康，进程已变化；启动首次观测为starting，等待同一所属服务就绪后Web／API及doctor全部健康，未重复重启。真实API返回六个生产者，覆盖仍partial、推算总量null。浏览器检查1440×1000／390×844明暗四组合，完整生产者文案、11px弱化提示、折叠及原周期关闭状态正确，无横向溢出、页面异常或HTTP错误；4条无状态请求不计成功，仅无关在线心跳本地应答。未修改周期、账号偏好或其他业务。174表9,235行、24条对象版本／删除标记与16个本地媒体文件的完整指纹保持。

所属`b56b7de6b25601d3:test`数据库、Redis和九桶已销毁，并由主树独立只读确认不存在；资源及运行清单缺席。子树36个缓存目录、19份日志／XML，以及主树私密登录状态、截图、探针、原始检查输出和新生成文档缓存均按归属清理；仅保留有界摘要，已集成干净子树及临时分支已清理。正式说明同步README、CHANGELOG、用户storage-connections、API storage-locations及开发reference/storage-buckets。本片只补媒体受控新增，原按key清理不补造负量，不新增版本GC、训练测量或其他消费者验收；完整E12与整套Epic继续活动，未推送或发布。

## 当前推进边界：停止万条辅助测量

2026-10-25，按用户对本轮数据集／存储主线的纠偏，停止C6万条训练准备测量，保留已完成的千条辅助记录。万条尚在夹具准备阶段，没有完成数据，不标为通过；所属进程结束，test资源已销毁。它不再列为本轮必需交付，也不作为B8、B3、B4或其他存储修改合入／人工环境刷新的前置。下列合同中实施期间的C6源码冻结叙述仅保留历史上下文，现已解除；后续规模专项需有明确独立目标才启动，不从本轮主线自动展开。

## 主树集成与人工环境

2026-10-25，E12-A4最终来源`e2744a6cc`已通过正常合入`9521704a0`集成，24个非Markdown文件与已验收隔离树逐字节一致。人工环境先核实两个所属Worker空闲，再停止原监督服务，应用0249并恢复原3100／8100、两个Worker和绑定Beat；没有重建镜像。迁移仅增加空`storage_fault_states`表及观测序列，原有170表9,230行的逐表内容摘要、九桶24个版本／删除标记的完整字节摘要和12个源文件摘要全部保持。当前171表、9,230行。Web／API数据库健康、实际OpenAPI新GET及Vite交付控件源码通过；两个新Worker各注册73任务且准确队列不变，目录根、维护配置及Beat绑定保持。沿用本片130项后端通过、1项已复现基线失败、24项Web、4项SDK及实际恢复／明暗窄屏证据，不重复历史消费者验收。两个所属一次性mode的数据库、Redis及18桶已独立核验不存在；清理已合入任务树及私有中间产物，仅留有界结果。完整E12仍为部分交付。

2026-10-25，报告附件用途切换、B3当前对象账目与B4托管原件新增已集成到主树，人工dev应用0245／0246。56个非Markdown源文件与已验收最终隔离树逐字节一致；沿用各片已完成的测试、实际I/O及四组合浏览器结果，不因集成重复整套验收。迁移前后原有166表9,230行和九桶24个版本／删除标记的内容摘要保持。原3100／8100、两个Worker与绑定Beat按原常驻服务恢复，目录根和维护配置保持；健康及新API路由读取通过，两个Worker注册新代码，Beat已实际触发恢复任务。当前摘要包含临时输入与托管原件两个生产者，仍为partial、推算总量null，周期默认关闭。

下文隔离实施时“待合入”“根环境未变”及C6冻结条件均为历史记录；当前以上述集成状态和停止辅助测量的范围为准。合入不要求万条训练准备测量，不新增性能门槛或消费者改造。

## 本轮定稿：E12-A4 来源故障去重与恢复观测

2026-10-25，基于干净`637c2a280`复核原始E12-S04／AC04与§6“告警有去重、恢复事件与负责人”。A3已有四类本地来源故障审计，但每次失败均追加、没有持续状态或成功恢复。本片沿原整套实施／提交授权approved，补齐这四类观测的持久状态、重复计数、恢复转换和可见处置方；不将其当成其他用途切换的前置，也不据此勾完完整E12-S07。

最小方案是按现有AuditLog查询最后一条失败，但它不能证明恢复，也会受日志归档影响。因此增加一张稀疏`StorageFaultState`表与一条数据库观测序列，继续复用现有审计历史和来源诊断；健康文件不逐个建行。迁移0249接0248，不补造历史故障状态，旧审计保持；回退应用保留表和事实，显式降级只允许空新增表。此观测不授权读取、写入、自动暂停或删除。

### 去重、证据与晚到结果

统一接入现有`scan_snapshot`、直接源`reference_snapshot`、fenced扫描Worker及`directory_previews.generate → register_entry`四条链，不改扫描、原件、缓存或预览业务算法。稳定故障键保存安全摘要及UUID关联，不存路径、对象键、凭据、签名URL或异常原文；run／job／request只作证据，不按请求生成新故障。

| 类别           | 去重及恢复范围                                                                                               |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| 根不可用       | 同Location／Source、节点、冻结根anchor和实际配置；不能因换根、停用或注销变为恢复。                           |
| 认证／访问拒绝 | 同来源、操作及准确目标摘要；目录列举与文件读取分开，邻文件成功不能清除对象级拒绝。                           |
| 原件变化       | 同来源、path_digest与期望内容身份；已核验时用固定SHA，未核验时用固定观测摘要。新内容被采纳不冒充旧内容恢复。 |
| 预览失败       | 同来源、path_digest、Asset、原件SHA、SPEC及实际绑定／策略依赖；同一预览ready并最终提交后才恢复。             |

公共身份包含实际依赖的位置／来源配置、节点和维护代次；依赖连接、缓存绑定或策略时纳入相应已捕获事实。它们变化后保留旧观测为历史范围，不能直接标健康。恢复只来自相同范围的实际成功：读取需完整内容核验、最终权限／配置复核；扫描需原task／fence核验与对应目录结果提交；预览需原业务发布成功。原图缓存命中、16字节探针、邻资源、取消、旧投递或撤权不提供恢复证据。

每次实际I/O前从当前调用者Engine的独立有界连接取得单调序号，故障行原子接受较新观测，同序号重放不重复计数。成功只更新已经存在的匹配故障行；已恢复行保留水位，晚到旧结果不能抹去较新故障或重新打开已恢复状态。首次失败尚未持久化时并发成功可能没有故障行可更新，随后晚到的首次失败保守保持待复核，下一次同范围成功再恢复；“暂无故障记录”不代表实时健康。若观测库或旁路写入失败，保持原业务结果和A3的安全日志边界，不提交调用者事务、不重试物理I/O，也不锁调用者已持有的Location／Source父对象。

首次发生、恢复以及恢复后再次发生各写一次转换审计；持续失败仅更新接收观测次数、最近时间和安全关联。状态、转换审计同事务。旧A3日志保留，不从最后失败时间臆测当前健康；不新增外部通知渠道或阈值。

### 可见范围与界面

新增只读`GET /storage-locations/{location_id}/faults`，沿现有datasets:read、当前manager、位置可见性及source_scope_clause裁剪。项目管理员只见其可管理来源，孤立／已删除来源仅超级管理员可见；状态与全量计数使用同一可见范围。接受`state=active|recovered|all`、`limit=20`（1–100）、`offset`分页，返回匹配状态、首次／最近失败与恢复时间、接收次数及安全关联，不返回内部scope摘要。显示当前观测范围，旧配置与旧内容明确为历史，不能混同未恢复与当前实时健康。GET只读持久行，不触源、不发任务。

来源诊断复用紧凑字号、语义状态色和已有分页模式，增加故障观测区：未恢复／已恢复／全部、时间与重复次数、处理方、来源与现有任务链接；超级管理员可跳转转换审计。默认处理方为根／访问故障的“平台管理员”和内容／预览故障的“数据集管理员”，没有指定个人则明确显示，触发读取的人不自动变成负责人，也不把备份声明当故障认领。无新的认领、通知、重试或强制消警工作流。会话／角色／位置变化重建查询owner，撤权隐藏旧数据，保持现有居中详情及返回导航；无需新增独立运维页面。

```text
authorized source context -> bounded observation sequence -> existing physical I/O
                                              | failure / final validated success
                                              v
                     sparse fault state + transition AuditLog (one transaction)
                                              |
visible Location + source scope -> saved paged query -> existing source diagnostics
```

### 准确文件范围、验证与集成

本片跨模型、四条既有接点、API与管理面，超过八文件：新`apps/api/app/db/models/storage_fault.py`和`alembic/versions/0249_storage_fault_states.py`；现有模型注册、`services/storage_fault_events.py`，新`services/storage_fault_state.py`承接原子状态及查询；`dataset_content.py`、`dataset_references.py`、`directory_previews.py`、`dataset_scans.py`及`workers/dataset_scan.py`；`schemas/storage_location.py`和`api/v1/storage_locations.py`；新`tests/test_storage_fault_states.py`、`test_migration_0249_storage_fault_states.py`及现有A3、内容、引用、扫描／预览受影响断言；Web新`StorageFaultsPanel.tsx`及测试，现有`StorageLocationSourceSummary.tsx`、`api/storage.ts`和审计动作标签／测试；OpenAPI、生成路由、SDK web-only排除、本树生成类型；README、CHANGELOG、用户存储指南、API存储位置指南、开发存储说明、本计划及执行队列。没有依赖、镜像、服务、环境变量或Celery公开任务参数变化。若需要额外文件，仅限实际接点或生成契约，不延伸消费者业务。

使用隔离工作树、本地API虚拟环境及核实owner的可丢弃test／e2e；验证稳定去重、重放／逆序、已恢复后再发生、邻目标／缓存不消警、配置或内容变化不假恢复、真实事务／回滚、扫描fence与预览发布失败、观测失败保留业务，以及角色／Key／位置／来源裁剪、分页与GET零源I/O。仅跑新增和受影响回归，不重复历史消费者资格、规模或训练测量。生成OpenAPI／类型、4项SDK合同、Web类型／lint／CSS tokens、Ruff／格式、文档检查／构建及正常提交钩子、diff检查；锁／upsert使用[PostgreSQL既有事务锁](https://www.postgresql.org/docs/16/explicit-locking.html)和[SQLAlchemy PostgreSQL条件更新](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)，不引入新协调系统。

真实一次性目录重现重复根不可用，恢复同一根后完整读取及持久恢复；浏览器查看未恢复／已恢复与重复计数、刷新／分页、任务／审计链接，检查明暗×1440／390宽度、焦点、溢出、控制台及失败请求。检查通过后正常提交、合入；按原监督服务应用人工0249并刷新受影响API／Worker，保持3100／8100、Beat、原业务与文件内容，核对迁移前后摘要。按owner销毁测试资源及私有中间产物，清理已合入任务树，仅留有界结果。全类别健康、容量／配额告警、任务无进展、远端Agent和通知渠道仍按原始条件单独推进。

### E12-A4 后端实施检查点（历史）

`6c1da7345`固定稀疏状态、I/O前独立有界序号、原子转换审计、四条既有调用链及`StorageFaultOut / StorageFaultList`，供独立控件复用。该提交仅为源码检查点；后续实现、验收与清理结果以本文A4 Outcome为准，不能将检查点当成当时已经验收。

## 下一片定稿：E12-B4 托管原件集中写入增量

2026-10-25，基线 `5c95be25d`，继续原始 E12-S03／§5／工程任务第2项。B3已在隔离树交付通用账目表、分页对账和周期计划，feature为 `6ccae7415`＋`ab45ab907`；本片在该实现上接通现有托管原件集中写入链，一次覆盖Dataset direct／ZIP／connector／directory_copy、Task及辅助原件，不按导入消费者拆开新资格工程。C6尚在原进程测量，根应用继续冻结；本片不是E07、B8或C6的前置。

最小方案是事后汇总Creation的size；该值不能证明当前可见字节变化，也会丢失真实写入后业务发布失败的事实。因此复用B3的事件与对账，在已有Creation独立事务中登记最终写入意图，并在真实I/O收尾后尽力发布。无需新表、迁移、API端点、任务、配置、命令、依赖或服务。

### 共用意图与实际计量

从 `storage_usage_accounting.begin` 提取可被既有AsyncSession／AsyncConnection事务复用的显式地址入口，接收固定endpoint／bucket／完整key、producer、owner UUID、逻辑组／实际attempt和类别；PredictionInput旧入口保留为薄适配，既有幂等身份及逐实际PUT行为不变。创建按生产者／owner／实际attempt区分；不可变删除仍按物理scope／key／VersionId跨owner去重，不能因通用化退回按业务owner重复扣减。

`managed_original_creation._physical`在既有 prepared→writing 的独立事务中同时保存此最终提交尝试的账目意图；事务未成功就不调用writer.complete。复用同一引擎及Creation已有状态转换，不增加另一套业务责任、消费者门禁或异步任务。端点只取已验证冻结configuration.endpoint，桶／key／owner_kind／origin取该Creation，不从当前Binding猜地址。成功事务不跨原生I/O持锁；每个Creation只沿既有一次最终提交路径执行，重复读取verified结果不再记账。

写入仍由现有writer执行：空对象条件PUT，非空对象条件CompleteMultipart；准备中的parts／abort不属于当前可见对象字节，不作正负量。捕获真实写入回执、prepared大小、完整读回及额外当前HEAD证据，保留返回VersionId与物理结果。仅条件提交成功、完整SHA／大小核验、非空且非null不可变返回版本与当前HEAD相符，并完成原生收尾时记正增量；412为本次零。无版本／null、返回旧版本但当前已替换、应答不明、关闭失败及证据不足保持unknown；不能因为最终看见对象而推断本次创建。

无论Creation回执／读回／业务发布事务随后失败、调用者取消还是最终业务行回滚，已发生的物理事实由独立账目保留。真实I/O未收尾或进程在发布前消失时，已持久意图保留pending／unknown，不按时间消除。账目发布失败不改变已成功原件的业务结果；不新增读取门禁。沿用现有真实drain／close与维护permit次序，不为观察而提前释放资源。

Retirement与Reclamation目前不执行最终物理DELETE，不接负量，也不顺带实现回收。浏览器direct暂存、mask暂存、旧预签名重放、其他生产者和提供方lifecycle仍是未覆盖写删，不能在签发URL时加量。已有LIST分页对账继续负责未解释变化，平台推算总量仍为null；本片不补造历史流水、不宣称全平台账目完成。

```text
frozen Creation + existing prepared -> writing transaction
                   | atomic durable mutation intent
                   v
existing conditional PUT / CompleteMultipart -> receipt + full readback + current HEAD
                   | true native close/drain
                   v
independent accounting fact -> existing LIST-window reconciliation -> partial summary
```

### 展示、准确范围与验证

已有摘要的producers列表增加该实际生产者，coverage保持partial。现有存储折叠区按返回producers解释“临时输入写入／清理”和“托管原件受控新增”，不得把原件退役描述成物理移除；未接入与未知总量说明保留。只改覆盖文案和相关断言，不增加控件或重做工作台。

预计19个准确候选路径，超过8文件，以同一增量合同交付：API `services/storage_usage_accounting.py`、`services/managed_original_creation.py`、`schemas/storage.py`；现有 `tests/test_storage_usage_accounting.py`、`tests/test_managed_original_creation.py`、`tests/test_task_original_creation.py`、`tests/test_managed_original_creation_routes.py` 与新 `tests/test_managed_original_accounting.py`（既有夹具仅在受本次新事务影响时调整）；Web `StorageUsageAccounting.tsx`及其测试；用户存储连接指南、API存储位置指南、开发storage-buckets说明、README、CHANGELOG、本计划与执行队列；OpenAPI快照和生成路由文档仅在实际生成差异时纳入。无其他调用者、writer协议、Worker或SDK接口改动；当前树生成类型仍本地生成。

修改代理从合同提交后的当前根HEAD创建独立树，仅在该树应用已验收B8及B3依赖；不得在根树C6终态前合入应用。验证通用入口不改变B3幂等行为，Creation意图与writing原子提交、条件空PUT／MPU成功和412、丢应答、持久化／业务发布失败、取消后真实收尾、精确旧版本与当前替换、无版本保持未知、verified重读不重复；同一集中owner覆盖Dataset／Task／辅助及各origin的固定地址，不重新资格化导入消费者。实际一次性PostgreSQL／MinIO核对已确认原件与账目、LIST前后变化及原件关系保持，测试数据仅在已核验可销毁的test/e2e空间建立。

覆盖文案在真实浏览器明暗／桌面／窄屏查看，复用B3已完成的周期控件和撤权验收，不重复其资格。完成相关API／Web、格式、文档构建、正常提交钩子与diff检查；精确销毁所属资源、清理日志／截图／缓存，仅保留有界摘要及待集成Git树。人工3100／8100、两Worker／Beat和C6原session27154保持。回退保留0246事实表，停用新增接线与说明；不反向删除原件或历史事实。原始其他生产者、外部写入与真实部署测量仍独立推进。

### E12-B4 Outcome：托管原件集中写入增量

2026-10-25，本地隔离分项完成。通用显式地址入口支持调用方 AsyncSession／AsyncConnection 事务，保留 PredictionInput 原有创建身份及跨 owner 的准确不可变 DELETE 去重。Creation 在原 prepared→writing 事务原子保存最终尝试意图，固定冻结 endpoint／bucket／完整 key、owner_kind 与 origin；继续使用原 writer、原 permit 和真实 drain／close。只有条件提交成功、完整 SHA／大小读回、返回的不可变非 null 版本仍为当前版本且原生收尾完成才确认新增；412 只确认本次零，其他不足证据保持 unknown。回执／读回／业务持久化失败或调用者取消不抹去已取得的物理事实，账目发布失败仍保留 pending 且不改变成功业务结果。

相关后端共218个不同本地用例、Web33项通过，覆盖事务原子性、空PUT／MPU、412、丢应答、持久化／业务回滚、重复取消及真实关闭、读回前后替换、null／无版本、verified重读和Dataset四origin／Task／辅助的固定地址。通用 PredictionInput／storage_usage 回归保持，独立只读审查无新增缺陷。标准文档构建、frontmatter／导航／可读性／codegen、OpenAPI无漂移、Ruff／格式、CSS tokens及diff检查通过；提交走正常钩子。

实际一次性PostgreSQL／MinIO启用所属桶版本控制，空对象与非空MPU创建2个原件，完整读回SHA／大小、Creation与AssetLocation关系通过；LIST由0到2对象、28 B，确认流水2条／28 B，未解释字节和不确定对象均为0，verified重读无重复流水。真实MinIO实测限本地集中写入正向与LIST核对；故障／取消窗口由真实SQL加受控原生writer测试覆盖，不宣称生产或各导入器重新资格化。浏览器1440×1000／390×844明暗四组合查看实际摘要，生产者文案与退役／未知总量说明一致，无横向溢出、页面／控制台错误或失败请求；周期计划复用B3既有验收。

原件退役／Reclamation仍无最终物理DELETE，浏览器暂存、旧预签名重放、其他生产者及外部写入未接入，coverage仍partial、projected_total_bytes仍null，完整E12-S03继续。所属test／Redis／九桶、浏览器和临时产物按归属清理后保留待集成Git树；根3100／8100、两个Worker／Beat、C6同session27154与根应用源码保持。C6终态前不合入根应用。回退只撤本片接线与说明，保留0246历史事实及原件；不新增发布版本或验收前置。

## 本轮定稿：E12-B3 当前对象增量账目与周期对账

2026-10-25，基线 `587a0c9cd`，落实原始 E12-S03／§5／工程任务第2项。最小路径是保留现有 LIST 快照、只追加写删流水；本片在此基础上复用已有分页任务完成对象级对账及显式周期计划，使账目能被实际核对。首个完整生产者限 `prediction_inputs`；其他用途未接入时明确为部分覆盖，不能勾完全部工程任务，也不能据此新增消费者资格或清理许可。它不是附件切换、训练基准或 E07 迁移清单的前置。

### 计量及事件合同

- 唯一计量域为指定 endpoint／bucket 当前可见对象的内容字节，与已有 `ListObjectsV2` 一致；历史版本、删除标记、multipart 和提供方磁盘开销均不混入。保存精确地址、版本、大小及时间窗口，版本实际写删事实与 nullable 的当前对象字节增量分开记录。S3 key 不作 URL 解码或文件系统归一化，ETag 不是内容摘要。
- 在已有独立 owner 事务中，物理 I/O 前持久化意图；写入以 lease／relative／write_nonce 保存稳定逻辑组，每次实际条件 PUT 在 I/O 前独立登记尝试窗口及幂等回执；412 不重复增加，业务发布失败后实际创建的新版本独立保存，旧未知尝试不被覆盖。删除按准确 endpoint／bucket／key／不可变 VersionId 去重，不按 owner 声明次数扣减。没有业务级联删除，重复投递／部分失败／重试不重复累计。已有 reservation、业务撤销及失去引用不产生实际字节减少。
- 新写入仅在条件创建实际成功、完整内容读回且当前 HEAD 确认同一返回版本时记录正增量。412 复用不再次增加；仅固定旧版本可读、超时／提交应答不明或只看到最终存在，不能反推本次创建。条件写只针对当前版本，见 [S3 条件写合同](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)。
- 删除保留已有保存版本／完整校验和所有权规则。只有确实移除该版本、删除前确认它是当前对象且大小已知、删除后确认当前内容不存在，才记负增量；暴露旧版本、删除标记、替换、并发和无法确认均保持 unknown。不依赖提供方一定支持条件 DELETE，也不因 key 看似唯一而推断减量。
- 每个对象的实际结果独立保留，后续对象删除失败不能抹掉先前成功事实。复用现有 `settled_io` 的真实收尾：同步 I/O 只收集有界回执，由原异步 owner 在收尾时用独立事务发布；取消和业务发布失败仍记录已发生的物理事实。进程在提交前消失或账目发布失败，持久意图保留 pending／unknown，不能按经过时间改成成功或零。账目发布失败不把已成功的业务结果改为失败，不增加读取或生命周期门禁。

### 快照和对账合同

新增 `StorageUsageMutation`、`StorageUsageInventoryObject`、`StorageUsageReconciliation`，以及下述 `StorageUsageSchedule`；统一在现有 storage_usage 模型文件注册。迁移 `0246_storage_usage_accounting.py` 接在已验收附件迁移 0245 后，无历史增量补造。表与索引以精确地址／幂等身份／时间窗口为界；旧应用可在保留 schema 的条件下回退，显式降级仅允许空新增表，禁止静默丢弃流水。

现有盘点每页同时暂存有界对象观测，稳定代次使用本轮 AsyncJob ID，不能使用每个执行块都递增的 fence generation。每页最多1,000对象，复用10,000页及24小时预算；重复 key、地址碰撞或无效页不得冒充完整新基线。只有整桶成功，才发布该桶的新对象基线和对账结果；失败／取消／失效 fence 保留前次成功结果。仅保留必要的两个成功代次及当前暂存，按归属清理被 fence 的旧暂存，不全量载入对象或流水。

按精确对象比较前后观测，与完全落在其观测间隔内的已确认增量核对；跨越页观测窗口、pending／unknown、首次无基线和版本歧义单列不确定。缺失对象只能在完整桶扫描后认定缺失。未覆盖用途或外部写入造成的差额称“未解释变化”，不能宣称账目损坏、精确物理容量或有权删除；部分覆盖时全平台增量推算总量保持 null。GET 仍只读数据库中的固定大小汇总，不访问对象源、不派任务、不返回逐 key 清单、地址或凭据。

### 周期计划和展示

`BucketsResponse` 增加可空账目／最近对账摘要与周期计划摘要，旧客户端字段不变。新增超级管理员全权 `PUT /api/v1/storage/usage-schedule`，请求为 `enabled` 和 `interval_hours`（1–168，默认24）；默认关闭，不增加环境变量或服务。开启时保存该操作者／API Key 的现有授权证明及精确配置范围，首次到期为当前时间；现有30秒 recover 任务兼任到期调度，沿用 AsyncJob、dispatch、checkpoint 和 fence，不新建另一套任务系统。活动盘点期间不重叠启动；配置或权限撤销使计划暂停并显示原因，必须显式重新授权，不能随意挑选其他管理员身份恢复。关闭计划不暗中取消已运行任务。

存储页沿用当前紧凑字号、`Modal`／语义色及会话撤权保护，在可展开的“增量账目与周期对账”内展示已确认变化、未知／部分覆盖、最近对账结果与周期开关／间隔。标签在控件上方、提示在下方，默认24小时不写进选项说明；普通查看不自动启用计划。新端点按现有 SDK web-only 排除，不虚报 SDK 已覆盖。

### 实施、验证及回退

本片数据流保持计量、业务许可和实际对象分离：

```text
prediction_inputs -> durable intent -> native I/O + bounded evidence
                                          | true drain / close
                                          v
                                  independent mutation ledger

LIST pages -> staged object observations -> complete bucket baseline
                                               + mutation windows
                                               -> reconciliation summary

persisted summaries -> existing storage GET -> existing panel disclosure
explicit schedule -> existing 30s recover -> existing AsyncJob / dispatch
```

本片跨模型／服务／Worker／API／Web／正式文档，超过8个文件，准确范围为：`apps/api/app/db/models/storage_usage.py`、模型 `__init__.py`、迁移0246、新 `services/storage_usage_accounting.py`、现有 `services/prediction_inputs.py` 与 `services/storage_usage.py`、`workers/storage_usage.py`、`schemas/storage.py`、`api/v1/storage.py`；新增账目、prediction-input账目、周期计划及0246迁移专项测试，必要调整既有 storage_usage／prediction_inputs／prediction_input_lifecycle 测试；Web `api/storage.ts`、`hooks/useStorage.ts`、`StorageUsagePanel.tsx` 及其测试、新 `StorageUsageAccounting.tsx` 及其测试；OpenAPI快照／路由生成文档／当前树生成类型和 SDK coverage；用户存储指南、API存储位置指南、开发桶说明、README、CHANGELOG、本计划与执行队列。无依赖、镜像、Executor、Tracker、会话或审核改动；若实际必须增添路径，先将具体原因回报主代理核定。

使用基于当前本地 HEAD 的隔离修改树，并仅在该树应用已验收 B8 依赖，主树 C6 未终态前不合入任何应用源码。验证意图先于 I/O、幂等重投、412复用、业务回滚、提交应答不明、取消／丢进程、部分删除和未知不减量；实际 PostgreSQL／MinIO 验证精确版本、当前版本暴露、同 key 替换以及完整盘点前后账实核对。周期计划覆盖真实 Worker／Beat 到期、活动任务不重叠、权限／配置撤销和恢复；查询保持零源 I/O，旧快照、失败／取消与 fence 回归有据。Web 用例及实际明暗／桌面／窄屏截图验证新控件和撤权、无溢出／新增控制台错误；同步 API／SDK 合同、代码及文档检查。一次性 test／e2e 资源和中间产物按归属清理，保留3100／8100人工环境。

回退先关闭周期计划，再切回保留新增 schema 的旧应用；关闭新账目展示不改物理字节，旧快照仍可读取。事实覆盖不足如实报告，不通过修复其他消费者或重做历史资格来扩大本片验收。其他生产者的增量接入仍属于原始第2项剩余工作，按具体物理写删入口推进。

## 本轮定稿：E12-C6 训练准备与重复传输资源基线

2026-10-25，基线 `454ffe6f8`，对应原始E12-S05／§7，并为E10-S05已有“重复传输成为实测瓶颈后才启动节点缓存”的条件提供事实。E10-A／B现有冻结、来源解释和实际加载器对账不重复建设；现用Executor镜像未升级的限制独立保留，不新增GPU、模型、Tracker、会话或审核资格工程。

本片在业务为空、完整归属核验的所属test环境，用1,000／10,000个真实编码、已登记托管身份及标注的合成图片任务测量。夹具物理对象写入及身份登记在计时外完成，不能用假索引或伪造内容回执代替可读原件；保留三种图片尺寸和中文文件名，不声称已验证目录／跨站点训练输入规模。经过实际JWT HTTP预检、同摘要创建和普通账号真实Celery构建至ready；分别计预检、创建请求、Worker物化／打包和最终完整包读取，汇总SQL、CPU／RSS、实际S3正文读取、临时目录峰值及收尾。两次完整包下载直连本机S3签名地址，核对同SHA和实际传输，不把包重复读取成本冒充全部训练运行耗时或缓存收益。

观测包装仅存在于独立API／Worker bootstrap，不替换业务返回值或生产依赖。按Task／split／样本图片和标签摘要验证最终manifest与真实ZIP成员，原Item／Task／Annotation和原件字节保持；预算、源变化或授权拒绝如实记录为未完成，不为通过修改生产时限／样本数上限。工具绑定自身及测量调用方源码指纹，C5原进程和六份冻结工具不变；不同模式的共享主机竞争与合成暖存储限制明确记录。

准确文件为`scripts/benchmark_dataset_training.py`（归属／夹具／HTTP及Worker协调）、`scripts/test_benchmark_dataset_training.py`、`scripts/dataset_training_benchmark_runtime.py`（独立观测bootstrap）、`scripts/test_dataset_training_benchmark_runtime.py`、`docs-site/dev/reference/dataset-training-benchmark.md`，以及DEV、本计划、E10计划和执行队列。另在既有原图读取参考页补充站内入口，共十文件，跨脚本／正式文档，以同一测量合同交付；无应用源码、公开API、表、迁移、依赖、环境变量、镜像或发布版本变更。独立修改代理只负责bootstrap及其测试，主树负责夹具、协调和文档。

先做有意义的工具检查，再跑真实PostgreSQL／Redis／MinIO／Celery／TCP测量；记录明确终态、可复现结果与当前瓶颈，不自动开缓存或优化工程。工具关闭所属子进程与临时目录，启动器资源随后按精确confirmation销毁并独立复查；保留人工3100／8100、两Worker／Beat及C5所属e2e，不重复无关浏览器验收。

## 本轮定稿：E12-C5 本地增量同步资源基线

2026-10-25 基于 `517e68867` 对照原始 E12-S05／§7。C1仅测元信息扫描，C4仅测HTTP原图读取，E04的同步功能验收也未提供整轮规模资源记录。本片测已实际采纳的本地reference来源，在既有预算内分别运行1,000／10,000文件的无变化同步和少量纯新增自动采纳；计时覆盖调度推进、扫描、完整预检以及最终idle，不在scan终态提前结束。

复用现有一次性环境归属守卫、普通运行账号Worker和扫描观测bootstrap，仅增加观测与可复现工具，不修改业务依赖、同步算法、授权、预算或发布门槛。汇总整轮全部Celery分块的SQL／执行时间、CPU／采样RSS、目录／stat与源读取字节，单列已有文件重复读取；夹具建立和显式阶段唤醒排除真实15分钟周期等待，不能将结果当作生产定时调度SLO。原件字节、旧Item／Asset及采纳头保持，新条目数与实际来源准确对账。

先运行工具辅助检查，再完成实际PostgreSQL／Redis／Celery／目录测量。同步正式开发参考、DEV与执行队列，保留人工3100／8100、两个Worker／Beat；结束后清理所属资源及中间产物。测得瓶颈如实记录，不自动追加优化或Tracker／会话／审核专项；训练准备及真实部署测量仍按原始要求另行推进。

## 本轮定稿：E12-C4 目录原图读取资源基线

2026-10-25 基于 `e038231cc` 复核原始 E12-S05 与性能验证计划。C1 已测目录遍历，E08 已测索引列表与预览请求；二者均未测真实原图的完整核验、传输及临时空间。本片只新增可复现测量工具与报告，不修改生产读取、预算、授权、缓存、API、Worker 或依赖，不引入新的性能达标前置。

在完整归属核验且业务为空的一次性 e2e 环境，复用现有真实扫描／严格登记服务准备三张图片：1024×768 PNG、1920×1080 JPEG、4096×2048 PNG，保存实际 SHA／大小。通过独立普通运行账号 API 和真实 TCP 调用现有 `dataset-items/{id}/content`，每档记录首次请求及单路／四路各20次完整读取；逐次核对 SHA、长度及响应头。只测本地目录原图、无持久缓存和暖文件系统，不冒充十万张媒体全量读取、冷盘、S3／SFTP、跨站点或生产 SLO。

观测 bootstrap 仅包装已有文件读取、解码与 SQL 计时，按测量请求隔离，不替换业务依赖或返回值。记录到响应头／完整响应的 p50／p95、实际源读取字节与次数、SQL 数及时间、API CPU 和采样 RSS、源 FD 与匿名临时文件的峰值／收尾；注明采样可能漏掉瞬时峰值。另在最大文件完整核验后的响应传输中主动断开，记录临时资源收尾时间及后续可用性，不将其写成读取前或读取中取消证明。超时仅使本轮证据未完成，不自动重启重复执行。

新增 `scripts/benchmark_dataset_read.py`、`scripts/dataset_read_benchmark_api.py`、`scripts/test_benchmark_dataset_read.py` 与 `docs-site/dev/reference/dataset-read-benchmark.md`；同步目录扫描参考页、DEV、本计划与整套队列。复用已有工作树资源／环境守卫，不新增资源治理机制。工具辅助测试、实际 HTTP 测量、Ruff／格式和文档检查完成后按归属清理。无产品界面变化，不重复浏览器矩阵；人工3100／8100、两个Worker／Beat及原数据保持。

本片已完成最终本地测量；结果与边界见下方 [C4 Outcome](#e12-c4目录原图读取资源基线)。

## 本轮定稿：E12-C3 平台当前对象分项计量

2026-10-25 基于 `78ba9cfdb` 核对原始 E12-S03／AC01 与 E09-S04。既有盘点按 endpoint／bucket 去重并保存总字节，但 LIST 页丢弃 Key，桶的多个用途标签不能作为互斥分类。本片补齐托管原件、预览／缓存、训练版本文件、临时产物、其他平台对象及未归类六项；外部源索引继续独立展示，不与平台保存量相加。不修改消费者、Tracker、会话、审核、模型协议或清理授权。

本片已实现并完成本地验收，交付证据见下方 [C3 Outcome](#e12-c3平台当前对象分项计量)。

复用现有 Inventory／AsyncJob、分页预算、检查点和成功桶快照。每页最多 1,000 个当前对象，保留准确 Key／Size；以有界数据库查询匹配已有归属记录，再按 `(endpoint,bucket,key)` 只计一次。同分类的多份归属申报合并，跨分类冲突列入未归类。路径形状仅用来定位候选数据库记录，不能凭桶名或文件名直接决定分类；固定引用复用现有纯解析／校验 helper。没有可信归属的历史、残留及目录标记仍计入实际盘点总量并单列未归类，不要求先迁移所有消费者。

原件覆盖 Creation 最终地址、AssetLocation 与业务删除后的 Retirement；Task／辅助原件复用其独立 Creation。预览／缓存覆盖 SourcePreview、OriginalCache、Pyramid／Video Output 和训练预览。训练版本按已有版本整体前缀及删除意图计量，包含准备中与保留的真实文件，不能以 ZIP 的 size_bytes 或 manifest 样本大小代替总占用。暂存包括 Creation 的独立上传暂存地址、PredictionInput、ExportOutput／ExportMediaBundle。头像、附件、审计、持久 RLE、训练实验及分析输出归其他平台对象。已过期、正在退休或业务已删除的记录不表示物理字节已消失；分类不授予可用性或删除资格。

大小只取本轮 LIST 实际返回值，不累计预约／计划大小。分类字典随检查点提交，桶完整结束后与总数、总字节、采样时间一起发布；失败／取消保留上次成功结果。旧快照没有分类时显示未统计；旧检查点不拼凑分类完整性，下一次完整盘点自然补齐。JSON 分类证据绑定同次测量时间、对象数和总字节，避免回退旧 Worker 后混用新旧统计。GET 仍只读快照，返回采样范围、覆盖、时间、去重依据及未归类／冲突数量；不返回对象键或归属明细。

在现有 Snapshot 增加可空分类 JSON，必要的准确地址查询复用／补索引；不新建对象注册中心或生命周期。前端复用“平台对象占用”及当前认证 owner，展示分项、已测桶数、未知覆盖、过期与分类口径；默认折叠的桶明细和任务详情保持。统计限当前配置的平台桶、当前可见对象；历史版本、DeleteMarker、未完成 multipart、本地临时目录、提供方物理开销及未盘点位置不在范围内，不冒充磁盘容量。

文件范围：API `models/storage_usage.py`、窄迁移、`services/storage_usage.py` 与独立分类 helper、`workers/storage_usage.py`、`schemas/storage.py`，相应用量／分类／迁移用例；Web `api/storage.ts`、`pages/Storage/StorageUsagePanel.tsx` 与测试；OpenAPI／生成类型、README／CHANGELOG、用户存储说明、API 指南、开发计量口径及 E09／执行队列。超过八文件，以共同的持久快照合同交付；无依赖、镜像或发布版本变化。

验证只围绕计量：同址重复申报／共桶、六分类对账、准确地址／端点、无归属和冲突、真实大小不等于预约数、训练前缀多文件、删除后残留、多页／旧检查点恢复、失败保留及读取零源 I/O。复用已通过的权限、取消和代次回归，不重做消费者资格验收。一次性 test／e2e 做真实数据库和独立 Celery／MinIO 盘点、API 与明暗／窄屏展示；迁移后按所属监督服务刷新受影响人工 Worker，保留 3100／8100、Beat、业务和原件指纹，清理测试资源后提交。完整 E12 其余原始要求仍独立推进。

## 本轮定稿：E12-C2 / E09-B4 当前已采纳源索引规模

实现与本地验收已完成，结果见 [C2 Outcome](#e12-c2--e09-b4当前已采纳源索引规模)。下列内容保留为实施合同。

2026-10-25 基于 `7fe707bb6` 对照原始 E09-S04／AC04、AC05 与 E12-S03／AC01。`storage_usage.read_usage` 已从持久盘点读取平台当前对象字节，并按实际桶去重；位置来源摘要只有引用数量和诊断，`DatasetRegistrationRun.counts.indexed_bytes` 仅属于单次注册，不能作为位置级去重规模。下一片补齐这一明确缺口，不延伸 Tracker、会话、审核或模型工作流。完整平台用途分类、提供方物理容量、告警和生产备份仍按各自原始要求分别记录。

统计准确命名为“当前已采纳源索引规模”：以当前可见来源的 `DatasetSourceItem` 为主，连接 `current_revision_id` 指向的采纳修订、该修订扫描记录及条目和 Item／Asset，核对固定关系、reference／managed_copy 模式、摘要与完整大小。两种模式都计来源文件，托管复制形成的平台对象仍单独计量，两个指标不相加。使用该修订冻结的定位事实，不按当前配置猜测历史位置；同 Asset 重新采纳或移动后不强制等于最初 `DatasetReference.run_id`。不累计全部历史修订、未采纳候选、未完成扫描或缓存。暂停／注销但仍保留的采纳头继续计量并说明状态，业务删除后不从历史审计补造当前头。

去重限单个逻辑位置内可证明相同的物理引用，使用冻结扫描的 anchor 与完整来源地址，合并跨 Dataset 的重复和父子来源重叠；不按 SHA、Asset ID、source_id 或来源内 path_digest 单独去重。不同位置别名、无法比较的节点挂载和缺少定位事实不能被宣称为全局物理去重。相同物理引用的大小／内容事实冲突时单列未知，不任取最新值、最大值或重复相加。普通名称、缓存或授权修改带来的修订变化不抹去已采纳索引，也不被解释为原件此刻可读。

内部去重域：filesystem 使用位置 ID、冻结 `scan_host_id`／root／base anchor 和 `snapshot.config.relative_path + entry.relative_path`；不得用每个来源自己的 `source.root_identity` 拆开父子来源重叠。S3 使用位置 ID、冻结 endpoint／bucket、完整 object key 和规范化 VersionId，`None`／`"null"` 同属未版本化定位，不同非空版本分别计量。SFTP 使用位置 ID、冻结 root_handle／qualification 和完整受控路径。来源修订和过滤条件不进入去重键；无法比较的多节点记录进入未知覆盖，不能简单当成不同物理文件相加。本片不证明硬链接、重复挂载或跨位置别名的全局物理去重。

在现有 `GET /storage-locations/{id}/source-summary` 增加固定大小的 `usage` 结果；复用当前 manager、位置可见性与 `source_scope_clause`，统计本位置全部可见来源，不依赖来源／任务分页。候选、去重、未知计数和汇总在同一 SQL 快照完成，返回引用条数、可证明去重文件数与字节、未知或冲突数量、覆盖范围、去重依据、观测时间范围和查询时间；覆盖不全时明确区分已知部分与未知总量。没有可见采纳头的受支持范围可确认为零，尚未支持或缺证据的范围返回未知。响应不含路径、anchor、endpoint、bucket、key、凭据或逐样本清单。GET 不读目录、不访问对象存储、不发任务、不写新快照。

复用现有居中位置详情的来源诊断区域展示该统计，与平台对象字节分列，替换 `StorageLocationsConsole` 中“已索引源数据量：待统计”的固定占位；保留紧凑字号、未知／部分／旧观测说明、刷新失败保留旧值及撤权隐藏。后端增加 `services/storage_source_usage.py` 与专项测试，接入现有 summary／schema；前端在 `StorageLocationSourceSummary` 附近复用或抽取小型展示组件。同步 OpenAPI／Web 类型和适用 SDK 合同、用户存储说明、API 指南、开发指标口径、README／CHANGELOG、本计划、E09 与执行队列。无新表、迁移、Worker、依赖、环境变量、镜像或发布版本。

验证围绕本统计：同一真实目录文件被两 Dataset 采纳只计一次，父子来源重叠、不同文件同 SHA、移动／重采纳／替换、缺失或冲突证据、暂停／注销／业务删除、当前权限裁剪及分页不改变汇总。检查 GET 零源 I/O、固定查询数量和固定响应大小；复用已有 S3／SFTP 夹具核对对应冻结地址语义，不重复全套消费者验收。Web 覆盖已知／未知／部分、刷新失败与撤权；隔离 e2e 做实际双 Dataset 重复引用及明暗／桌面／窄屏查看。按现有流程检查生成合同、代码与文档，保留 3100／8100 人工环境并清理本片一次性资源。该切片完成后只资格化这一计量范围，不据此宣称完整 E09／E12 完成。

## E12-C2 / E09-B4：当前已采纳源索引规模

2026-10-25 完成实现和本地验收，对应原始 E09-S04／AC04、AC05 与 E12-S03／AC01 的来源规模分项。现有 source-summary 返回整个可见位置的 usage，与平台对象字节分列；统计只读保存的当前采纳头、冻结扫描及完整内容证据，不访问物理来源。跨 Dataset 和父子来源按准确地址去重；不同路径同 SHA 仍分别计量，S3 的准确特殊键与对象版本保留。暂停／注销保留头仍计量，删除头不从历史补造。位置内多个不可比较冻结域使该范围保持未知；同址内容冲突排除整组，已知小计不冒充总量。

一次性 test 数据库中 44 项 API／服务／真实数据库用例通过，覆盖三类来源、重新采纳／移动、权限裁剪、来源分页、坏历史证据和零源 I/O。去重汇总使用一个 SQL statement，应用不加载逐文件清单。25 项相关 Web 用例通过，空观测文案调整后再验其中 6 项；本轮还完成 679 文件／6,968 项完整 Web 回归。4 项 SDK 合同、OpenAPI 检查／类型生成、TypeScript、完整 Web lint／CSS tokens、Ruff／格式及文档检查／构建通过。

隔离 e2e 通过真实扫描／采纳服务准备三个位置，再由实际 HTTP 和浏览器查看：双 Dataset 共 4 条采纳记录去重为 2 文件／226 B；冲突位置保留 1 文件／113 B 的已知小计、2 条未知记录和 1 组冲突，总量为 null；空范围确认为 0 B。前两种在来源 limit=1 的不同页保持同一汇总。浏览器实看桌面 1440 与窄屏 390、明暗主题；刷新、统计口径展开、位置切换和 Escape 关闭正常，新卡片与页面无横向溢出。控制台零 error、零 warning，捕获网络无明确失败或 HTTP 错误。该证据是本地合成统计验收，不代表提供方物理容量或生产规模性能。

本片无表／迁移、Worker、依赖或镜像变化；人工 3100／8100、两个 Worker／Beat 保持，12 表 155 行、24 对象及 12 源文件指纹未变。按归属清理一次性 test／e2e 资源和本轮临时产物。仅此来源规模分项完成，完整 E09／E12 仍按原始矩阵继续，不增加 Tracker、会话或审核前置。

## 本轮定稿：E12-C1 真实目录扫描资源与取消恢复基线

本地测量已完成，实际结果与限制见 [C1 Outcome](#e12-c1真实目录扫描资源与取消恢复基线)。合同不替代生产或真实站点资格。

2026-10-25 基于 `491f35fdd` 只读核对原始 E12-S05／AC05。扫描入口是 `workers/dataset_scan.py`，以目录为事务检查点，已有取消、恢复及旧 attempt 围栏；`filesystem_storage.scan_directory` 最多保存一个目录清单，限制 5,000 项和两秒；`dataset_scans.BUDGET` 限单轮 50,000 项／5,000 个目录，写入每批 200 行。合同定稿时，现有小夹具和 E08 索引基准尚未证明代表性文件树上的实际资源与恢复代价；以下保留测量合同，实际结果见 Outcome，不将源码预算当作实测资格。

复用现有一次性 e2e 资源与独立真实 Celery Worker，从已有服务创建、取消和恢复目录扫描，不改共享环境、生产扫描算法、存储预算或 Worker 协议。新增可重跑的受控基准工具及必要的归属／结果校验测试；只允许已核验的本工作树可丢弃数据库和工具创建的临时根，失败也收尾子进程、准确文件和资源。无新的产品页面、表、接口、依赖、镜像或发布门槛。

在现有预算内测 1,000／10,000／40,000 个文件及对应目录，包含宽／深分布、中文同名、合法和损坏图片及排除项。单目录超过 5,000 项和单轮超过既有总预算另测明确失败，不提升预算来满足档位；原计划的十万档只是候选，不能在当前五万项限制下宣称已支持。记录实际文件系统、硬件、数据组成、冷热状态和测量开销；结果限本机合成目录，不推断 CephFS、NFS、跨站点或生产吞吐。

每档记录真实 Worker 峰值 RSS、总用时／吞吐、逐目录处理时间、SQL 次数／耗时、目录遍历和 stat／内容读取次数、文件描述符收尾与最终准确清单。中途取消后核对未完成状态、已提交目录和晚到提交拒绝；恢复同一运行，量化重验目录及重复工作，并对账无重复／漏项和源文件未修改。只读列表查询另核对不遍历物理目录，不重复 E08 已完成的页面性能验收。未达成处记录可复现瓶颈和当前限制，只有测出的直接问题才提出必要优化，不自动新增其他业务改造。

交付受控工具、可复现命令、脱敏测量表及准确限制，知识写入 `docs-site/dev/` 的存储基准说明并链接现有扫描排障文档；本计划和实施队列记录实际结果。保留 3100／8100、人工 Worker／Beat、业务及原件指纹；结束后销毁可丢弃环境并清理测试文件与报告，正常检查和提交。生产 SLO、真实站点和 E12 其余恢复／告警工作继续分别记录，不以本片代表整项完成。

## 本轮定稿：E12-A3 本地来源故障分类与关联

2026-10-25 基于头像用途切换收尾 `20e36ca36` 复核原始 E12-S04／AC04。本片交付本地数据集来源的根不可用、存储认证／访问拒绝、实际原件变化与预览生成失败事件，复用现有 AuditLog、来源轮次、Preview 和 AsyncJob；不要求 Tracker、会话或审核改造，不另建事件中心或告警平台。整套实施与提交授权继续有效，不另设批准轮次。

最小方案是在失败响应中增加说明，但响应结束后无法按位置／样本／后台任务追溯。采用现有审计历史，action 为 `storage.fault`、target 为 `storage_location`，明细仅包含固定分类／错误码／阶段及已授权解析的 location、source、dataset、item、asset、task、run、job 等 UUID。HTTP request_id 沿现有上下文；后台失败保留真实 Run／Job 关联，不伪造原 HTTP 请求。禁止记录路径、对象键、签名 URL、凭据或异常原文。

根分类仅在实际打开已配置根或核对根身份时标记，不把任意缺失文件称为挂载缺失；认证分类在 S3／SFTP 实际服务拒绝点标记，不把所有连接失败称为认证失败。原件变化只观察实际读取／摘要核验，客户端提交错误 SHA 不形成原件变更事件；预览沿已有失败事实记录，仍不回滚已核验原件。已有错误响应及业务权限保持。

读取事件使用 `storage_access.engine(db)` 的独立有界写入，调用者回滚不删除观测，也不提交调用者业务；观测失败只记固定安全日志，不改变原读取异常。扫描失败仍先以现有 task／fence 验证当前尝试，仅记录实际失败。复用审计页中文动作、对象类型及明细过滤；位置来源诊断可跳转当前故障记录，不增加独立页面或查询端点。最脆弱前提是观测库此时可写，失败时允许丢失该旁路观测，不改变读取正确性或编造持久成功。

超过八文件，范围为新 `storage_fault_events.py`、现有 filesystem／S3／SFTP 错误捕获、dataset_content／dataset_references／directory_previews 与扫描失败接线，审计标签及位置诊断链接，相关测试和用户／开发／API说明、CHANGELOG及执行队列。无新数据库表、迁移、服务、依赖、环境变量或模型协议。验证四类真实文件／受控服务失败、独立事务与回滚、未授权及错误客户端 SHA 零事件、观测失败保留原响应、原扫描 fence 与 Job 关联；复用实际浏览器检查新增动作／链接与既有明暗／窄屏布局。保留3100／8100及原业务／字节，刷新受影响 Worker，清理任务资源并正常提交。远端站点事件、通知渠道和其他消费者接入保留为原 Epic 后续，不作为本片新增前置。

## 本轮定稿：E12-B2 外部原件保护责任

2026-10-25 基于 `7ba1eaa08` 对照原始 E12-S07、§8.1 与 AC08。现有恢复演练已保留外部索引并拒绝缺失／同名替代，责任方却只在夹具返回事实里硬编码，业务表与界面无法登记。本片补齐来源级保护责任、恢复方式及内部参考编号，恢复后保留真实声明；不把声明等同于外部原件已备份、已恢复或物理删除许可。整套实施授权继续有效。

最小做法是在部署文档登记，但无法跟随不同子目录的来源与数据库恢复保留。采用 DatasetSource 两个独立字段：可空 JSONB 声明与从0开始的声明修订号；旧来源保持未登记。责任方为可保留的团队／人员名称，恢复方式为外部备份、上游重新获取、无恢复方案或未知，内部编号可空且不接受路径／URL。服务器记录操作主体及时间，声明有自己的 expected_revision。独立 PUT 接口沿既有来源管理／Key 权限与 Location→Source 锁顺序更新；不访问物理来源，不改变 config_revision、generation、policy_revision、扫描／登记作业或内容绑定。停用、注销、归档及孤立来源保留责任记录，访问沿现有范围，孤立来源仅超级管理员可见。

复用数据集当前来源的紧凑摘要与居中编辑弹窗，标签在上、提示在下；未知和无方案明确区分，不显示“备份验证成功”。位置来源诊断展示同一只读摘要，原数据集删除后仍可查看声明。新增数据库字段与窄迁移、公开 schema／PUT、服务和已有 DTO／摘要接线、Web API／组件与测试、SDK 覆盖分类、恢复夹具与专项验证，连同正式用户／API／开发／运维说明、README、CHANGELOG及队列，超过八文件。无新表、服务、依赖、环境变量、模型协议或镜像重建。

验证真实数据库登记与独立修订冲突、同位置不同责任、当前权限／Key、活动扫描与既有读取不变、停用／注销／Dataset删除后保留；迁移往返及非空声明拒绝破坏性降级。恢复夹具通过业务服务登记后生成指纹，从持久字段产生外部责任事实；实际销毁源后的隔离恢复核对原声明和修订，再沿用缺失原件及同名内容拒绝。浏览器核对保存、冲突提示、来源切换、明暗／宽窄和弹窗焦点。维护人工3100／8100、两个Worker／Beat及原业务／原件；按归属清理后正常提交。声明最脆弱前提是填写者信息准确，因此所有方案均显示“已声明”，不借声明编造实际可恢复性；真实生产备份和部署目标保持原有独立事项。

## 本轮定稿：E12-B1 离线一致备份与隔离恢复演练

2026-10-24 基于 `2a18af83b` 重新核验，工作树干净。整套实施已获用户授权；本片补齐 E12-S06 的本地方案与试点，不将其等同于生产备份、在线/PITR、保留保护或生产 RPO/RTO。现有工作树归属/迁移验证复用；现有分步 dump/mirror 不是一致恢复证据。3100/8100、两 Worker、beat、业务库及镜像均保留。

```text
new random owned source -> real synthetic asset/label/freeze + interrupted task
             | offline, no API/Worker/Beat, exact owner / no clients
             v
one backup_generation -> PG custom dump + all 9 purpose objects + SHA/metadata
                       -> configuration/heads/frozen facts + separate key material
             | verify complete archive, then destroy source resources
             v
new random owned empty target -> full PG restore -> explicit physical mapping ledger
                              -> object restore + key + immutable business verification
                              -> unfinished work / missing external original held for review
             v
bounded timing/report -> clean both owned namespaces and private artifacts
```

只接受脚本本次新建的两个随机 test root，无指向既有环境的参数；由 LocalResources 验证本机服务器、DB comment、九桶 tag 和 Redis label。先检查全部归属，锁模式，未知客户端或非空目标拒绝；不强断连接、不创建/修改共享PG roles。目标只创建空库，full restore 前不执行 Alembic upgrade；恢复后验证迁移头再授权runtime。源无运行服务，合成数据准备完成后零客户端；清单完成前复核数据库和对象集合不变，来源停写是本试点的进程隔离条件，不能宣传为生产停写门禁。

备份目录0700、文件0600；统一代次包含代码提交、schema/head、数据库dump SHA、九用途映射、逐物理对象key/大小/流式SHA及读取所需metadata、外部原件保护责任、未完成作业和冻结事实。总对象/单对象/总字节/dump设明确预算；S3分页，不以ETag替代SHA。首版仅保护当前对象；遇版本化、删除标记或未完成分片拒绝发布。备份最后原子写完成标记；缺对象、读中变化、中断或篡改没有成功代次。密钥独立受限文件保存，仅合成演练专用Fernet key，不复制真实.env，不写公共清单/日志/Git；缺失/错误key必须阻止恢复验证。

恢复到独立九桶，不能仅换环境变量。只在恢复目标DB按白名单变换 StorageBinding、平台 DatasetAssetLocation、SourcePreview、OriginalCacheEntry、PredictionInputLease、TrainingStorageDeletion 的物理桶/存储身份，并保留变换账本；记录未知/不匹配绑定须拒绝。源与目标使用独立配置子进程，原dump、业务ID、Asset/SHA、采纳修订、snapshot/manifest/package及来源证据不得改写。容量观测保留其原位置/时间身份，不冒充目标实时统计。旧Redis队列不恢复、不启动调度；未完成版本/作业记录保留并报告待核对，不能自动重新执行含历史物理locator的冻结任务。B1不开放恢复应用流量；后续上线门禁须覆盖所有异步owner及外部映射。

实际演练须在备份后销毁源，证明目标不借源桶/源库：恢复标注—样本—Asset/修订链，校验ready版本所有原图、标签、清单、ZIP和Native输入；部分冻结保持未完成。合成外部原件移除后索引仍保留、实时严格读取拒绝，同名不同内容替代也须拒绝，ready保留内容独立可验。包含缓存与临时输入/删除账本的目标映射，证明不再指向源桶；缺key、坏dump/对象、owner不符、目标非空、预算和故障阶段清理设有意义回归。报告已知零写入窗口和阶段用时，只说明本次规模。

准确范围超过8文件，跨工具/DB与存储合同/运维文档：新增 `scripts/storage_backup_bundle.py`、`scripts/test_storage_backup_bundle.py`、`scripts/verify_worktree_recovery.py`、`scripts/worktree_recovery_fixture.py`、`scripts/worktree_recovery_mapping.py`、`scripts/test_worktree_recovery.py`；必要复用现有helpers，不修改生产API/Worker/依赖/schema。新增正式 `docs-site/ops/deploy/storage-recovery.md`，更新部署两页、运维索引、DEV、README、CHANGELOG、本计划及整套队列。纯脚本unit命令与真实 `apps/api/.venv/bin/python scripts/verify_worktree_recovery.py` 分开记录；Ruff/格式、文档frontmatter/导航/可读性/构建、diff及提交钩子。无UI变更不重复浏览器矩阵。

回退移除演练入口及工具提交；所有源/目标为本次随机owner资源，finally按原归属销毁，不触碰dev。失败清理不能冒充成功；若资源清理失败保留private清单和具体owner供再次核对，不强删未知资源。归档所有正常和注入故障报告的脱敏摘要到Outcome，删除任务临时文件后提交；整项E12仍保持活动。

## 本轮定稿：E12-A2 / E09-B2（已交付）

2026-10-23 用户继续授权；基线 `08d5d5ed2`，工作树干净。本轮交付位置内来源诊断，不新增数据库表、任务或环境变量，不重建后端镜像。

```text
Location modal -> source-summary GET -> current-authority source page
                                      -> reference observations + preview records
                                      -> active/latest scan and preflight + last complete
              -> dataset/source/scan links + shared AsyncJob modal
```

- 只聚合本页授权来源的 `DatasetReference` 业务样本，含保留的历史修订；托管复制和未登记路径不计入。原件复用 Workbench 六态及观测时间范围，明确是历史观测，不是实时健康检查。
- 预览独立计数：当前 Item/Asset/SHA、SourceItem 与规格匹配的 ready/failed 记录，无匹配为 unrecorded；策略关闭和来源停用分别计数。当前内容可合法复用已有预览；不要求策略修订相等。ready 还核对当前平台绑定，只表明生成记录，不证明对象此刻存在。
- 扫描、预检分别展示活动优先的最近轮次，以及最近完整成功轮次；恢复旧轮不能被新失败记录遮住。完整性、时间、配置修订匹配分别表达；预检不依附于最新扫描。配置不匹配的历史原件观测不改写成当前健康。
- GET 只查数据库，不读取文件、HEAD/LIST 对象、探测节点或生成预览。页内批量 SQL，无逐来源查询。继续沿用 manager、位置和来源授权及 `datasets:read`；只返回白名单字段，配置、路径、证明和原始错误不外露。
- UI 来源列表占满宽度，原件/预览/扫描分开，细项和任务历史折叠，活动任务和失败摘要可见。保留独立分页、数据集/来源/扫描深链与 return、居中任务详情、Escape 和焦点恢复、切换账号及撤权隔离。
- 文件范围（超过 8 文件，跨 API/Web/docs，以共同只读合同提交）：`app/services/{dataset_workbench,storage_source_summary,storage_source_diagnostics}.py`、`app/schemas/storage_location.py`、`tests/test_storage_source_diagnostics.py`、现有摘要/Workbench 回归、OpenAPI snapshot、`StorageLocationSourceSummary{.tsx,.test.tsx}`、`StorageSourceDiagnostics.tsx`、README/CHANGELOG、存储位置 API/用户指南/架构文档、E09/E12 及 backlog 进度。API 路径前缀均为 `apps/api/`，Web 路径前缀为 `apps/web/src/components/connections/`。
- 验证：一次性 test DB；原件变化/失踪、历史内容身份及移动、预览规格/SHA/策略/绑定、活动旧轮/失败/不完整/旧配置、任务历史删除、权限及页范围、GET 零源 I/O。Web 验证独立分页、诊断口径、深链、撤权与刷新失败。生成 OpenAPI/types，SDK 合同、tsc/lint/css tokens、文档检查及 diff 检查。
- 人工环境保留：在当前 dev 中用已有 D-7 观测查看实际 API/UI；桌面和窄屏、明暗主题、嵌套详情/Escape/焦点/返回与无溢出，检查网络及控制台。清理本轮 test 资源与临时截图，不删除验收业务数据。
- 回退：回退应用提交即可；本轮无迁移、写入或新后台执行。E12 其余容量/配额/恢复及 E09 其余诊断不宣称完成。

## 本轮定稿：E12-A1 / E09-B1

已重新核对 CHANGELOG、E09/E08 当前阶段、API/模型、0208 迁移头、任务权限与测试夹具。E01/02 和统一 AsyncJob 已满足本切片依赖；E11 清理和 E07 迁移触发条件不属于本次。本轮只交付平台九用途的后台对象盘点、成功快照和存储页消费，不宣称完整 E12-A 已完成。

```text
StoragePage / ML overview -> snapshot read API -> PostgreSQL success snapshots
                            |
Super administrator -> inventory POST -> AsyncJob + singleton checkpoint
                                           |
                                    Celery paged S3 listing
                                           |
                           fenced checkpoints -> successful bucket snapshot
                           Beat lease recovery / cooperative cancellation
```

### 合同与边界

- `GET /storage/buckets` 只读数据库，不访问对象存储；ML overview 使用相同快照。九用途按 endpoint + bucket 去重，以 `roles` 表达共用，保留 `role` 为首个用途的兼容字段。
- `POST /storage/usage-inventory` 创建统一后台任务；重复提交复用活跃任务。读取和启动均要求超级管理员；API Key 要求现有全权 `*`，执行中重新核对账号、Key 和配置。任务列表/详情/取消遵循同一权限。
- 每页最多 1000 个当前对象，累加实际对象字节，包括目录标记；每段最多 10 页 / 30 秒，分页提交检查点，段间延迟 1 秒。单桶最多 10000 页、整轮最多 24 小时，超预算显式失败。连接/读取超时及重试有界。Worker 与 Beat 使用现有 default 队列。
- 同一轮串行盘点各桶；每桶只有完整分页成功才替换成功值。单桶失败保留原值和原时间，继续其他桶；整轮标记失败并显示成功/失败桶数。取消保留已完成桶和旧成功值。崩溃/投递失败由租约恢复；任务 ID 和执行代次隔离旧执行者。
- `measured_at`、`measurement_started_at`、`scope`、`source`、`coverage`、`is_estimate`、尝试状态和时间独立返回。超过 24 小时或最近尝试失败/取消为 stale；没有成功值返回 null，不冒充零。总数仅在全部配置桶均有成功快照时提供，同时返回已测桶数和总桶数。
- 列举为采样时间窗口内当前对象汇总，非原子快照，不含历史版本、删除标记、未完成分片、供应商副本/压缩开销。外部源索引字节不混入。物理总量、可用量和配额明确未知。
- 新增 0209 两张表：成功桶快照和单例盘点检查点。AsyncJob 被保留期删除后快照仍保留，FK SET NULL。endpoint/bucket 变更后旧身份快照不复用。
- UI 使用既有紧凑字号、语义状态色与全宽列表；统计默认折叠，活跃任务/失败摘要可见；详情复用居中 AsyncJobDetailModal。刷新状态仅刷新查询，单独按钮启动盘点。

### 精确文件范围

本轮超过 8 个文件、跨 API/DB/worker/Web/docs 五个组件，属于较大切片；以共同的快照合同保持原子提交。

实际修改清单（相对仓库根）：

- `CHANGELOG.md`
- `README.md`
- `apps/api/alembic/versions/0209_storage_usage.py`
- `apps/api/app/api/v1/admin_ml_integrations.py`
- `apps/api/app/api/v1/async_jobs.py`
- `apps/api/app/api/v1/storage.py`
- `apps/api/app/db/models/__init__.py`
- `apps/api/app/db/models/async_job.py`
- `apps/api/app/db/models/storage_usage.py`
- `apps/api/app/schemas/storage.py`
- `apps/api/app/services/async_job_notify.py`
- `apps/api/app/services/storage_usage.py`
- `apps/api/app/workers/celery_app.py`
- `apps/api/app/workers/signals.py`
- `apps/api/app/workers/storage_usage.py`
- `apps/api/openapi.snapshot.json`
- `apps/api/tests/test_admin_ml_integrations.py`
- `apps/api/tests/test_storage_event_loop.py`
- `apps/api/tests/test_storage_usage.py`
- `apps/web/src/api/adminMlIntegrations.ts`
- `apps/web/src/api/asyncJobs.ts`
- `apps/web/src/api/storage.ts`
- `apps/web/src/components/connections/StorageLocationsConsole.test.tsx`
- `apps/web/src/components/connections/StorageLocationsConsole.tsx`
- `apps/web/src/components/jobs/AsyncJobDetailModal.test.tsx`
- `apps/web/src/components/jobs/AsyncJobDetailModal.tsx`
- `apps/web/src/components/jobs/asyncJobPresentation.test.ts`
- `apps/web/src/components/jobs/asyncJobPresentation.ts`
- `apps/web/src/hooks/useStorage.test.tsx`
- `apps/web/src/hooks/useStorage.ts`
- `apps/web/src/pages/Storage/StoragePage.test.tsx`
- `apps/web/src/pages/Storage/StoragePage.tsx`
- `apps/web/src/pages/Storage/StorageUsagePanel.test.tsx`
- `apps/web/src/pages/Storage/StorageUsagePanel.tsx`
- `docs-site/api/guides/_routes.generated.md`
- `docs-site/api/guides/storage-locations.md`
- `docs-site/dev/reference/storage-buckets.md`
- `docs-site/user-guide/datasets/storage-connections.md`
- `docs/plans/archive/1792661016_dataset-storage-console-boundaries.md`
- `docs/plans/1792661016_dataset-storage-operations-recovery.md`
- `docs/plans/backlog/1792661016_dataset-storage-baseline.md`
- `docs/plans/backlog/1792661016_dataset-storage-roadmap.md`
- `docs/plans/backlog/README.md`
- `packages/python-sdk/api-coverage.toml`

### 验收与回退

主要分层命令：

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_storage_usage.py tests/test_storage_event_loop.py tests/test_admin_ml_integrations.py -k "storage_usage or slow_storage or overview" -q'
pnpm --filter @anno/web exec vitest run src/pages/Storage/StorageUsagePanel.test.tsx src/pages/Storage/StoragePage.test.tsx src/hooks/useStorage.test.tsx src/components/connections/StorageLocationsConsole.test.tsx src/components/jobs/asyncJobPresentation.test.ts src/components/jobs/AsyncJobDetailModal.test.tsx
pnpm --filter @anno/web exec tsc -b --noEmit
pnpm --filter @anno/web lint
pnpm --dir docs-site check:frontmatter
pnpm --dir docs-site check:navigation
```

- 一次性 test 模式 API/DB：无成功快照为 null；九用途/共桶去重；多页精确累计与目录标记；失败/取消保留成功值；重复提交；配置/账号/Key 撤权；旧代次不能提交；崩溃检查点恢复；投递失败可恢复；查询和 ML overview 零 S3 调用；迁移升降级与 SET NULL 保留。
- Web Vitest：空值/真零/过期/失败/部分覆盖、启动/重复启动、查询刷新不启动任务、任务详情与切换账号晚回调隔离。
- `pnpm openapi:export`、`pnpm codegen`、SDK OpenAPI 合同、Web tsc/lint/css-token、文档 frontmatter/navigation、`git diff --check`。
- 人工环境：确认无重要活跃任务后刷新本工作树 API/Worker/Beat；核对注册任务并实际盘点。桌面/窄屏、明/暗主题截图；重复刷新只发查询；详情居中/Escape/焦点与无溢出；检查控制台/失败网络请求。仅清理本轮测试资源，不删人工验收数据。
- 回退：先停盘点/取消任务，回退应用提交，保留 0209 成功快照表；如需降级，在确认无盘点执行后降至 0208（只删除本切片观测元数据，不删除对象）。不回退为每请求扫描的默认 UI，不触发对象搬迁/清理。

## 研究背景（晋升前基线）

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                             | 已有能力、缺口与复用约束                                                                                 |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| [storage.py](../../apps/api/app/services/storage.py)              | summarize_bucket 全量物化列表，失败 error+零值，无持久成功快照；线程池避免阻塞不等于消除每请求全桶遍历。 |
| [storage.py](../../apps/api/app/api/v1/storage.py)                | health 只查 annotations，buckets 逐桶扫描；需分开节点/根/能力探测、容量采样和业务可用性。                |
| [docker-compose.md](../../docs-site/ops/deploy/docker-compose.md) | 已有 pg_dump/restore 和对象镜像说明，缺一致代次、对象清单及隔离恢复对账证据。                            |
| [lan-production.md](../../docs-site/ops/deploy/lan-production.md) | 已有九桶、配置、密钥/CA 备份要求；外部原件由明确责任方保护，索引不冒充备份。                             |

候选修改入口：[storage.py](../../apps/api/app/services/storage.py)、[storage.py](../../apps/api/app/api/v1/storage.py)、[storage.py](../../apps/api/app/schemas/storage.py)、[async_job.py](../../apps/api/app/services/async_job.py)、[StoragePage.tsx](../../apps/web/src/pages/Storage/StoragePage.tsx)。本轮准确文件范围与迁移已固定于上方实施合同；其余切片需独立复核。

## 1. 当前问题与依据

当前 `summarize_bucket` 通过列举整个桶计算对象数和字节数，页面刷新可能触发昂贵的全量统计；当前存储页“总容量”使用的是对象已用字节，并不等于磁盘物理容量。[存储服务](../../apps/api/app/services/storage.py)、[存储页面](../../apps/web/src/pages/Storage/StoragePage.tsx)

系统已有 AsyncJob 全局历史和详情、媒体失败项重试，应该扩展其证据和诊断能力，而非新建另一套运维任务中心。[任务入口](../../apps/web/src/components/shell/JobsBell.tsx)、[任务详情](../../apps/web/src/components/jobs/AsyncJobDetailModal.tsx)

外部目录原地引用后，平台需要同时表达“索引了多少外部数据”“平台实际存了多少副本”和“底层磁盘还剩多少”，不能把三者相加成一个使用量。

## 2. 目标与边界

目标是让用户判断数据是否可读、任务卡在哪里、平台真实占用多少空间，以及出现数据库或存储故障后如何恢复到可验证状态。

范围包括统一指标口径、异步容量核对、读取与同步观测、故障分类、保留策略执行观测、恢复清单和演练。具体垃圾回收授权规则归 EPIC-11；业务 UI 归 EPIC-08/09；跨站点路由归 EPIC-06。

不承诺未知外部存储的总容量，不把集群副本机制当备份，不默认平台替用户备份所有外部原件，不在普通页面暴露密钥、完整路径和签名 URL。

## 3. 用户故事

| 编号    | 用户故事                                                                         | 阶段                   |
| ------- | -------------------------------------------------------------------------------- | ---------------------- |
| E12-S01 | 作为数据负责人，我能区分原件不可读、预览失败和扫描未完成，知道应找谁处理。       | M1                     |
| E12-S02 | 作为运维，我能查看已用空间、配额与数据新鲜度，且页面刷新不扫描全部对象。         | M1/M2                  |
| E12-S03 | 作为平台负责人，我能知道外部引用、托管原件、预览、训练冻结与临时文件各占多少。   | M2                     |
| E12-S04 | 作为运维，我能关联某个用户读取错误、位置健康、后台任务和远端站点事件。           | M1/M3                  |
| E12-S05 | 作为维护者，我能在代表性规模下测量扫描、读取、同步与训练准备的资源瓶颈。         | M0/M2                  |
| E12-S06 | 作为运维，我能从数据库备份与一致的对象清单恢复，并证明版本和标注仍指向正确内容。 | M1 方案与试点；M2 演练 |
| E12-S07 | 作为负责人，我能识别缓存占满、长期失败任务和无法保护的外部原件，并做明确取舍。   | M2                     |

## 4. 统一容量口径

| 指标               | 定义                                             | 能否跨数据集直接相加                         |
| ------------------ | ------------------------------------------------ | -------------------------------------------- |
| 逻辑样本字节       | 当前查询范围内样本引用的内容大小之和             | 否，同一内容可被多个数据集引用。             |
| 已索引外部原件字节 | 已完成元数据观测的外部文件内容大小               | 仅在明确去重域和来源范围内聚合。             |
| 托管原件占用       | 平台管理的持久原件副本字节                       | 按唯一物理位置记录聚合，不按引用次数累加。   |
| 预览/缓存占用      | 平台生成或按需保存的派生文件字节                 | 按唯一缓存对象及类别聚合。                   |
| 版本冻结占用       | 为版本可复现保留的原件、标签、manifest、打包文件 | 区分复用原件与额外副本，不能重复算共享对象。 |
| 临时产物占用       | 导入暂存、导出临时包、未完成上传等               | 与持久引用分开。                             |
| 位置已用空间       | 提供方/文件系统报告的范围内已用空间              | 必须标明统计范围，不能与对象逻辑字节混算。   |
| 配额               | 管理员或提供方设定的可用上限                     | 不是物理磁盘总容量。                         |
| 物理总量/剩余      | 底层有可靠 API 或挂载指标时取得                  | 未知即显示未知，不以对象字节推算。           |

每个统计响应带 `measured_at`、`scope`、`source`、`coverage` 和 `is_estimate`。展示“已索引”时说明未扫描或失败目录不在统计内。副本、纠删码和文件系统开销由提供方物理指标反映，不由应用简单乘系数冒充真实容量。

## 5. 采集与服务设计草案

- 写入/删除成功后记录可幂等累计的字节变化；异步盘点周期性纠偏。只有最终确认的物理删除才减少实际占用。
- 统计按位置和用途聚合，页面读取持久化快照，不在每次请求里全量列桶。
- 盘点使用分页和 checkpoint，受速率限制；失败时保留上一结果并标注过期，不显示零。
- 外部目录的目录扫描提供索引量；可选节点指标提供挂载容量。两者来源不同，分别显示。
- 定时同步、容量盘点、摘要核验与缓存清理错峰和限流，避免同时读遍同一磁盘。
- 长任务仍进入 AsyncJob；位置维度可过滤任务，审计日志串联 `request_id/job_id/location_id/asset_id`，不以高基数 ID 作为所有监控指标标签。

建议摘要接口返回逻辑、托管、缓存、冻结、临时和提供方指标的独立字段；不提供无范围定义的 `total_capacity` 混合值。

## 6. 健康与告警

| 故障类别          | 可见症状                       | 建议处置                                           |
| ----------------- | ------------------------------ | -------------------------------------------------- |
| 连接/认证失败     | 位置读请求连续失败             | 校验网络或轮换凭据，不触发样本删除。               |
| 根目录未挂载      | 根标识缺失或与配置不符         | 暂停扫描，恢复正确挂载，避免把空挂载点当空数据源。 |
| 内容变化/校验不符 | 已有 Asset 与实际字节不一致    | 隔离该引用并进入变更处理，不能重试后接受新内容。   |
| 容量不足/配额耗尽 | 新副本/预览/冻结写入失败       | 暂停写入任务，允许原图读取；清理必须按保留规则。   |
| 任务无进展        | 心跳正常但 checkpoint 长期不变 | 记录阶段和最后文件，受控重试或人工处置。           |
| 预览失败          | 原图可读但派生产物失败         | 单独重建，不把数据集标为整体不可用。               |
| 站点离线          | 心跳和读取失败                 | 展示降级；由 EPIC-06 处理恢复与候选副本。          |

阈值需在 M0/M1 收集真实基线后设定。告警有去重、恢复事件与负责人，不把单次超时变成连续通知。原图不可读率与预览失败率分别统计。

## 7. 性能验证计划

建议测试档位为 1 万和 10 万张图片，混合大小、深目录、同名文件、少量损坏文件与慢文件系统；这只是验证数据集规格，未经测试不宣称处理上限。

记录首次扫描时间、每批吞吐、峰值内存、数据库查询数、目录 stat/read 次数、取消响应时间、增量重复工作量、预览首屏延迟和原图读取分位延迟。比较修改前后相同环境，避免用网络变化解释代码收益。

关键验收是有界资源与正确性：扫描不一次载入全部路径，后台任务可取消/恢复，列表页不随全库规模重复扫描物理存储。性能预算与 p95 目标在真实磁盘、网络和并发下评审后写入发布门槛。

## 8. 备份与恢复设计

### 8.1 保护范围

- 数据库：Dataset、DatasetItem、Asset、位置和副本索引、来源扫描水位、项目关联、标注、冻结版本清单、必要的任务状态。
- 密钥恢复材料：连接加密所需密钥及配置，独立受控保存；不能只有数据库密文却没有恢复密钥。
- 托管持久文件：原件、标注附件、冻结内容、训练 manifest/包与其他不可重建业务文件。
- 可重建缓存：记录重建规则和版本，可选择不备份，不能包括被业务当作唯一原件的文件。
- 外部原件：登记保护责任和恢复方式。平台只持有引用时，目录丢失不可能仅靠索引还原图片。

### 8.2 一致性方案

1. 备份建立一个 `backup_generation`，记录数据库恢复点、对象清单版本和应用配置版本。
2. 首个可靠版本可使用维护窗口暂停资产写入、删除、冻结发布及相关索引变更，再获取一致清单和数据库备份。
3. 恢复需要的对象按该清单复制并验证；保留窗口内禁止清理这些对象。冻结任务只有内容落盘、校验与数据库发布均完成后才能进入可用备份集合。
4. 后续在线备份可用数据库一致性快照/PITR 加不可变对象清单与删除日志演进，但必须证明同一恢复点不缺对象，不能仅分别执行两个定时备份。
5. 备份完成记录校验摘要、缺失列表、范围、密钥版本与恢复步骤；未完成清单不能标为成功备份。

### 8.3 恢复顺序

恢复密钥和基础配置，恢复数据库与匹配清单，恢复托管对象，核对外部位置根映射，最后恢复读服务和作业调度。遗留“运行中”任务先进入待核对状态，防止重复提交冻结版本或删除操作。

恢复后对选定项目验证标注—样本—内容修订链路、冻结版本 digest、已有 URL 重新签发和位置授权。缺失外部原件显示不可用并保留索引，不允许匹配同名文件后静默恢复。

恢复点目标（RPO）和恢复时长目标（RTO）由业务损失容忍度和实际演练确定；本文不把“每日备份”自动等同于合格恢复方案。

## 9. UI 边界

存储页负责位置健康、容量、采集时间、盘点进度和恢复任务；数据集页只展示本数据集相关的样本可用性、来源同步状态和影响摘要。备份恢复操作默认管理员可见，数据管理员不看到全平台存储拓扑。

空状态明确区分“尚未统计”“提供方不支持”“暂时失败”和“确实为零”。原地注册完成后，可解释新增的是索引与可选预览，原图仍位于来源位置。

## 10. 验收标准

| 编号     | 可验证标准                                                                                               |
| -------- | -------------------------------------------------------------------------------------------------------- |
| E12-AC01 | 外部原件、托管原件、预览、冻结和临时文件分开计量；同一物理对象被多个数据集引用时不会被计入多份实际占用。 |
| E12-AC02 | 不支持物理容量查询的位置显示“未知”；展示配额时明确其不是磁盘总量。                                       |
| E12-AC03 | 连续刷新存储页不会触发全量 list；统计过期显示时间与状态，盘点失败不把已用量变成零。                      |
| E12-AC04 | 根挂载缺失、认证失败、原件变化和预览失败产生可区分事件，并能关联具体位置/任务/样本。                     |
| E12-AC05 | 代表性规模下记录扫描资源与取消/恢复行为，证明路径遍历及查询有界；目标未达成时有具体瓶颈和门槛结论。      |
| E12-AC06 | 从备份恢复到隔离环境后，选定项目标注、内容修订与冻结版本核验一致；恢复所需密钥可用。                     |
| E12-AC07 | 在冻结任务进行中触发备份，恢复后只出现完整已发布版本或明确未完成任务，不出现“可用但缺原图”的版本。       |
| E12-AC08 | 外部目录不可恢复时保留索引和来源责任记录，系统不宣称已恢复原件，不自动读取同名不同内容。                 |
| E12-AC09 | 缓存清理、备份保留与迁移观察期并存时，不会删除冻结版本或有效恢复点仍引用的对象。                         |
| E12-AC10 | 恢复演练有实测数据损失窗口和用时，可据此批准或调整 RPO/RTO，而非仅记录备份任务成功。                     |

## 11. 迁移、回滚与风险

- 新统计口径与旧口径并行对账，页面切换时保留差异解释；撤回新统计 UI 不影响底层字节。
- 观测采集失败不能阻断正常原图读取；来源内容校验失败属于读取正确性问题，不可用“监控降级”绕过。
- 开启自动清理前先 dry-run 列表与字节估计；实际清理权限和保留优先级由 EPIC-11 控制。
- 数据库与对象仓库由不同人员维护时，恢复清单必须有统一负责人，否则两个独立“备份成功”无法证明业务可恢复。
- 待决策：具体备份目标位置、保留时间、外部目录责任人、告警接收人和夜间任务窗口。

## 12. 工程任务与完成定义

- [ ] 定义指标字典、采集范围和去重域，替换“总容量”混用字段。
- [ ] 实现持久化容量快照、异步分页盘点、增量账目与差异核对。
- [ ] 接入统一日志关联、位置健康、错误分类与告警恢复事件。
- [ ] 建立代表性性能数据集和测量报告，确认有界资源与具体发布预算。
- [ ] 设计一致备份清单、删除保护和任务恢复规则；保存密钥恢复材料。
- [x] 完成一次离线合成隔离恢复演练，覆盖冻结中断、缺失外部原件和旧任务状态；生产恢复上线仍须另行验收。

完成定义：用户能看懂实际占用与数据可用性，运维能定位故障并按演练过的步骤恢复业务，而不是只恢复一个运行中的容器。

## 本项目推进切片与晋升条件

运维提供实际目录/容量、备份目标和空间、外部原件责任；测试固定代表数据和预算。当前主机硬件只能说明本地环境，不能推断生产吞吐或多站点规模。

| 切片  | 独立结果           | 交付范围                                                                     | 验收 / 回退边界                                          |
| ----- | ------------------ | ---------------------------------------------------------------------------- | -------------------------------------------------------- |
| E12-A | 诊断与统计快照     | 位置/原图/预览分错，异步分页采样和持久成功快照；随核心链路可定位失败。       | 刷新不全桶 list；失败保留旧值/时间；未知物理容量不为零。 |
| E12-B | 分项账目与一致恢复 | 九用途占用、异步纠偏、备份代次与保留，隔离核对 DB/对象/配置/密钥/冻结。      | 冻结中断不产生假 ready；实测 RPO/RTO 后批准目标。        |
| E12-C | 规模与站点观测     | 按真实图片规模/网络记录 RSS、吞吐、查询、取消、读取与恢复，接 E06 站点证据。 | 1 万/10 万是候选试验档位非承诺，不突破文件/字节预算。    |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_event_loop.py](../../apps/api/tests/test_storage_event_loop.py)、[test_storage_public_url.py](../../apps/api/tests/test_storage_public_url.py)、[test_training_storage_deletion.py](../../apps/api/tests/test_training_storage_deletion.py)、[StoragePage.test.tsx](../../apps/web/src/pages/Storage/StoragePage.test.tsx)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-buckets.md](../../docs-site/dev/reference/storage-buckets.md)、[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)、[lan-production.md](../../docs-site/ops/deploy/lan-production.md)、[network-security.md](../../docs-site/ops/deploy/network-security.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

### E11-C2-C 完整核验历史恢复回归

2026-10-24 扩展既有合成演练，在源准备 matched、同大小不同内容的 changed 及从未派发的 pending 三条观察。销毁源后实际恢复150表91行、九桶20对象；核验历史所有列继续受完整/保护行指纹约束，未加入物理重定位白名单。目标原件定位改变后旧 context=false，pending不自动运行；另一个独立断言在目标显式完整读取匹配。既有冻结/中断、密钥、外部原件缺失及同名替换拒绝验证通过，队列仍待审核。11个恢复纯测试与实际源/目标清理通过；这仍是本地合成恢复资格，不能当作生产恢复点或删除许可。完整交付、342个不同本地用例与人工环境保留证据见[E11 Outcome](1792661016_dataset-permissions-lifecycle.md#e11-c2-c-已登记原件完整内容核验)。

### E12-A1 / E09-B1 平台对象盘点交付

E12-A1 / E09-B1 已完成。主实现提交 `0e85ce496`；本次交付记录同时补上经回归复现的“终态先于快照读取”竞态修复，避免已完成响应携带较早统计而停止轮询。没有宣称完整 E12-A、E12 或 E09-B 完成，也未分配发布版本。

- 已交付：九用途按实际桶去重、显式后台分页盘点、逐页检查点与有界预算、租约投递恢复、进程丢失信号隔离、合作式取消、逐桶成功快照、脱敏失败与旧值/时间保留。GET 和 ML overview 只读快照，超级管理员与全权 Key 限制覆盖任务列表/详情/取消及执行中的授权复核。
- API/DB：30 个不同用例通过（首批 28、进程丢失 1、完成读取竞态 1；竞态修复后附带重验 2 个已有用例不重复累计）。覆盖多页/跨段/重投、共享桶、真零、目录标记、失败/预算、取消后旧值、读前后撤权、配置变化、窄 Key、无外部 I/O 的 GET/overview、0209 迁移升降级、Job 清理后快照保留。读取竞态先出现 `completed + 0/9` 的失败，再修正为先读轮次状态、后读快照，相关 3 项通过。
- Web：6 个目标文件共 57 个不同用例通过，覆盖原存储导航和媒体重试、未知/真零/过期/部分覆盖、显式启动、会话晚结果、拒绝后隐藏及重新授权恢复、触发按钮消失后的焦点回退。异步弹窗显式返回焦点补充通过；最后两项面板边界与位置提示修改后重验 23 项，不重复累计。SDK OpenAPI 合同 4 项通过，合计 91 项本地用例；未宣称全量测试或远程 CI。
- 静态与文档：Web tsc、完整 Web lint、CSS token 检查、Ruff、Prettier、OpenAPI 导出/类型生成与完整提交钩子通过；只有既有 4 个 Web lint 警告和训练比较页 2 处样式建议。OpenAPI 保留全部旧端点，只新增盘点 POST；nullable 计数/状态与全权 Key 的调用兼容性已写明。文档 frontmatter 196 文件、navigation 198 页面通过。
- 真实环境：保留本工作树 dev 3100/8100，0209 已应用，两个 Worker 均注册盘点/恢复任务，Beat active；没有重建后端镜像。最终刷新后的实际任务 `0122136c-969f-4c48-8708-6b81dff6d4b8` 完成 9 个桶、失败 0。人工验收对象总计 20 个、38911 字节；这不是物理容量。取消任务 `79b08671-45aa-4372-8ba1-3f15a0a269db` 保留此前九桶的值与时间，随后 `100972e1-ceb5-4bed-a5fb-df4e7e7d89ee` 再次盘点全部成功。
- 真实浏览器：使用独立会话、无 mock。首次未知、成功零值、九桶采样时间、默认折叠和展开、连续刷新不产生盘点 POST、任务居中与 Escape 返回焦点通过。1440×1000 和 390×844 明暗主题均检查截图；窄屏明细改为容器内横向滚动，页面宽度仍为 390，无页面溢出；窄屏任务弹窗为 342×318，居中且可关闭。控制台没有 error/warn，仅 debug/info；页面错误与失败网络请求均为零。权限、失败、部分覆盖及进程丢失故障以本地测试验证，未在保留的业务环境注入故障。
- 正式文档：`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/api/guides/storage-locations.md`、`docs-site/dev/reference/storage-buckets.md`、`README.md`、`CHANGELOG.md` Unreleased 与生成 API 索引同步；SDK coverage 明确将平台运维入口排除为 Web 专用。
- 清理：一次性 `1822b025369a33f4:test` 数据库/Redis/九桶按 owner 校验销毁；本轮截图、报告、日志、临时脚本、测试缓存及专用浏览器会话清理。保留 dev 人工环境、原演示数据、已有冻结版本与本轮可查看的成功/取消任务记录。
- A1 交付时剩余：E12-A 的原件/预览/扫描关联诊断、E12-B 分项归因/配额与一致恢复、E12-C 规模基准仍需独立复核；E09-B 的 E11 清理影响预览和 E07 迁移/退役合同未实施。本计划继续保留为活动计划，外部源索引字节、历史版本/分片与底层物理容量不冒充已测量。

### E12-A2 / E09-B2 来源诊断交付

实现提交 `58a9725cf`；无迁移、无依赖或 Worker 改动，不重建后端镜像。本切片已完成，不宣称完整 E12-A/E12 或 E09-B 完成，未分配发布版本。

- 交付：位置摘要按当前来源页批量聚合原地引用样本的六态原件观测、当前内容的独立预览记录、扫描与预检的活动优先轮次和最近完整成功；显示历史配置和观测时间。包含历史修订，移动保持身份；托管复制和未登记路径不混入。GET 零源 I/O，不改变既有读取/接纳合同。
- 界面：全宽来源列表，四类证据分开，数量细项和后台任务历史折叠；任务历史独立分页并保持展开状态。保留来源/扫描/返回深链，任务居中展示，Escape 仅关闭上层并恢复焦点。刷新失败保留并标明旧结果，撤权隐藏私有数据。
- API/DB：36 个不同用例通过（来源摘要 8、诊断 14、Workbench 14；夹具修正后仅重验失败项，不重复累计）。覆盖权限/孤儿历史/页范围，GET 禁止存储调用，预览 SHA/Asset/规格/目标绑定，策略停用/恢复，历史修订与移动，空/缺失 Asset，旧活动轮与新失败、配置修订及任务删除。1 与 5 个来源的查询次数未随行数增长。
- Web：摘要 7、位置控制台 12，共 19 项通过；SDK OpenAPI 合同 4 项通过。tsc、完整 Web lint、CSS tokens、Ruff、OpenAPI/TS 生成、API 索引、文档 frontmatter（196）/navigation（198）及 diff 检查通过。仅保留既有 4 条无关 ESLint warning 与训练页两处 z-10 建议。总计 59 个不同本地用例；不宣称全量套件或远程 CI。
- 实际浏览器：保留的 3100/8100 dev 无 mock；D-7 来源为已验证 1、失踪 1、变化 1，独立预览记录为 3。历史来源最近扫描失败、不完整、历史配置及上次完整成功同时可见。刷新只有一条摘要 GET 200，未创建任务。任务详情居中、Escape 和焦点恢复；来源往返存储原位置、历史任务第二页与来源第一页独立保持。1440×1000 和 390×900 明暗主题截图已检查，无横向溢出、页面 JS 错误或失败 HTTP 请求。
- 正式文档：`docs-site/api/guides/storage-locations.md`、`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/dev/concepts/storage-connections.md`、`README.md`、`CHANGELOG.md` Unreleased 及 OpenAPI 同步。API 路由未新增，生成 API 索引与 SDK coverage 无需变更。
- 清理：已按 owner 销毁本轮 test DB/Redis/九桶及临时资源，清理日志、报告、截图、测试缓存和专用浏览器；保留 dev、D-7 来源、质检/复核记录及原 ready 冻结版本。完整 pre-commit 通过；首次 hook 仅自动排版路线表格，暂存格式化结果后重新完整通过。
- 剩余：运维告警/日志关联与跨节点失败分类仍需独立合同；E12-B 分项归因、配额和一致恢复，E12-C 规模基准、E09 的 E11/E07 维护影响预览仍未完成。本次记录不是实时读取证明，也不提供物理容量或完整源索引字节。活动计划继续保留，不归档整套 Epic。

### E12-B1 离线一致备份与隔离恢复交付

2026-10-24 完成，仅资格化本机离线合成试点；生产 RPO/RTO 未定，整项 E12 继续活动。实现、正式文档和本记录同次提交，不分配发布版本。

- 交付：私有完整备份代次、最后发布完成标记、流式 SHA 与 metadata、全部九用途及共桶范围、逐项预算和无版本历史门禁、独立演练密钥；源销毁后 full restore 至随机空目标，迁移头匹配后授权，六类物理 owner 白名单映射并保留17项变换账本。旧队列不恢复、不启动应用/调度，未完成任务明确待核对。
- 纯测试：30个不同用例通过，包含备份与恢复的完整性、预算、分页/中途变化、空/共享桶、私有文件、完成标记、篡改、metadata、错误归属/非空目标/未知客户端、密钥类型与身份、优化模式拒绝、六类映射及拒绝前零变更。首次29项及新增空目标复查1项分别通过，不重复累计；均不连接业务基础设施。
- 最终真实演练：代次 `a769013d-6b7e-4b38-856f-86ac140337c8`，迁移头0213，145表71行、九桶19对象4700字节。备份源库/源桶/Redis及外部文件销毁、源dump和旁路事实文件移除后，只使用完整代次与独立密钥恢复。原始全库指纹一致；映射后的受保护业务指纹一致，连接密文成功解密，完整缓存与预览回读校验通过。
- 业务验证：1个ready版本的3张冻结图、标签、manifest、ZIP、来源证据与Native输入校验通过；1个已有部分媒体的running版本保留未完成，无伪造清单/包。两条外部引用索引保留，缺失原件严格拒绝；旧路径重建同名不同内容也拒绝，冻结内容仍独立可验。目标Redis为空。
- 用时与窗口：源准备20.529秒、备份3.197秒、源销毁1.508秒、DB恢复13.605秒、对象恢复2.825秒、应用核验3.169秒；含归属/清理的总用时47.284秒。已知停写期间写入为0，仅适用于无服务的随机合成源；不能据此宣称生产数据损失窗口或恢复承诺。
- 故障：源准备后注入中止、对象篡改、错误密钥、坏dump及DB恢复后中止均按预期退出1并验证清理。坏dump明确返回 `dump_integrity`，26.286秒结束并完成清理；恢复中止场景先销毁源再恢复目标，证明失败清理不依赖源。单元补充完成标记前DB回调失败、坏dump及缺key等边界。
- 文档与检查：新增 `docs-site/ops/deploy/storage-recovery.md`，同步部署两页、运维索引、DEV、README、CHANGELOG Unreleased及整套队列。Ruff检查/格式、文档frontmatter/导航/严格可读性及构建通过；仅有既有大包和未加载代码语言的构建提示。无应用UI/API/Worker/schema改动，不重复浏览器矩阵，不重建镜像。
- 清理与保留：演练正常/失败均按owner销毁随机DB/Redis/九桶并清理私有目录，最终清理状态verified；任务日志、报告、构建和Python缓存在记录后清理。当前dev 3100/8100、两个Worker及beat保留，服务只读核验；未向人工业务数据写入夹具。
- 剩余：生产停写与删除保护、保留策略/PITR、恢复应用上线/异步owner处置、规模基准/告警和实际外部来源恢复仍需独立验收。此切片不提供通用生产恢复命令。接下来复核E11/E09维护影响与受控生命周期，不归档仍活动的E12。

### B1追加验收 — 图片独立交付责任

E02-B13-B3-H1新增实际inline组件的schema3图片恢复夹具，精确选择已核验Dataset图片和当前TaskOriginal，不纳入历史未知原件。先销毁源后，随机目标恢复166表196行和九桶83对象（31107字节）；两套bundle的四份媒体副本、两个ZIP及ready／真实中断building责任完整核验，冻结输入、历史URL和七天共同截止保持，准确缓存索引失效。夹具明确未经过HTTP／Celery队列；实际队列验收另见E02 H1 Outcome，二者不互相冒充。恢复工具四文件80项纯测试通过，随机资源清理已独立确认；此结果仍限本地离线合成资格，不提供生产保护、RPO／RTO或旧URL自动迁移。

### B1追加验收 — 视频独立交付责任

E02-B13-B3-H2 增加真实生成的视频、Dataset／TaskOriginal 条件创建及 schema4 ready／中断 building 的 inline 组件夹具。源销毁后恢复 166 表 216 行、九桶 92 对象（68278 字节），逐字节核对四份视频副本、两个 ZIP 与四份探测证明；完整原始创建／来源事实、冻结输入、首次截止和历史 URL 保持，恰好一个 schema4 缓存索引失效，不重新探测、回源、签名或自动恢复执行。七文件 105 项恢复纯测试及最终随机资源清理通过。实际 HTTP／独立 Celery／MinIO 验收另见 [E02 H2 Outcome](1792661016_dataset-storage-locations-connections.md#e02-b13-b3-h2-outcome--视频冻结导出与独立媒体交付)，不以此合成恢复代替生产保护、实际站点或 RPO／RTO 资格。

### E12-C1：真实目录扫描资源与取消恢复基线

2026-10-25 基于 `d30be6e17` 完成工具与本地真实测量，本节与实现同提交。对应原始 E12-S05／AC05 的目录扫描资源、取消恢复及有界查询证据；没有改动生产扫描算法、预算、API、迁移、依赖或镜像。新增 `benchmark_dataset_scan.py`、独立观测 Worker bootstrap 和八项辅助测试；复用现有工作树完整资源核验，普通 Worker 移除迁移／测试 DSN，拒绝非空业务库。测量报告绑定两份工具 SHA，运行期间变化拒绝出具结果。

正式一次性 e2e／真实 Celery／PostgreSQL／Redis／ext4 完成六个场景、七次扫描执行。1,000／10,000／40,000 文件分别用时 0.611／3.959／16.189 秒，采样 Worker 峰值 RSS 242.4／244.1／244.5 MiB，启动基线 237.4 MiB；全部路径与类型、排除计数、无 DatasetItem、原文件不变准确核对。Worker SQL 分别为 307／2,161／8,341 条、0.175／1.102／5.118 秒；遍历与 stat 包含最终目录重验，不把逻辑 stat 当系统调用。四万档 SQL 执行占总时长约 31.6%，保留为后续定位线索，没有据此直接认定生产瓶颈或开启优化专项。

取消在已捕获十一目录／3,018 行检查点时发生：事务 36.1 ms，93.9 ms 后观测到真实收尾和零源 FD；晚到提交未增加检查点，旧 attempt 围栏拒绝。恢复先重验十一目录，再枚举九个目录并最终验证二十目录，3.154 秒完成相同一万文件，零漏项／重复。两轮累计 52 次遍历、24,055 次 stat，明确有重复工作。5,001 项平铺目录在 0.510 秒因 `directory_too_large` 失败，50,001 文件在 14.963 秒因 `scan_budget_exceeded` 失败，均不伪装完整；保留原 5,000 项／目录、50,000 项／轮和 5,000 目录／轮预算。所有执行的普通源文件打开与收尾源 FD 为零；六档实际索引列表处理均至多25项、十条SQL、零物理来源打开／遍历。

首轮工具校准发现平铺夹具重名覆盖、计时起点／p95 和 Redis 归属检查缺口，修正并重建一次性环境后完整重跑；校准数据不计为正式结果。八项 helper 单元测试及子案例覆盖真实5,001文件、同名／排除项、保留元信息的正文篡改、stat观测和错误连接／owner凭据拒绝；正式全场景 exit 0，独立只读复核确认脚本SHA、口径和结果。Ruff／格式、文档 frontmatter／导航／严格可读性／构建与 diff 检查通过。没有应用UI变更，不重复浏览器或旧功能全量验收，未运行远程CI。

正式知识位于 `docs-site/dev/reference/dataset-scan-benchmark.md`，DEV 与 Celery 排障已链接复现／清理命令和实际限制。结果是共享主机上的本机合成暖目录、单 Worker；RSS是带查询间隔的采样，VmHWM为累计高水位；不代表协调进程、数据库、慢文件系统、原图读取、同步、训练准备或生产SLO。原图读取、同步和训练准备仍属于原始 S05 的其他测量，完整 E12 保持活动，不因本片增加新的发布前置。

校准与正式 e2e 资源均按归属销毁，最终数据库／Redis／九桶不存在；临时树、Worker、测试夹具、日志、报告和构建缓存清理。人工3100／8100均200，十二表、24对象、十二源文件完整指纹不变，原Worker／Beat保留。后续返回整套原始Story／AC矩阵选择剩余本地缺口，不启动Tracker、会话或审核专项。

### E12-C3：平台当前对象分项计量

2026-10-25 基于 `b848ee1c2` 完成原始 E12-S03／AC01 和 E09-S04 对应的六分类计量。复用现有 AsyncJob、分页 LIST、检查点和成功快照，只增加分类 helper、快照 JSON 与准确定位索引；同一 endpoint／bucket／key 只计一次，实际 Size 与预约／业务大小分开。规范路径只查询候选，归属仍由已有保存地址及纯 helper 验证；恢复后的 Task／辅助原件使用当前独立定位，不改写创建历史。跨分类冲突归入未归类，物理字节始终保留。

分类累计与检查点同事务，完整桶的分类、总数、字节和测量时间一并发布。旧检查点／旧快照不补造分类，读时拒绝总量或时间不匹配的 JSON；列举或分类失败保留原成功值。准备中、到期、退休及业务已删除但仍有物理字节的归属继续计量；训练版本计整个保存前缀。GET 只聚合成功快照，不查询物理源，不改消费者或清理许可。

58 项不同相关 API／DB／迁移用例、27 项 Web 和 4 项 SDK OpenAPI 合同通过；覆盖全部16种引用 helper、独立原件恢复映射、同类去重／跨类冲突、保留／删除意图、两页累计、失败保留及旧证据。迁移 0242 往返保留旧桶总量，升级后的旧分类为 null。Ruff、Web tsc／完整 lint／CSS token、生成合同、文档 frontmatter／导航／严格可读性与构建通过；Web仅有既有四项 lint 警告及训练比较页两处样式建议，不冒充远程 CI。

真实隔离 HTTP／独立 Celery／MinIO 盘点九桶、八对象、98 B；原件3、预览／缓存12、训练版本24、临时17、其他19、未归类23 B，分项之和准确等于总量，三个连续 GET 保持同一快照。真实浏览器实看1440／390、明暗主题，六分项与采样窗口清晰，无页面或卡片横向溢出；正文13px、提示11px。分类口径展开／收起、两次刷新通过，捕获请求无盘点POST、HTTP错误为零，控制台零error／warn，页面错误为零。

人工环境通过所属监督单元应用0242并刷新两类Worker，恢复固定3100／8100及绑定Beat；实际API派发的盘点完成，24对象／38,911 B与分类之和一致。十二表155行、24对象及十二源文件完整指纹保持。隔离test／e2e资源与本轮产物按归属清理；不重建镜像，不改依赖或版本。

正式知识同步README、CHANGELOG Unreleased、用户存储说明、存储位置API指南、`dev/reference/storage-buckets.md`及存储架构。结果仍限当前配置平台桶的当前对象，不覆盖外部来源、历史版本、删除标记、未完成分片、本地临时磁盘或提供方物理开销；未归类保持可见。仅此分项完成，完整E09／E12继续按原始矩阵推进，不增加Tracker、会话、审核或生产站点的额外验收前置。

## E12-C4：目录原图读取资源基线

2026-10-25 完成原始 E12-S05 的目录原图测量分项。新增可重跑的隔离基准、观测 bootstrap 和正式[实测说明](../../docs-site/dev/reference/dataset-read-benchmark.md)，复用现有扫描／严格登记、JWT 和真实 HTTP 原图接口；不修改生产读取、预算、授权、Worker、依赖或镜像。先实际复现综合健康检查因无 Celery Worker 返回503，而数据库／Redis／MinIO均200，再将工具就绪检查改为这三项实际依赖，不为测量额外启动Worker。八项辅助用例通过，覆盖该就绪边界、真实编码／摘要、计数与FD分类、线程上下文和失败收尾。

最终一轮125次真实读取中124次完整响应逐次核对SHA／长度，一次在大PNG完整核验后只接收64 KiB即断开。三张图片为2,360,909／1,862,268／25,175,958 B；各20次负载的单路完整响应p95为89.67／102.89／318.95 ms，四路为317.90／214.49／724.85 ms。每次SQL cursor执行65次；内容读取会话、实际read调用／字节、SQL／解码分位、CPU及资源采样均保存并解释。四路最大档匿名临时文件峰值100,703,832 B，API RSS采样峰值522.72 MiB，各负载结束源常规／目录FD及匿名临时FD／字节均为零。断开后实际收尾5.57 ms与之后16.52 ms的观察延迟分列；后续完整读取成功，源文件前后SHA一致。

结果限本机共享Xeon／112逻辑核、ext4、三张真实编码的合成图片、暖文件缓存、无持久原图缓存与同机TCP；采样峰值不代表资源上界，响应体断开不代表源读取中取消。固定查询数量和并发尾延迟如实保留，不自动扩展优化、Tracker／会话／审核或性能门槛；同步、训练准备和真实部署测量仍按原始要求分别推进，完整E12未标完。

Ruff检查／格式、Prettier、文档frontmatter（203文件）、导航覆盖（205页面）、可读性及完整构建通过；构建只有既有promql／env语法高亮回退提示。最终差异检查与正常提交钩子纳入提交验证。本片无产品UI变更，不重复浏览器矩阵或SDK／API全套验收。

一次性e2e按准确owner停止／销毁，独立核对数据库、Redis与九桶不存在，资源清单和工具临时树移除。人工3100／8100均200，两Worker／绑定Beat正常；12表155行、24对象及12源文件完整指纹未变。清理本轮测试报告、日志、脚本、构建和检查缓存，只保留有界核验摘要；不移动人工环境或原件。

## E12-A3 Outcome — 本地来源故障分类与关联

2026-10-25 完成原始 E12-S04／AC04 的本地来源分项。复用现有 AuditLog，以 `storage.fault` 区分配置根不可用、存储认证／访问拒绝、实际原件变化与预览生成失败；只保存已授权解析的关联 UUID、受控阶段／错误码及安全 request_id。来源诊断仅向超级管理员提供现有审计页的来源过滤链接，没有新增页面、查询接口、表、迁移、服务或队列。

旁路观测使用当前调用者 Engine 的独立有界事务，不提交调用者业务，回滚后仍可保存已发生的故障；观测失败不改变原读取异常。实际配置根与服务认证拒绝点分别标记，普通缺失文件、错误客户端 SHA 和业务授权拒绝不冒充存储故障。扫描仍先核对当前 task／fence，旧投递不生成新事件；预览失败仍保留已核验原件。

109 项不同后端用例全部通过（新观测专项11、SFTP分类2、相关既有回归96）；17项相关Web用例通过。实际文件内容变化与真实MinIO认证拒绝产生可查询事件；独立Celery Worker在受控根离线时失败，记录准确位置、来源、轮次与Job。SFTP服务异常分支使用替身验证，不宣称真实远端部署。

实际浏览器验证来源链接、审计动作／来源筛选及详情；1440×1000与390×844的明暗四组合均检查截图，无页面横向溢出。控制台零error／warning、页面异常为零；已捕获响应无HTTP错误，23条请求未捕获响应状态不表述为已成功。人工3100／8100及两Worker／绑定Beat已恢复健康，每Worker注册73任务，实际扫描空任务派发通过；12表155行、24对象完整字节及12源文件指纹前后一致。一次性test／e2e数据库、Redis及各九桶按归属销毁，独立核对不存在并清理任务中间产物。

正式知识同步README、CHANGELOG Unreleased、用户存储说明、超级管理员审计说明、API存储位置指南及开发审计架构。Ruff、CSS tokens、文档frontmatter／可读性和完整构建通过，正常提交钩子及最终差异检查纳入提交验证。当前仅覆盖来源核验、引用图片读取、扫描失败和预览生成，观测库失败可能缺记录；没有扩张Tracker M2、会话、候选审核或其他消费者专项，也不代表远端站点、通知渠道、完整E12或整套Epic完成。

## E12-A4 Outcome — 来源故障去重与恢复观测

2026-10-25，在`d61ade58c`的独立工作树完成四类既有来源故障的持续状态、去重和恢复转换。后端检查点`6c1da7345`、控件`15a1f8cf3`及最终边界修复`db22b15cb`均正常提交。0249接0248，增加稀疏状态表和数据库序列，无历史回填；旧审计保留。健康文件不建状态行，状态与首次／恢复／重新发生的转换审计同事务；持续失败只累计接收次数、最近时间和安全关联。

四条既有链按实际来源、节点、配置、操作／目标及内容／预览依赖取得I/O前序号，旁路事实查询和记录均使用当前Engine的独立有界事务，不提交或破坏调用者事务。已恢复水位拒绝重放和旧结果；首次失败晚于无行成功落库时保守待下一次同范围成功，不声称实时健康。原图缓存、探针、邻文件、新内容、旧扫描投递及撤权不消警。未执行旧内容核验的显式刷新不恢复旧期望故障；扫描在对应目录提交后恢复，准确缓存预览的完整核验也只在最终登记提交后恢复，历史重放不恢复。没有可用观测token／准确内容范围时保留A3安全故障证据，不补造健康状态。

只读分页GET按当前manager、Key、位置和来源裁剪，行与计数同范围；最终角色变化拒绝已按旧角色查询的结果。项目管理员不可见孤立来源，超级管理员可见其安全记录；已删除来源不生成旧Dataset链接，不可见Job不返回链接。GET只读数据库，显示保存范围匹配／历史／未确认，不探源。既有来源诊断新增状态筛选、时间、重复数、角色处理方和已有任务／审计链接；没有自动指定个人、认领、通知或强制消警。

不同后端／数据库／迁移用例共131项：130通过、1项原有断言失败、无跳过；聚焦重跑只覆盖上述集合，不重复累加。新增状态26项、迁移2项均通过，涵盖稀疏序号、重放／逆序／再发生、准确范围、真实事务与审计回滚、独立观测SQL失败、缓存／预览发布、角色降级、Key范围／撤权和安全证据。受影响内容、引用、扫描、S3／SFTP分类、真实扫描并发及历史来源回归包含在总数内。保留的`test_directory_media_never_uses_legacy_urls_or_pyramids`原断言期望`ImagePyramidError`，现有路径实际抛出`ContentError(directory_cache_not_supported)`；冻结`d61ade58c`生产模块精确重现同一失败，两个pyramid实现无差异，不扩展渲染器或消费者修复。另24项不同Web用例、4项SDK合同通过；类型、完整Web lint／CSS tokens、OpenAPI／生成类型、文档frontmatter／导航／可读性和完整构建通过，仅有既有lint建议与高亮回退提示。Ruff／格式、正常提交钩子及差异检查通过。

实际独立HTTP／Celery环境中，冻结目录根两次离线读取、两次扫描失败分别形成API与Worker各一条根故障，接收数均为2。恢复相同dev／inode根后，完整图片SHA保持，API读取只恢复API行，Worker行继续未恢复。按所属环境刷新最终Worker代码并重取既有能力证明后，探针仍保持未恢复1／已恢复1；真实新扫描完成目录提交后变为未恢复0／已恢复2，证明节点分离及探针不消警。此实测限本地固定目录，其余准确范围和失败窗口由相关数据库回归覆盖，不冒充远端站点资格。

真实浏览器逐张查看1440×1000／390×844明暗四组合截图，13／11／15px层级一致、无横向溢出；未恢复／已恢复／全部、刷新、单页分页禁用、任务弹窗Escape／焦点恢复、转换审计关联、来源深链与实际返回均通过。捕获1079条网络记录：823条200、208条304、12条204、36条未取得状态；故障查询8条200及4条无状态，无已捕获HTTP错误。控制台零warning／error、页面异常零；无状态记录不表述为成功。

所属`7e4e2feb1f7b501b`的test／e2e数据库、两个Redis及各九桶已按准确owner销毁，独立核对全部不存在，18120／13120已关闭；JWT、浏览器状态、样例、原始日志／JUnit、临时脚本、截图及检查缓存清理，仅保留有界摘要和待合入Git树。正式知识同步README、CHANGELOG Unreleased、用户存储指南、存储位置API和开发存储说明，SDK将此管理面GET明确排除。隔离实施期间未修改人工3100／8100及0248环境；后续主树合入、人工0249及Worker刷新已完成，见[主树集成与人工环境](#主树集成与人工环境)。A4这四类本地观测分项完成，完整E12仍部分交付；不增加训练、规模、Tracker、通知渠道或其他消费者验收前置。

## E12-B2 Outcome — 外部原件保护责任与恢复保留

2026-10-25 完成原始 E12-S07、§8.1及AC08的责任登记分项。DatasetSource以独立JSONB和修订保存责任方、四种恢复方式、可选内部编号及服务器登记主体／时间；旧来源保持未登记。新增PUT沿原来源管理／Key权限和Location→Source锁，独立修订冲突返回409，不访问物理来源、不改变内容／策略修订、generation或活动作业。停用、注销、归档和孤立来源保留声明，孤立源仅超级管理员可见；声明不授予备份验证、读取、清理或恢复许可。

Web提供紧凑摘要及居中弹窗，位置来源诊断复用同一只读摘要。实际浏览器完成首次保存、另一HTTP请求更新后的真实409冲突、载入最新记录及再次保存，最终声明修订3，来源内容配置不变；孤立源责任方和无恢复方案可见。1440×1000与390×844明暗四组合检查截图且无横向溢出，Escape返回编辑按钮；控制台零error／warning、页面异常为零。网络捕获的一次409为主动冲突，另16条无响应状态不宣称成功。

21项不同新增后端／迁移用例通过；88项相关既有回归通过，1项目录金字塔旧用例失败：`test_directory_media_never_uses_legacy_urls_or_pyramids`期望ImagePyramidError，当前原件pin抛ContentError。对应测试、image_pyramid及image_pyramid_originals均未在本片修改，未执行基线用例，不将其表述为已验证的基线失败或本片已修复。24项Web、4项SDK合同、11项恢复纯测试通过；首轮新Key测试字段名错误修正后对应一项通过，不重复累计。独立只读复核未发现阻塞。

实际离线演练先销毁源，再恢复166表220行、九桶97对象；责任声明全部字段及修订受完整／保护指纹约束并保持，两个缺失外部原件及两个同名替代均拒绝。声明记录外部备份方案仍不把外部原件算进九桶备份。实测全流程116.878秒，限本地合成演练，不作为生产RPO／RTO资格。

人工环境已迁移0244并刷新所属监督服务、两个Worker与绑定Beat；每Worker注册73任务，实际扫描空任务派发通过。12表155行既有业务列、24对象完整字节及12源文件指纹保持；新增两列另核对为0／空，未虚称schema前后逐字相同。一次性test／e2e数据库、Redis和各九桶按归属销毁，演练两套随机资源独立清理。正式知识同步README、CHANGELOG、用户存储说明、API来源指南、开发存储架构及运维恢复说明；OpenAPI／类型和API目录生成物、SDK覆盖分类已同步。Ruff／格式、CSS tokens及文档检查／构建通过，正常提交钩子与差异检查纳入提交验证。本片未开展Tracker M2、签名端点兼容或生产备份工程，完整E12与整套Epic保持活动。

## E12-C6 进行记录 — 工具与真实链路校准

2026-10-25，独立观测bootstrap集成`0fd687027`，协调器、九项helper和正式[训练准备参考](../../docs-site/dev/reference/dataset-training-benchmark.md)随本记录交付。共33个不同辅助检查通过，包含上下文／原生Body委派与真实close、SQL／S3成败、普通账号归属拒绝、精确ZIP字节／标签、阶段超时收尾及不完整观测不能伪报完成。独立只读审查修正默认相对`/minio`地址依赖Vite的问题，仅在测量子环境使用直接S3签名，不改共享.env。

8样本真实JWT HTTP／PostgreSQL／Redis／Celery／MinIO校准通过：预检1.043秒／782 SQL、创建1.271秒／1,100 SQL、Worker5.371秒／883 SQL，读回5,802字节完整包两次并核对19个ZIP成员；所有观测上下文收尾。原Dataset／Item／Task／Annotation／关联完整行与原件字节保持，所属API／Worker已停止。随后增加的观测完整性断言沿校准实际指标核对为真，阶段等待预算及取消守卫另经辅助检查；未重复已通过校准。

S3操作和正文计量限当前及已传播上下文，独立SDK Transfer线程不计入；上传输出由最终包／对象内容独立核验，不虚报全网络流量。0.5秒采样wall是观测窗口，保留三种重复内容、近似分区及本机暖存储限制。千张／万张仍待实际终态，不将工具／小校准称为完整C6或E12-S05完成。

校准test按准确归属停止／销毁，独立doctor确认数据库、Redis及九桶均不存在；清理原始报告、日志及检查输出，仅保留私有有界核验摘要。长期人工环境与C5原进程保持。

Ruff检查／格式、206文件frontmatter、208页严格导航、207文件可读性、22参考页链接和三计划104条相对链接通过；完整文档构建38.75秒，仅既有env／promql高亮回退。补上新参考页站内入口后严格导航通过；普通提交钩子与最终diff检查随提交执行。校准后仅将客户端响应计时与观测落盘等待分开，九项协调器检查重验通过，不重复累计。人工3100及8100数据库健康均200、doctor整体健康且两个Worker在原监督会话，未重启。独立bootstrap代理干净树经patch等价核对后清理，构建／检查缓存清理。

## E12-C5 Outcome：本地增量同步资源基线

2026-10-25，同一原进程完成1,000／10,000文件各两轮，共四轮实际Celery同步；进程正常退出，六份冻结工具SHA完全保持，未改生产同步、授权、预算或代码。大档采纳准备2,533.302秒单列，无变化轮933.155秒／CPU578.71秒／1,176,509 SQL（302.353秒）／405任务；纯新增100文件轮968.287秒／CPU604.95秒／1,211,882 SQL（313.235秒）／415任务，采样RSS峰值375.80／378.62 MiB。千文件两轮已交付93.107／96.832秒，不重复运行或累计辅助检查。

全部旧Item／Asset／采纳修订身份及源树字节／元信息保持，新增成员恰为10／100。万文件两轮各重读15,681,860 B旧原件；新增100文件实际155,360 B，经过301次内容读取累计466,175 B，全轮16,148,035 B。四轮结束源常规／目录与匿名临时FD、临时字节全为零。三种合成PNG、暖ext4、本机共享PostgreSQL／MinIO、受控阶段唤醒和单次测量限制保留；大档新增与C6及子树有限检查重叠，不作独占主机或生产定时SLO资格，不自动扩展优化工程。

11项不同辅助检查和工具交付的文档／钩子证据复用，本次独立复核四轮准确结果及全部工具hash。完整表及复现写入[正式增量同步参考](../../docs-site/dev/reference/dataset-sync-benchmark.md)。仅原始E12-S05／§7本地增量同步分项完成，训练C6、生产调度及完整E12继续活动；没有Tracker、会话或审核前置。

C5所属e2e已精确停止／销毁，独立doctor确认数据库、Redis和九桶均不存在，原Worker及自建临时源树收尾；原报告／进度日志／清理输出已删除，只留私有有界核验摘要。人工3100／8100数据库健康均200，C6独立test仍运行，未重启人工服务或清理其他命名空间。最终结果文档仅作表格及范围更新，检查／钩子与diff结果纳入本次提交。

### E12-C6 千样本实际结果

2026-10-25，原session27154完成1,000样本档并继续万档。夹具98.532秒；预检103.697秒／94,030 SQL，创建121.527秒／127,084 SQL，Worker966.099秒／99,235 SQL。实际read正文分别1,566,860／1,566,860／3,709,056 B，全部1,000／1,000／2,001响应正文关闭、观测上下文完整。来源原件及Dataset／Item／Task／Annotation／关联行保持；父树独立核对当前源码指纹与启动时完全一致，不仅依赖Git工作树状态。

真实训练包575,336 B、1,000样本／2,003成员及独立已知标签精确通过；两次完整下载29.48／26.96 ms且SHA相同。API／Worker采样峰值RSS447.05／264.96 MiB，构建临时目录采样峰值2,145,681 B，匿名临时字节收尾0；指标／日志留至工具退出，不把最终目录统计冒充仍占用匿名FD。观测JSON在最后采样后写出，采样峰值可能小于最终目录字节，已在正式口径说明。

同进程、前档数据保留、三种重复合成内容、暖MinIO及C5／子树有限检查的主机竞争均保留。千档两次小包传输不构成已测主瓶颈，不据此直接启动节点缓存；大包／跨站点没有本片资格。只提交已完成千档事实，万档与整项C6仍未完成；继续同一原进程，不提前清理test或改动冻结源码，也不新增其他Epic的验收前置。

## E12-B3 Outcome：当前对象增量账目与周期对账首片

2026-10-25，基于批准合同及独立B8依赖实施。新增迁移0246（接0245）、四张独立表和Snapshot可空对象代次；首个生产者prediction_inputs在真实I/O前保存持久意图，条件PUT每次实际尝试独立保存回执并关联稳定逻辑组。成功／unknown写入后外部删除再创建的新版本分别保留，412只记本次零增量；精确endpoint／bucket／key／VersionId删除跨owner去重。版本事实与可空当前对象增量分开，业务提交失败、取消、部分删除、收尾失败及账目发布失败不能抹掉事实或编造减量。只读复核修正同client注入、真实关闭先于owner终态、降级空检查加锁及重试事实单调性，最终未留阻断发现；复核不冒充运行证据。

现有LIST任务逐页暂存观测，仅整桶成功才发布以AsyncJob为稳定代次的基线／对象窗口对账，保留两次成功代次。GET只读固定数据库汇总；全平台推算总量仍为null。超级管理员显式保存1–168小时计划（默认关闭、间隔24小时），现有30秒recover负责调度；授权／配置撤销暂停，显式重授权才恢复，关闭不取消活动任务。存储页默认折叠账目明细，控件沿用13px正文和11px次级提示，未接入其他生产者、消费者或生命周期许可。

验证分开记录：原179项目标后端／迁移／相关回归通过；最后修订另有21项账目／producer检查全部通过，与原组按用例身份去重为184项，包含实际PostgreSQL／MinIO、精确版本覆盖与回退、每次PUT意图先于I/O、并发条件创建、未知不能补造、部分删除及真实close失败。并发夹具补齐S3条件创建的原子语义后通过，不修改生产门禁。32项Web、4项SDK合同、TypeScript、ESLint、CSS tokens、OpenAPI检查与生成物、Ruff检查／格式、文档构建及diff检查通过；文档构建仅有既有代码语言高亮提示。没有远程CI结果，不重复累计已通过的旧组。

所属dev环境通过实际JWT HTTP、普通Celery Worker和独立30秒Beat完成九桶三轮：完整基线0B；条件创建20B后账目+20、LIST20B；精确版本清理后账目-20、LIST0B，最后一轮已知差异为0。初次夹具未启用已有版本控制要求而被拒绝，保存的一条unknown如实保留：创建轮对应不确定对象1、完整差异null，未回填为成功或零；后续删除轮无新不确定，累计unknown仍为1。实际撤销所属测试管理员后Beat暂停为access_revoked，浏览器401清空缓存并返回登录；恢复账号不自动恢复计划，显式重授权后再盘点。浏览器最终关闭计划并保存2小时间隔成功。活动任务不重叠、配置变更暂停和权限边界由目标用例验证，不声称额外真实并发竞态资格。

实际浏览器检查1440×1000及390×844的明暗四组合，查看截图并操作展开、间隔与开关／保存；无横向溢出，提示位于控件下且关联aria-describedby。正常流程无页面异常或新增控制台error；撤权401属于预期拒绝，不虚称所有网络请求均成功。实际Worker／Beat与浏览器轮次在最后幂等修订前已完成，后续只复验相关账目用例，未重做无关工作台或历史消费者验收。

所属Beat、监督API／Web／两个Worker及浏览器会话已停止；dev和test均按精确confirmation销毁。随后独立连接检查两库、两Redis及各九桶全部不存在，两个资源manifest移除；只清理本树命名空间，人工3100与8100数据库健康均200，未重启人工服务或触碰仍在运行的主树C6。任务私有凭据、日志、截图、JUnit与构建／测试缓存清理，仅保留本节有界结果。UI子树实际提交经六文件payload等价与本树检查后清理，功能提交保留。

原始工程第2项仍是部分覆盖：其他平台物理写删入口尚未接入；历史版本、删除标记、multipart和提供方磁盘开销不计为当前对象字节。旧应用回退必须保留schema，先关闭周期计划；迁移降级仅在五表受同事务锁保护且新增流水／代次为空时允许，禁止丢账。完整E12保持活动，不新增B8、E07、训练、Tracker、会话或审核前置。

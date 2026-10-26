# EPIC-07：对象存储选型与 MinIO 退役迁移

> 状态：源端清单、已保存定位目录及初次复制分项已验证；B3已补齐当前九用途的27类定位目录。E09-B17已接通配置源清单后台任务，E07-B4／E09-B18已交付应用初次复制、持久检查点、续跑及选择重试。E07-B5已交付后续清单增量批次、准确版本映射复用及跨批次证据保留；B6已交付耐久迁移窗口、登记访问排空及绑定任务的受控源只读。B7-A已交付受控本机MinIO原生写入封闭，105项不同检查及非空真实源演练通过；B7-B已完成窗口绑定与最终清单／增量封存，234项不同检查和真实HTTP／独立Worker／非空源链路通过。B7-C已完成已封存映射发布与托管原件实际读取，A离线及B同键新版本／删除标记下的HTTP／Worker／浏览器验证通过，撤回等待真实GET收尾。B7-D已接通训练保留产物纯读取与导出ZIP；B7-E已接通头像、附件、审计、RLE及独立导出媒体，准确版本离线读取及真实撤回排空通过。B7-F已接通媒体缓存准确副本、同源HEAD／Range及旧直接签名，原图缓存保持C授权并以跨位置owner责任保护，真实A离线及Range撤回排空通过。B7-G已完成暂存读取与混合写边界，B7-H已完成九用途同事务切换及带真实B新对象的受控恢复演练。锁定双候选本地比较已完成并保留一项实际差异；完整选型、生产迁移、观察与退役仍待各自条件，完整E07活动，不分配发布版本。

## E07-B7-H Outcome：九用途原子切换与保存新对象

2026-10-26，本地受控分项完成，对应原始§7.7／S04及§8／AC08。原生Journal `3186452f4`、模型／0264与组事务 `aee8ee085`、真实夹具 `1bb666b77`、可信运维CLI及正式文档 `78b47fce2`、夹具权限／签名Host修正 `fe3e2606e` 和已有媒体运行时接线 `ae67cb9eb` 已正常提交。完整E07仍活动，累计归档仍为9／12，不分配发布版本。

预览沿九用途既有能力探测签发短期审阅；激活在单一数据库事务比较完整向量、更新全部绑定并保存耐久组责任。历史owner、准确保存地址、SHA／大小／版本保持。提交未知按原身份查询与恢复，不重新递增代际。受控恢复先排空并物理封闭B，保留独立rollback claim，再整组恢复A分配；代际继续增加，A按保存release UUID恢复，B仍物理只读以保留新对象读取。普通命令不能越过未结束的组责任。没有恢复旧数据库快照或反向删除B新文件。

主树255个不同后端节点全部通过，包括组事务、并发／提交未知、维护窗口及缩略图读取回归；原生／运维CLI／夹具共190项纯检查通过，不重复累加子树重叠SQL节点。固定Ruff及格式、21份改动Python的AST、OpenAPI无漂移和正常提交钩子通过。文档207页frontmatter、208页严格可读性、209页导航及完整构建通过。ready缩略图仅移除来源身份比较中的动态Gate修订／transport因素，当前来源、权限、配置、owner及实际读取许可仍复核；真实B新缩略图随后通过实际读取验证。

实际一次性链路完成九用途复制、最终增量封存、发布和原子切换。正常直传在B产生原件及import暂存，独立Celery以已有固定摘要镜像中的libvips生成media-cache缩略图；当前API和共享Python包只读挂载，没有安装依赖或重建镜像。真实链路共178次API请求；恢复A写入后核对A旧／B新原件的认证内容、缩略图能力及完整SHA，保存回执不变；尚未过期的B PUT实际返回405，普通B release被持久claim拒绝，后续annotations／import分配回到A，九项代际由1到2再到3。此前五轮在夹具权限、默认端口Host或运行依赖接线处未完成，不计为通过；第六轮完整通过，`migration_ready=false`保持。

人工环境通过原启动脚本刷新至0264，原3100／8100、两Worker各78任务及原队列、绑定Beat均健康。新增切换表为空，185张原业务表逐表行数及完整摘要一致；总表数186到187，总行数仍9,237，只有alembic版本行按预期变化。没有对人工数据执行切换或回滚。六轮演练及两个隔离导入诊断的64项Docker资源、8个进程组已独立确认不存在；所属test数据库、Redis及九桶销毁后再次确认缺失。三个修改树已按干净与提交等价证据清理，自有中间材料清理，仅保留有界脱敏摘要。

正式知识已写入README、DEV、CHANGELOG Unreleased、[用户说明](../../docs-site/user-guide/datasets/storage-connections.md)、[架构](../../docs-site/dev/concepts/storage-connections.md)及[迁移运维](../../docs-site/ops/deploy/storage-migration-inventory.md#九用途原子切换与新写入回滚)。用户指出汇报过度围绕回滚，已在运维说明明确其仅为切换故障恢复：正常成功后进入观察和退役，不反复回退。本次原始AC08演练已收口，不再追加回滚专项。下一步仅按原始S06／AC09核对观察、保留与退役；真实目标选型、生产条件和E06／E10独立保留，不增加Tracker、模型、训练规模或全历史归零前置。

## E07-B7-H 定稿：九用途原子切换与新写入后回滚

2026-10-26，以 `00cefcb5a` 为实施基线，承接原始 §7.7／S04 和 §8／AC08。上一轮只完成状态核对与只读设计，不算本片实施完成。本片交付九用途同事务切换、耐久组责任及带真实新写入的受控回滚，不新增全历史归零、全 producer、模型、Tracker、审核或训练规模前置。

正向先无副作用地预览九项绑定并执行各用途既有目标探测，再复核封存 A、已发布历史读取映射和 B 的当前身份。单一数据库事务以完整九项代际比较更新全部绑定，并创建以 Publication ID 为主键的切换记录；任何一项漂移均整组拒绝。保存前后向量与读取发布身份，提交结果未知时按同一身份恢复，不再次递增代际。

回滚采用原稿允许的双端准确定位：`active → rollback_draining → rollback_bound → rollback_restoring → rolled_back`。先令 B 应用入口排空并用独立原生 Journal 封闭 B 写入；先持久保存 rollback claim，再在同一事务恢复九项 A 地址（代际继续递增）。外部恢复 A 前先持久保存唯一 release 操作 ID，随后撤回 A 读取映射、解除最终封存责任、释放原生 A 写入封闭并关闭维护窗口。最后复核全组向量和 A 实际开放结果，恢复 B 应用读取准入；B 原生仍保持只读且保留 rollback claim，不能用普通原生命令释放。已有 B 保存 owner 不改址，旧 B pending 写入须显式重建到 A。

修改代理从本次定稿提交建立独立工作树，分别负责数据库模型／0264／组事务及直接回归、原生 Journal／B 入口核验及纯测试、独立 B 拓扑和真实演练夹具；根树负责受信运维 CLI、整合、正式文档和实际验证。必要 guard 仅阻止回滚中普通恢复 B 写入，以及提前解除 A 最终封存；不改九用途单项切换合同。API／Worker 不获得 Docker 权限。

验证九项全有或全无、并发漂移拒绝、同身份重放、提交未知恢复及保存定位不可变；真实演练通过正常上传和独立 Worker 缩略图产生 B 新对象，回滚后认证读取 A 旧对象与 B 新对象并核完整内容，确认旧 B PUT 被物理拒绝。无需九套完整业务流程。按 runtime skill 使用核验归属的一次性 test 环境，保留人工 3100／8100 和原数据，不重建后端镜像；正常提交、正式文档和所属资源清理后记录实际 Outcome。完整 E07 的真实供应商条件、生产选择、观察与退役仍保留，不能以本片代替。

## E07-B7-G Outcome：暂存读取与混合写处置

2026-10-26，沿原始S04／AC04、AC06–AC07完成本片。Mask暂存`a96642c13`、日志纯读与认证下载`772f068bb`、实际验收`9c44fec03`及正式文档／夹具适配`50dcf57ac`已提交。没有新schema、依赖、镜像或Binding变化，完整E07保持活动。

Mask以保存地址和预期SHA读取准确B版本，schema1映射不访问全局A客户端；preflight最终持锁复核捕获的Publication，原上传门禁、batch、计划及逐项结果保持。终态执行日志完整核验源大小、SHA和执行元数据后脱敏，JSON下载描述明确返回认证内容地址及脱敏摘要，Web沿固定同源路径获取Blob并回收URL；不在任何S3位置新建脱敏文件。权限、终态及原artifact身份在读取前后复核，映射撤回、停止或归属未知返回受控409，关闭不明仍保留责任。非日志签名及显式旧脱敏缓存helper保持原合同。

主树152个不同后端节点和4个SDK合同全部通过，隔离修改树25个Web检查通过，普通提交hooks另完成全Web lint／类型检查。152包含Mask60、日志71和旧缓存写helper21，组间不重叠；新增17／23项分别已在前两组内，不重复累计。旧写helper首轮7过／1失败是共享fixture缺少原产物role，补齐后仅定向重跑失败节点及该fixture的13个log检查全部通过；生产校验未放宽。另一项AL fake缺bind已在干净`007759a62`复现，不纳入本片或新增前置。Ruff固定版本、格式、OpenAPI无漂移、生成类型、207页frontmatter／208页可读性／209页导航及40.06秒文档构建通过。

真实所属API／独立Celery和MinIO完成九用途复制、最终封存与发布后，实际停止A，在B两个对象键制造foreign latest及删除标记。认证API按原SHA完成Mask预检，独立Worker从保存batch完整materialize暂存，终态实验详情／恢复说明、日志描述及认证内容下载通过；原上传owner、batch计划、Run／Attempt产物及相关行原文摘要保持。未执行整批Mask导入或模型训练，不将暂存读取冒充输出写入资格。168次受检HTTP请求完成，无认证及跨项目请求被拒，目标全部版本事实证明没有新增S3派生物。

真实浏览器在1280桌面及390窄屏实际点击日志下载，两次文件均为82字节，完整SHA一致且无原凭据、URL或宿主路径。四种明暗宽窄截图已查看，无横向溢出；627条观测请求中四次下载API均200，无HTTP错误／页面异常／控制台error，六条无状态记录不计成功。随后暂停一次真实B暂存GET：撤回耐久进入withdrawing并等待，放行后拒交，实际Body／SDK和临时目录关闭，才完成withdrawn；新预检、JSON描述和内容端点均409。

旧Creation、预标租约、训练产物创建及export building保留原写入责任，维护窗口排空或取消后显式重建；旧签名／已接受PUT和multipart继续由既有原生栅栏封闭。A停止时准确源清理延期，Creation和Mask暂存源字节保留观察退役，不新建通用GC，不要求历史任务全部成功或unknown归零。

人工环境按原unit刷新，两Worker各78任务及原队列、Beat保持；首次doctor只观察启动中，沿同一进程等待后healthy，没有重复重启。原3100／8100均200，186表9,237行的完整摘要前后相同，活动Job／StorageOperation及Worker active／reserved均零，不重建后端镜像。7项Docker资源、3进程组已独立确认消失，浏览器已关闭；所属test数据库／Redis／九桶均销毁并逐项确认不存在。三个干净、等价整合的修改树及所属生成物已清理，仅保留脱敏有界摘要。

正式知识同步README、DEV、CHANGELOG Unreleased、[存储用户说明](../../docs-site/user-guide/datasets/storage-connections.md#导入暂存日志与在途写入)、[训练日志说明](../../docs-site/user-guide/online-learning/experiments.md#产物位置与历史实验)、[API合同](../../docs-site/api/guides/training-datasets.md#实验产物归属与历史核验)、[架构](../../docs-site/dev/concepts/storage-connections.md#已封存的原件读取映射)及[运维](../../docs-site/ops/deploy/storage-migration-inventory.md#发布托管原件读取映射)。下一片回到原始§7.7／S04及AC08的九用途原子切换和新写入后受控回滚；供应商条件、观察退役及E06／E10继续保留，不追加训练规模、Tracker或其他业务专项。

## E07-B7-G 定稿：暂存读取与混合写处置

本片继续原始 S04／AC04、AC06–AC07：补齐已有摘要的 Mask 导入暂存读取，以及已结束 Run 日志的脱敏下载。暂存按保存完整地址和预期 SHA 选择已封存准确副本，保持原配额、权限、计划、逐项结果及真实关闭；没有映射仍遵循原 A 门禁。日志必须先核验原内容并脱敏，以认证内容端点交付；原JSON下载描述增加认证标识、相对内容URL及脱敏摘要，Web沿同源请求取得Blob并回收本地URL。没有独立派生owner的远端日志不向当前Binding裸写；不暴露原日志，也不把只读副本代理扩成写代理。非日志保留原签名合同，SDK覆盖清单沿现有Web专用分类记录新增端点。

旧 Creation／上传签发、预标输入租约、训练包／产物创建和 export building 均保留保存的写入位置。维护窗口排空实际操作，原生栅栏封闭旧签名和已接受 PUT／multipart；未完成或未知责任保留后显式取消／重建，禁止随当前 Binding 原地改址。ready 文件的迁移读取不等于旧混合任务可继续写入；清理仍按原保存地址和真实责任延期，不把 B 副本当作 A 删除回执。不要求所有历史任务成功、未知历史归零，也不新增通用暂存、GC、Tracker 或训练规模专项。

修改代理仅在当前 HEAD 的独立工作树分别修改 Mask 暂存接缝／回归和日志下载接缝／回归；根树负责整合、必要的共用 helper、真实独立 HTTP／Worker 验证、README／CHANGELOG／用户／架构／运维说明与本记录。无新增 schema、依赖、共享配置、后端镜像或 Binding 变更。沿 runtime skill 使用核验归属的一次性 test 模式，保留人工 3100／8100 和原数据，验证后清理所属产物并正常提交。九用途原子切换、新写入后回滚、观察退役及真实供应商条件继续按原稿单独推进，完整 E07 保持活动。

## E07-B7-F Outcome：媒体缓存准确副本与 Range 读取

2026-10-26，本地分项完成。共用基础`a52d879ae`；缩略图`4c3e4b372`、原图缓存`2eb50ac07`、金字塔／视频`ecd4bb4fe`，主树流式传输、直接回归修正、真实演练与正式文档`c0ca431f5`。三棵修改树均从当时本地HEAD隔离建立，普通提交后以等价cherry-pick集成；另有一棵只读基线复现树，四树均已清理。

缩略图、图像金字塔、视频派生物及ready原图缓存接通已封存准确副本。保存的A归属、原件C授权／策略、owner与代际、SHA／大小和原期限保持；实际B同时登记发布及原owner责任，原图缓存以Entry专用purpose跨位置阻止GC。金字塔／视频只规划本次实际消费成员并复核原计划；schema2能力仍按原逻辑地址签发，由现有响应执行B的准确HEAD／GET／Range，schema1保持准确B直接签名。保存的显式`null`源版本与未保存版本分别查询，源ETag参与定位。原件已发布而缓存仍在另一活动位置时，动态transport不再误判为来源变化；其余来源事实和当前原件路由继续复核。

主树共有556个不同节点通过，另有1个旧删除断言失败：`test_dataset_delete_cleanup_does_not_guess_any_pyramid_bucket`，已在干净`a52d879ae`独立树复现同一失败，不算新修复或新增前置。子树另完成149个既有两族物理读取纯回归，主树不重复累加已覆盖的新增／调用节点。几何错误新检查首轮发现overview坏类型分支尚未统一，修正后该节点通过；最终四种坏尺寸／key均走原inconsistent-ready处理，正常坐标422与代际409保持。源路由独立、显式null和坏复制元数据的直接回归也已补验。

真实一次性链路完成九用途复制、最终增量封存及发布后，实际停止A，在B的目标键制造foreign latest和删除标记。基础迁移API链路189次；三类schema2缓存另经24次HTTP完整GET／HEAD／前向及后缀Range核对，两个schema1直接准确签名实际GET通过。独立Worker实际执行缩略图完整读取、原图缓存完整Snapshot捕获以及金字塔／视频的显式物理读取计划；保存owner／locator事实保持，原图缓存last-access仍按既有语义更新。视频payload是合成传输字节，不声明编解码／播放资格；两族物理计划不代替完整业务HTTP，当前权限／代际与最终交付由对应SQL回归覆盖。

另把真实打开的B Range GET暂停在读流中：撤回提交withdrawing并明确等待未关闭操作，继续读取后发现撤回、拒绝尚未交付字节，实际Body／SDK关闭后才完成withdrawn。取消／超时和关闭不明的双责任保留由相应回归验证；已交付字节不能追回，旧直接S3签名仍遵循原期限。

Ruff固定版本、格式、OpenAPI无漂移、正常提交钩子及差异检查通过；207页frontmatter、208页严格可读性、209页导航覆盖及文档构建38.68秒通过。人工0263／3100／8100与两Worker／Beat已按原unit刷新，两Worker各注册78任务及原队列；本轮刷新前后186表9,237行完整摘要相等，活动Job／StorageOperation及Worker active／reserved均为零，未重建后端镜像。真实演练7项Docker资源和2进程组已独立确认消失；所属test数据库、Redis及九桶已销毁并确认不存在。仅保留脱敏有界摘要，清理自有生成物和私有中间材料。

完整E07仍活动。下一步仅按原稿处置暂存与读取后还需写入的混合流程，再推进九用途原子切换、新写入回滚及观察退役；真实目标选型条件独立保留，不延伸Tracker、训练规模或已停止的业务专项。E06／E10及完整原始矩阵未以本分项替代。

## E07-B7-F 定稿：媒体缓存准确副本与 Range 读取

本片承接 B7-E，仅补原始 E07-S04／AC05–AC07 的媒体缓存消费者兼容：缩略图、图像金字塔、视频派生物和原图缓存。保存的逻辑归属、来源权限、有效期、当前输出／代际和清理状态保持；发布映射仅为本次实际读取成员提供已封存的准确 B 版本，不把整个缓存目录全部迁完列为前置。没有映射仍走原 A 的既有读取与维护门禁。新建、重建、上传和删除继续遵循各自保存位置，不自动改写回执或切换绑定。

共用读取适配增加保存 ETag 的定位约束、同一发布的显式成员集合和调用方异步读取准入；实际 B 上同时保留发布及 owner 专用操作，关闭不明时两者继续可见。schema 2 的浏览器能力地址保持逻辑 A 身份，由现有流式响应执行 B 的准确 HEAD／GET／Range，分别核对完整大小和选定区间、VersionId、ETag、复制创建证据与内容属性。部分读取不冒充整文件 SHA 重算。schema 1 沿既有直接 S3 签名形式固定 B 版本及原有效期，不新增旧版能力协议。

金字塔和视频按本次消费的 manifest／metadata 及所请求成员复核，保留完整库存的既有逻辑校验；原图缓存区分规范原件授权位置、保存缓存位置和实际 B 读取位置，并让 owner 读取操作跨位置阻止清理。映射撤回后拒绝新准入；流式响应在读取前后及交付前复核，发现撤回即停止后续流块，已发送字节不能追回；撤回完成仍等待物理 Body／client 关闭。取消和超时须排空既有物理边界。

验证包括准确版本、原 A 离线、B 同键新版本／marker、HEAD／全读／Range、无映射旧门禁、撤回与取消排空、权限及代际变化、清理与关闭不明的直接回归，使用核验归属的一次性 test 环境和独立 HTTP／Worker 演练。对应文档、正常提交钩子及差异检查后提交，清理自有资源，保留人工 3100／8100；不重建后端镜像，不增加训练规模、Tracker 或独立业务专项前置。整项 E07 的用途切换、新写入回滚、选型及观察退役继续按原矩阵保留。

## E07-B7-E Outcome：其余保留文件与独立导出媒体读取

2026-10-26，原始S04／S05及AC06–AC07的本片读取接缝交付。公共版本／prefix见证`cef2c1e1f`及消费者与真实验收`3ad703562`已提交；三个隔离来源`14e3bcfe8`／`cfe1a5c1b`／`93bb971c9`共18份文件与整合提交逐blob等价。没有新增schema、API、依赖、镜像或Binding变更，完整E07仍活动。

头像、报告附件、审计归档、RLE、评论附件和指南图片以完整保存地址及内容选择准确副本，保留原权限、期限、owner与最终复核。源请求版本和真实B响应分别核验；RLE按stored身份选路再核canonical内容，持久Guard保存A修订，内存路由独立复核。评论／指南／RLE及报告回收按既有唯一owner读取purpose识别B许可。独立导出媒体在三格式共享ready入口接通配套ZIP和逐件媒体，同一Publication及物理修订的完整read-set与封存源prefix全等，最终完成事务持锁复核；新写、创建回读和building流程保持原合同。

共同基础最终89项通过；主树保留文件组合1030通过／2失败，媒体组合197通过／7失败，两组合有2个重复节点。通过节点合并去重为1316。失败合并为8个既有节点，干净`cef2c1e1f`的独立一次性环境同样8项失败；日志捕获节点单独运行可通过，其余为指南上传竞争及媒体退休旧场景。本片不将这些节点冒称已修复，也不把继续修旧业务测试增加为读取交付前置。新增跨B owner-purpose的SQL回收保护通过，所有新增读取回归通过。Ruff、格式、OpenAPI无漂移、正常hooks和diff检查通过；207页frontmatter、208页可读性、导航及41.04秒文档构建通过。

真实所属API／独立Celery／源和目标完成九用途复制、最终增量封存及发布，随后停止A容器，在B七个目标键添加foreign latest及删除标记。头像匿名能力HTTP和独立Worker的avatar／bug／comment／guide／audit／raster／media七类读取仍取得准确副本；RLE真实持久owner完整核gzip与canonical，头像／RLE保存回执数据库前后快照一致，其他五类核验其物理接缝而不冒称完整业务HTTP。审计JSONL行及尾部语义、独立媒体完整prefix和准确新签名GET通过。真实评论GET取得Body后暂停，撤回耐久进入withdrawing并等待，放行后拒交并关闭，最后withdrawn；176次API请求完成，migration_ready仍为false。

只读复核先修正评论固定期限、审计完整行shape及持久回执证据。前两次实际运行在seed阶段停止，第二次脱敏栈确认头像夹具边长不足；修正后第三次完整通过，不将失败计作成功。三轮21项Docker资源及2个进程组独立确认不存在；主树及基线一次性数据库／Redis／各九桶均销毁并再次只读确认。基线树和三棵修改树已在干净、等价整合后清理；1633份所属生成／检查文件及10份本轮脚本字节码清理，仅保留有界0600脱敏摘要。

人工环境刷新前DB无活动Job／Operation，两Worker active／reserved均空。原所属服务刷新后3100／8100健康，两Worker各78项注册及原队列保持，Beat按原BindsTo重新启动；首次doctor仅观察到启动中，沿同一进程等待后健康，没有因短暂未就绪重新启动。原186表9,235行（含alembic版本表）完整摘要前后相同。原数据与schema0263保持，不重建镜像、不推送或发布。本片没有产品界面修改，没有追加浏览器布局或模型训练验收。

正式知识已同步README、DEV、CHANGELOG Unreleased、[用户说明](../../docs-site/user-guide/datasets/storage-connections.md#其他保留文件的迁移读取)、[架构](../../docs-site/dev/concepts/storage-connections.md#已封存的原件读取映射)及[迁移运维](../../docs-site/ops/deploy/storage-migration-inventory.md#发布托管原件读取映射)。下一片继续媒体缓存Range；暂存／混合写处置、九用途原子切换、切换后新写入回滚、观察／退役及选型条件继续按原稿保留，不扩展Tracker、审核、模型或训练规模。

## E07-B7-E 定稿：其余保留文件与独立导出媒体读取

2026-10-26，沿原始S04／S05和AC06–AC07，接通头像、报告附件、压缩审计归档、RLE掩码、评论附件、指南图片及独立导出媒体的已封存副本。只调整保存文件的读取传输，原owner、权限、期限、业务ID、冻结内容和摘要保持。新写、创建回读、删除以及building副本的混合流程沿既有合同；本片不替代后续media-cache的Range、原子用途切换、新写入回滚和观察退役，也不将模型、训练规模或完整业务流程增加为前置。

公共单对象代理同时接受原保存版本和同一路由HEAD返回的准确B版本，始终向B固定准确VersionId，保留真实B响应；其他版本、键、写删、列举及Range仍拒绝。独立媒体的完整性核验另用封存source逻辑prefix的latest-visible键与SHA／大小，与冻结inventory全等；禁止在B共享迁移prefix列举旧owner。只有同一完整封存桶选择migrate时，全部copy路由才可作为完整prefix见证；retain／rebuild不以部分映射证明无额外对象，历史版本也不冒充当前可见键。

RLE按stored SHA／大小匹配，解压canonical身份继续原核验；持久Guard的A Gate修订和B实际读取许可分别记录，最终绑定复核Publication。各业务读取继续保留当前归属／授权／期限、完整SHA和真实关闭，GC按已有全局唯一owner purpose等待B读取，不因原A位置过滤遗漏。头像及附件已有准确源版本保持，包括显式null；缺独立版本的既有内容合同不新增版本前置。

修改范围超过8文件：根树负责公共代理／Publication与定向回归；三个当前HEAD的隔离树分别独占标注文件六服务、公用头像／报告／审计六服务、独立导出媒体storage及共享Worker入口与各自检查。根树统一集成、所属一次性SQL及真实A离线／B准确版本核验、正式README／CHANGELOG／用户／架构／运维说明与清理；不改变API、schema、依赖或共享配置，不重建镜像。正常提交并核对最终diff；保留人工3100／8100、两Worker／Beat及原数据，按实际影响刷新受控运行。实际验证完成后才记录Outcome。

## E07-B7-D Outcome：训练保留产物与导出 ZIP 的迁移读取

2026-10-26，沿原始S04／S05与AC06–AC07完成本片。公共只读接缝`ce39ed0f3`、训练helper`52467fb7b`、读取关闭与无flush复核`843ecf031`、导出ZIP`6ca47191d`及训练读取接线`556efcbc2`已正常整合；真实验收脚本、旧测试替身修正及正式文档随本Outcome提交。没有新schema、依赖、镜像或用途Binding变化，完整E07保持活动。

训练保留包、冻结media／mask、assignment的dataset／resume／initialization、Run产物、preview、评估／模型权重、checkpoint和已映射AL保留PNG沿既有纯读取接缝选路。多件输入逐件匹配完整保存地址和内容证据；mask没有独立大小时采用映射中的已核验大小，不拿图片长度代替。导出ZIP的缓存核验、新签名与最终发布复核同一准确路由，schema1也不能绕过已准备映射。原保存owner、snapshot／manifest／包与内容摘要不变。写入、finish创建核验、清理及脱敏日志的混合写流程保持原合同，没有给只读client增加写能力。

实际B同时保留Publication和原产物读取purpose的许可，ZIP退休按唯一owner purpose等待跨位置读取。公共读取接口内部禁止autoflush，最终可共享锁复核发布及Gate；代理校验目标准确版本、ETag、长度、内容属性摘要和创建元数据，保留真实B响应，不伪造源版本。业务Body已关闭时不重复关闭，异常或取消仍兜底关闭；关闭未知保留两项责任，交付失败会关闭本地暂存结果。

主树去重后472个不同检查通过，另2个原有preview专属条件用例跳过。基础96、ZIP213及训练168通过的分组有5项重叠，不相加；训练首轮166通过／2失败／2跳过，两项失败是旧替身不接已有accounting关键字，修正替身后只重跑这两项通过，生产规则未放宽。训练代理另有185项通过子集；15个旧fake owner／计账db.bind／线程夹具节点在干净`ce39ed0f3`实测同样失败，独立保留，不计本片成功或追加写流程修复门槛。Ruff、格式、OpenAPI无漂移、正常hooks与diff检查通过；正式文档frontmatter／可读性／导航和完整构建通过。

真实所属源／目标、API与独立Celery完成九用途初次复制、最终增量封存与发布后，停止A容器并在B的四个产物键制造foreign latest和删除标记。认证HTTP训练包下载返回原ZIP，独立Worker完整核验冻结图片／mask及导出ZIP，实际新签名GET返回原ZIP；两类业务表完整原文摘要保持。无凭据请求拒绝。另一任务在真实成功GET的Body仍打开时暂停，撤回耐久进入withdrawing并等待，新下载409；放行后原读取拒交且关闭，才完成withdrawn。已签发目标S3链接在撤回后仍按原期限有效，未冒称撤销。164次API请求完成，本片没有改动产品界面或执行模型训练。

人工环境在确认DB无活动Job／Operation、两Worker active／reserved均空后刷新原所属服务；3100／8100及两Worker健康、原队列和78项注册保持，Beat按原BindsTo重新启动。全部186张表／9,235行（含迁移版本表）摘要刷新前后相同。首次真实脚本因私有文件排他创建的夹具错误停止，修正后在新一次性环境完整通过；两次所属14项Docker资源及2个进程组独立确认不存在，test数据库／Redis／九桶均销毁。三棵修改树已按四份来源提交等价整合后清理；私密日志、XML、凭据及构建缓存清理，仅留有界脱敏摘要。

正式知识同步README、DEV、CHANGELOG Unreleased、[用户说明](../../docs-site/user-guide/datasets/storage-connections.md#训练产物和导出-zip-的迁移读取)、[架构](../../docs-site/dev/concepts/storage-connections.md#已封存的原件读取映射)与[迁移运维](../../docs-site/ops/deploy/storage-migration-inventory.md#发布托管原件读取映射)。旧A签名及冻结ZIP嵌入URL仍须原入口只读直至自然失效。独立导出媒体、其他用途产物、混合写流程、原子用途切换、新写入回滚及退役继续按原稿核对；不把训练规模、模型算法或全消费者业务资格加作前置。

## E07-B7-D 定稿：训练保留产物与导出 ZIP 的迁移读取

2026-10-26，沿原始S04／S05与AC06–AC07继续接通已经保存的训练产物和导出ZIP。复用B7-C活动Publication及逐对象准确副本，不改变Binding、业务ID、保存owner、冻结snapshot／manifest／package或其摘要。训练包、冻结媒体／mask、执行器输入及已有训练产物读取只调整存储接缝；导出ZIP的缓存核验与新签发下载接通相同只读计划。独立导出媒体、其他用途产物、原子用途切换及新写入回滚仍按原稿继续，不把本片结果代替完整E07。

公共只读适配器按完整保存地址和SHA匹配；有独立大小时同时匹配，没有mask独立大小时从已核验映射取得，仍沿原业务预算与完整SHA核验。可选源版本限定用sentinel区分未限定、显式NULL和准确版本，已有内容身份合同不新增创建版本前置。代理仅开放该对象的HEAD／完整GET及GET签名，固定目标准确VersionId／ETag，拒绝其他键、写删、列举与Range；校验目标内容属性及三项创建元数据后，仅移除已核实的迁移元数据再交给原有严格业务核验。

实际I/O同时保留B的publication许可和原owner专属读取purpose，关闭真实Body／client后才收尾；依赖该专属purpose的导出回收查询不再仅凭原A位置遗漏B上的读取。调用方原有当前授权、owner与期限、预算、完整SHA及关闭规则保持；返回本地暂存文件在取消／最终路由复核失败时关闭。写入、写后创建核验和删除沿原入口，A stopped Gate不增加例外。

已发出的直接A预签名URL和冻结ZIP中的嵌入URL不会被新数据库路由改写。原有最长七天期限进入原稿观察／退役安排：旧入口保留只读至这些URL自然失效，授权API可重新签发B的准确版本URL；撤回只排空应用内真实读取，不能声称撤销已发出的S3签名。没有独立SHA或固定归属的旧条目保持原拒绝，不要求全部未知历史归零。

修改范围为公共只读适配器／Publication查询及定向检查、训练现有读取接缝与检查、导出ZIP存储／缓存／下载与检查、实际迁移验收脚本和相关现有文档。修改代理使用当前本地HEAD的隔离树及互斥路径，正常提交；根树统一集成、运行所属一次性测试与真实A离线／B同键新版本及marker的HTTP／Worker读取，验证冻结字节和业务定位保持。使用runtime skill管理资源并保留人工3100／8100，必要时仅刷新当前Worker，不重建镜像、不改变共享配置或依赖。验收后清理所属中间产物，记录实际Outcome，不追加训练规模、模型或其他消费者业务资格。

## E07-B7-C Outcome：已封存映射发布与原件实际读取

2026-10-26，沿原始S03／S04／S05与AC05–AC07完成本片。不可变路由合同、完整Copy映射核心、准确版本reader与OriginalSource、十二处原件存储接缝及Publication控制已正常集成至`4ec05c9da`；0263保存发布与逐对象映射。可信运维提供`publish-reads/status-reads/withdraw-reads`，发布重算baseline／全部delta及封存证据并前后观测真实原生栅栏；状态查询只表示耐久数据库记录，源不可达时仍可取得修订续做撤回。

逻辑Task／Item／Asset、保存定位与创建回执保持，实际I/O使用B的Gate／许可及准确目标VersionId／ETag。当前凭据正常轮换可用于新读取，进行中计划的修订变化拒绝交付；物理改址、目标停止、发布撤回及当前授权失效仍拒绝。查询Gate使用只读共享NOWAIT，不flush调用方挂起的业务修改。活动／撤回中发布保留Copy与原生责任；晚到物理ACK继续保存，并使发布进入撤回中，不丢弃事实。撤回完成后历史独立保留，不增加永久冻结旧Copy的新门槛。

实际独立源／目标、API及Celery Worker完成九用途复制、最终增量封存与发布。随后停止所属A容器，在B两个原件键上加入foreign latest及删除标记：两个Task及DatasetItem的五次认证图片／下载均返回原始140字节PNG，独立Worker完整下载一致；六类业务／原件表原文摘要保持。无凭据请求拒绝。另一个Worker任务在真实成功GET、响应Body仍打开时受控暂停，显式撤回提交withdrawing并因真实许可未收尾而等待，新HTTP读取拒绝；释放后原任务拒绝交付并完成关闭，才完成withdrawn，原生claim仍保留。171次HTTP请求完成；本片不将这些原件读取结果替代其他九用途产物、原子切换、新写入回滚或退役资格。

浏览器实际点击数据集原件下载及Data Manager Task预览，认证下载200、画布像素为原PNG的RGB(31,97,151)。检查三张真实截图，覆盖明暗主题、1280桌面与390窄屏；页面无横向溢出，控制台error和页面异常为零。977条观测请求没有HTTP错误，15条无状态记录不计成功；没有改动产品布局或扩展标注业务。

核心124项、消费者原件存储426项、控制与迁移50项及CLI桥接22项通过，组间有重叠不求和。主树额外SQL集成231个不同用例最终通过：首轮230通过，另1个旧fixture给现有read_options传None数据库，在原基线同样不成立；改为真实隔离数据库和canonical legacy原件后仅定向重跑该项通过，生产校验未放宽。消费者旧fake transport fixture仅补已有元数据helper边界；10项保留PNG输出检查不纳入本片，不展开输出或模型专项。Ruff、格式、OpenAPI无漂移、文档207项frontmatter／208页可读性／209页导航、最终40.42秒构建与diff检查通过。

人工环境先应用测试过的0263再集成读取代码，迁移及刷新后原183表9,234行摘要保持，两张新表为空。原3100／8100及两Worker健康，注册与原队列已核对；Beat沿原systemd绑定随主环境重新启动，保留原调度配置与状态，不冒称PID不变。不重建镜像、改变共享配置或写入验收业务。一次性源／目标与进程已按owner清理，test数据库／Redis／九桶已销毁；保留脱敏有界摘要。完整E07与整套目标仍活动，后续按原稿接其他已保存产物的读取、原子用途切换及新写入回滚，不重开全库补认、训练测量或Tracker前置。

## E07-B7-C 定稿：已封存映射发布与原件实际读取

2026-10-26，基于干净`cf0837b88`继续原始S03／S04／S05及AC05–AC07。最小直接替换Binding不能改变历史地址，直接换客户端又会先被旧A Gate拒绝，且可能读B的latest；因此本片发布已封存逐对象映射并接通全部现有托管原件读取接缝。原Task／Item／Asset／TaskOriginal、保存定位、创建回执、冻结original receipt／manifest及其摘要保持；新分配Binding不在本片切换。九用途其他冻结包／派生物的只读路由、原子用途切换、新写入回滚与生产退役沿原稿后续实施，不用原件完成替代。

```text
Journal held + sealed Finalization / Copy
                ↓ actual observation + materialized full evidence
       immutable Publication / exact Route rows
                ↓ logical locator + saved SHA / size
OriginalSource / business authorization → PhysicalReadPlan
                ↓ exact target Gate / durable Permit
        HEAD(version) → GET(version + IfMatch) → full SHA → HEAD(version)
```

新增Publication与Route两表及0263：Publication ID固定为Finalization ID，保存最终窗口／Copy／token／evidence／源和目标、映射摘要／数量、状态和修订；一个逻辑来源最多一项active或withdrawing发布。Route固定准确源object ID、完整endpoint／bucket／原始key／可空source VersionId、独立SHA／大小、源ETag／is_latest，以及目标准确key、非null VersionId／ETag／内容摘要和原创建nonce／plan UUID。保持两端版本分别记录；行不可修改或删除，发布身份／摘要不可改写，有历史时禁止破坏性降级。

可信本地运维复用`finalize_storage_migration.py`，新增`publish-reads / status-reads / withdraw-reads`，继续使用私有config/state、actor-id和finalization-id；发布的expected-revision指Finalization修订，撤回指Publication修订。preflight短事务取得已sealed全量Copy固定输入，事务外重新物化baseline／全部delta及最终证据，Journal内前后真实观测原生封闭，最后短CAS同时写映射及活动发布。直接复用当前清单内已完整verify的全部历史S3版本；previous-only／history-only、retain、rebuild和marker保持原事实，不冒充可读副本，不要求全历史unknown清零。映射按100,000行既有预算，在数据库事务内分批插入；不增加公开API、后台服务、配置变量或证明JSON导入。

Publication独立保留逐对象回执和Copy责任；未撤回前普通Finalization.invalidate拒绝，原有Window／Journal封闭继续保持。显式撤回先提交withdrawing，阻止新映射I/O，再等待实际`migration_read_<publication UUID hex>`许可全部真实收尾，才提交withdrawn；不按TTL清除，不自动开放A写入、不删除目标或原源。原记录与映射继续保留，撤回后不原地重新激活同一发布；后续新窗口/最终核验建立新发布。未知应答用同身份续做，不能换ID绕过。

OriginalSource保留全部逻辑字段，并附独立不可变read_route事实；旧A的maintenance_revision不塞入B修订。解析先冻结原Gate修订，完整逻辑定位和SHA／大小已知后，只有原直读准入或精确active映射及B当前可读才返回。未匹配时沿原拒绝行为，不回退猜测；绑定位置禁用、物理漂移或目标停止均拒绝。原件原本是内容身份读取，旧创建VersionId不突然变成读取前提：按完整逻辑地址及保存SHA／大小选准确映射；多份同内容版本按is_latest优先、source_object_id稳定排序。显式版本合同的其他历史消费者留待按该源版本接入，不能全局SHA或同key猜测。

新增纯PhysicalReadPlan，分别给出实际location_id／maintenance_revision、带目标准确VersionId和目标ETag的ObjectSpec，以及route许可purpose；不伪装SDK端点或回包。`read_options`在实际Permit下取得B当前凭据、复核发布及物理身份，并保留现有原件保护；`recheck_read_route`在读取前后核对活动发布、修订及B配置／Gate，错误沿已有关闭／取消责任收尾。业务授权与逻辑receipt复核原样保留。reader的两次HEAD、条件探测和GET都固定同一个目标版本，并完整核SHA／大小；B出现foreign latest或marker也不改读版本。

本片超过8文件，准确写入范围为新model／0263／发布服务及DB检查，最终核验／Copy必要guard，新映射纯核心及检查，原件reader／original_sources／auxiliary_originals与检查，以及现有12个存储读取接缝：dataset_content、original_binary、dataset_references、prediction_originals、video_prediction_originals、image_pyramid_originals、video_derivative_originals、point_cloud_quality/originals、training_dataset_originals、active_learning_originals、sample_evaluation_originals和export_media_transfer。后者仅调整计划、实际Permit／spec和路由复核，不改业务算法、会话或审核。根树负责CLI／定向检查、独立真实验收脚本和现有README／DEV／用户／架构／运维／CHANGELOG及执行队列；没有产品布局或依赖、共享环境配置、后端镜像及发布版本变化。

实现合同：发布服务提供preflight_publish、publish、status、begin_withdrawal、finish_withdrawal、require_unpublished、frozen_copy及lookup；纯核心`build_read_mapping(values, *, window_id, window_revision)`返回schema、完整evidence、排序entries及mapping_sha256。OriginalSource追加默认None的read_route；`original_sources.read_plan(source)`纯返回计划，`read_options(db, source, *, read_timeout=30)`签名保持，`recheck_read_route(db, source)`无route时不增加I/O。12处调用用plan实际位置／修订／spec及plan.purpose(original_label)，原业务source保持；所有实际读取按原预算及取消规则收尾。

修改代理使用此定稿提交的独立HEAD工作树、互斥路径和正常提交。验证只覆盖本片：映射实际物化与摘要、DB权限／CAS／不可变历史／撤回真实排空、reader固定版本、原件与既有消费者存储兼容回归；独立非空源完成真实HTTP／Worker复制、封存和发布后，源保持stopped并实际不可达，B存在同key新版本／marker仍读准确旧字节，Task／Item／Asset及冻结内容身份不变。真实浏览器验证原件显示／下载、当前授权拒绝和恢复；不把12类完整业务资格或其他所有producer测试变成前置。保留人工3100／8100、原数据／对象／源文件，仅应用测试过的0263并在实际空闲后刷新所属Worker，不重建镜像；按精确owner清理一次性环境和原始产物，正常提交并记录Outcome。

## E07-B7-B 定稿：窗口绑定与最终清单／增量封存

2026-10-26，基于干净`18869a560`，沿原始S04／AC04–AC05及§7第4–6步继续实施。原生封闭已实际成立，但应用任务还没有与它建立可信绑定；本片交付受信任本地运维命令、耐久最终核验记录及现有控制台的只读结果。复用B6窗口与B5后继任务，不给API或Worker增加Docker／root凭据，不接受HTTP上传JSON证明，不新建常驻服务。

运维命令先在原Journal锁内实际验证held，再持久保存带最终核验UUID、窗口、数据库非敏感身份、nonce、原生操作及配置摘要的应用claim；数据库短事务核对当前超管、源／目标、stopped窗口与Gate，确认旧任务和实际操作已收尾后CAS递增窗口修订并登记最终核验。新清单及B5后继沿已有window_revision绑定，无需给每类任务添加新的来源字段。claim跨进程退出保留，独立release与物理重封闭均拒绝绕过；按相同身份续做，不按TTL解除。

在新修订下完成清单、B5后继复制及完整verify后，命令短事务将指定批次冻结为sealing，再释放数据库事务，物化baseline／全部delta并重新核摘要、逐行决策、准确目标回执及逻辑证据；Journal锁内前后实际核验原生状态，最后短CAS核对全部固定身份和修订，才保存sealed。最终源清单须完整、全部当前对象SHA核验、无当前issue／multipart并覆盖全部唯一桶；九用途及共桶策略须明确。当前行只允许已准确核验的copy、明确retain／rebuild以及保留事实的delete_marker；其他skip不能冒充成功。最后Attempt须为完整verify并实际重读包括继承回执在内的目标版本，所有任务和许可已收尾。

catalog历史未知／未展开、mutable null、旧history事实及previous-only对象保持各自计数，不新增全历史清零或全消费者资格。retain不冒称已迁走，rebuild不冒称已执行，已保存报告中的`source_quiescence_proven=false`保持原文；最终记录表示某次观测和核验的封存事实，`migration_ready=false`继续保留。后续历史准确版本路由、原子切换、新写入回滚、供应商选择及生产退役仍按原始范围推进。

绑定期间窗口不能直接关闭；sealing／sealed批次不能新建尝试或后继。晚到物理ACK仍持久保存，改变证据时使最终核验进入待解除状态，不能丢弃事实。显式解除先提交invalidating以禁止新任务并等待实际操作收尾，再提交invalidated，最后清除Journal claim；之后仍须另一次明确release才能恢复原生写入。数据库不可用、提交未知或原生漂移均保留责任。未完成数据库登记的claim只能在相同数据库、锁定原窗口及当前Gate并确认无已登记／未收尾任务时显式废弃；原窗口已关闭不阻断未登记意图的核实解除；已确认登记但记录丢失保持未知，不靠查无记录直接解封。

本片超过8文件：原Journal及定向检查、新最终核验纯验证器、模型／0262迁移／控制服务及检查、窗口与Copy必要guard／DTO、可信运维CLI及检查、现有窗口UI与检查、OpenAPI派生、真实验收脚本，以及API／用户／架构／运维说明、README、DEV、CHANGELOG及执行队列。无依赖、共享配置、后端镜像、发布版本、训练规模或其他消费者专项变化。修改代理均用当前HEAD隔离树提交，根树统一集成并运行所需检查；真实API／独立Worker／非空受控源核对绑定、最终增量、完整目标读回、硬退出恢复、错身份／漂移拒绝与解除顺序。UI按既有居中弹窗、紧凑字号和控件外灰色提示实现，检查明暗宽窄、实际控件、网络／控制台及焦点；保留3100／8100和原业务数据，清理所属一次性环境及原始产物。

## E07-B7-B Outcome：窗口绑定与最终增量封存

2026-10-26，沿原始S04／AC04–AC05及§7第4–6步完成应用最终核验。核心集成`a19e98eed`，窗口控制／0262集成`84399b457`、计数与降级保护`bdbe77386`，界面`d00e857f0`、真实脚本`1bf1ff1d1`；可信运维CLI、真实全量verify合同修正、中断意图解除、界面只读说明及正式文档随本Outcome提交。完整E07保持活动，不分配发布版本。

- Journal先持久保存应用claim，数据库以当前超级管理员、固定源／目标、Gate、配置与父批次核对登记，并递增窗口修订。Inventory和B5后继沿原字段绑定；短事务冻结准确批次，事务外物化全部delta和实际原生观测，最后CAS封存。API／Worker不取得Docker或源root权限，没有HTTP证明导入或新常驻服务。
- 真实链路暴露full verify的三个子集专用字段均为null，已改用当前持久全量summary并与逐对象纯证据对账；合成数据库夹具同步真实合同。绑定提交未成功而窗口已关闭时，显式revision0可在同数据库、当前Window／Gate锁和实际排空下废弃未登记意图；reserved或已有记录仍严格核对。晚到实际回执保留，改变冻结证据会使资格失效。
- 最终234项不同检查通过：耐久／纯证据及CLI共80、同一次PostgreSQL集成88、当前Web66；重复轮次不累加。测试夹具新责任改为rollback，原窗口／迁移及Worker清理用例同次组合通过。Ruff、格式、OpenAPI、生成类型、Web lint／CSS和类型检查通过；文档207页frontmatter、208页可读性、209页导航及最终39.46秒完整构建通过。
- 独立真实API／Celery／受控MinIO／入口完成九用途、12对象版本及1删除标记；新清单沿窗口修订建立最终后继，复用11份原目标准确回执，12版本共98bytes完整SHA读回。提交sealing后硬退出73，再沿原UUID续做到sealed；冻结批次拒绝新尝试，窗口关闭及原生解封拒绝绕过。实际暂停所属Worker并入队旧任务后，解除先提交invalidating、拒绝新读取并等待旧任务真实终结，最后才提交invalidated及清claim；随后独立release和关窗成功。152次受检HTTP请求与源事实保持通过，`migration_ready=false`。
- 控制台真实检查明暗、1440×1000和390×844下的窗口及最终批次共8张截图。计数、观测时间、居中滚动、关闭禁用及外部灰色提示、准确批次跳转、Escape和返回焦点均通过，无页面或弹窗横向溢出。封存批次明确说明最终核验冻结，不虚构后继，所有修改控件隐藏；无页面／控制台错误，615条已观察请求无HTTP错误，9条未捕获状态不计为成功。
- 正式知识写入[用户指南](../../docs-site/user-guide/datasets/storage-connections.md#最终核验记录)、[API](../../docs-site/api/guides/storage-locations.md#迁移维护窗口)、[架构](../../docs-site/dev/concepts/storage-connections.md#最终清单与复制封存)、[运维手册](../../docs-site/ops/deploy/storage-migration-inventory.md#绑定窗口并封存最终增量)、README、DEV和CHANGELOG Unreleased。历史未知／未展开及21条本次history事实继续分列，不追加全历史清零、训练规模或其他消费者资格。

人工环境应用0262，原3100／8100、两个Worker及原Beat保留；所属监督服务已刷新Worker，182张原业务表的9,234行完整指纹保持，新最终核验表仍为空，未在人工环境实际绑定或切换业务。没有重建后端镜像、改共享配置或依赖。

三份真实owner的21项容器／网络／卷、7个所属进程组、原生配置／凭据及临时文件已精确清理，并独立按ID、owner标签及进程归属核实；一次性test数据库／Redis／九桶已销毁，e2e模式保持不存在，四棵源码树按七次提交载荷等价核对后清理。原始报告、检查点、截图、日志及测试／构建缓存随本轮清理，仅保留有界0600脱敏结果。

下一步按原始S03／S04推进历史业务定位到已核验准确物理版本的读取、原子切换和新写入后的回滚。当前封存不是永久物理证明，不授予生产后端选择、真实TLS、多节点、完整E07或物理退役资格；整套已归档9／12的统计保持。

## E07-B7-A 定稿：MinIO 原生源端写入封闭

2026-10-26，基于干净`e062cb061`，沿整套实施及提交授权实施原始S04／AC04和§7第4–6步的物理停止能力。先交付可独立使用的运维命令、真实Docker适配器及非空源验收；B6应用接线、最终B5封存、历史路由与原子切换仍沿原始E07继续，不以本片替代。最小方案是直接停止整个源，但停止后不能完成S3最终读取；因此选用保持原端点的唯一受控入口拒写，加完整源进程退出屏障，再以只读入口提供最终读取。单纯数据库Gate、普通只读IAM、URL到期、Nginx reload或零ILM worker均不能代替。

```text
旧签名／root／普通账号／匿名写入 → 原固定S3入口（Nginx） → 私有MinIO
                                       ↑                    ↑
                              原生Fence命令 ───────────────┘
                                       ↓
                         私有耐久日志＋真实进程／配置观测
```

仅操作部署方显式登记的源容器、入口容器、私有网络和配置目录，精确ID、镜像摘要、标签、挂载及网络成员必须持续匹配；不自动接管当前共享9000服务。MinIO采用当前已锁定`14cea493…`摘要，入口采用已安装Nginx摘要`dc5069ad…`；不构建后端镜像，不改共享`.env`。Docker与宿主机管理员属于可信运维边界，应用和其他容器没有源端直达路径。既有部署须先按说明建立唯一入口并保留旧公开端点，不能注册一个数据库状态就声称源已停写。

`storage_source_fence.py`提供`init / preview / seal / status / release`。配置、状态和锁均位于0700私有目录，文件0600；真实root凭据只由命令环境取得，不进入状态、终端或报告。所有修改要求预期修订及操作UUID；进程内和跨进程互斥，先持久意图再改变原生资源。失联、取消、应答未知及配置漂移保留封闭责任，不自动恢复写入，也不按TTL清除。公开汇总只给阶段、修订、观测时间和计数；原生证明不是可任意导入应用的操作者JSON。

封闭顺序：持久意图→入口切只读并真实重启入口、终止旧入口进程→保存原规则→禁用全部当前生命周期规则并回读→停止全部源进程并确认退出后重启→列举并终止本范围未完成multipart→第二次完整源进程屏障→复核入口、源进程代次、禁用规则、无multipart及后台变更资格，再发布当前观测。覆盖已有当前／非当前过期规则的非空版本化源；规则不以删除代替禁用。无tier、无站点／桶复制及无Restore状态须在最后重启后真实复核，失败或不支持保持未知；不额外要求全库历史定位或全部业务消费者通过。显式release先使证明失效，按保存配置和当前禁用快照CAS恢复规则，成功后才恢复入口写入；外部改动拒绝覆盖，源数据和已有目标不删除。

本片超过8文件，范围为三个脚本（耐久核心、Docker适配与CLI）、三个定向检查文件、一个真实验收脚本，以及现有运维迁移手册、架构、CLI专用环境变量示例／派生参考、DEV、README、CHANGELOG和本计划。无应用API、数据库迁移、公开Worker、依赖、应用环境变量、新常驻控制服务或产品UI变化。真实验证在专属容器／卷／网络完成，不初始化或修改人工3100／8100的数据库、对象及文件；按精确owner清理。

验证包含正常封闭与显式恢复、旧预签名PUT／root／普通及匿名写拒绝、已被源实际接受的慢PUT收尾、multipart终止、当前及历史版本SHA保持、GET／Range／清单可用、已有生命周期规则恢复、硬退出后续跑、错CAS／身份／网络／配置漂移拒绝、丢失应答不自动解封与敏感输出排除。复用现有清单／复制核心在持续封闭期间完成最终源报告及对象核验；暂不授予应用最终切换或退役资格。执行相关纯检查、Ruff／格式、文档检查／构建、正常提交钩子及最终diff；清理所属资源和原始产物，保留有界脱敏摘要。

## E07-B7-A Outcome：原生源端写入封闭

2026-10-26，按原始S04／AC04及§7第4–6步交付独立原生命令。耐久核心集成`8ea10a689`；Docker适配与真实验收集成`946e7a543`／`24f07bec3`，入口网络、持久multipart和停止源恢复修正至`f71c32203`；CLI、最终真实缺陷修复、正式文档及结果随本Outcome提交。未改应用API、schema、Worker、UI、依赖或共享配置，不构建后端镜像、不分配发布版本。

- 唯一入口拒写、真实入口重启和两次完整源进程退出屏障保留最终S3只读能力。私有日志先保存意图，修订CAS／操作UUID及单入口唯一状态锁保护续跑；失联、进程中断或漂移不自动解封。显式恢复先失效证明，再CAS恢复原生命周期规则和写入口；源已停止、尚未重启的中断也能在只读入口下恢复。
- 真实演练暴露固定MinIO的全桶multipart列表重启后缓存归零，而上传仍在磁盘。已改为保存准确上传ID、ListParts／Abort／NoSuchUpload逐个核对，再有界检查当前原生元数据；停止前晚到且未映射的上传拒绝封存，显式核实处置后才能续跑。实际缓存3→0与迟到上传仍可ListParts同时观测，不能以空列表冒充排空。拒写后同一SDK连接后续读取400也已独立复现；关闭入口keepalive后，同一客户端读取200，完整链路验证通过。
- 最终105个不同用例全部通过：耐久核心36、Docker适配57、CLI12，不把subtest和重复轮次累加。覆盖私有描述／日志、跨进程互斥、实际资格和身份漂移、应答未知、停止边界恢复、规则及上传责任与安全错误输出。Ruff／格式、正常提交钩子及diff检查通过；正式文档frontmatter／可读性／导航和完整构建通过。
- 专属真实MinIO／Nginx非空源完成9用途、11对象版本及1删除标记的封闭与完整SHA保持；源已实际接受慢PUT，旧签名PUT／Complete、root、普通及匿名共5条写路径均405。硬退出同UUID续跑、源停止边界恢复、2个准确上传终止、未知残留拒绝与处置、实际源代次匹配通过。持续封闭期间完整清单与11个目标准确版本复制读回通过，规则及原签名写入显式恢复、漂移及错CAS拒绝通过；`migration_ready=false`保持。
- 正式知识写入[运维手册](../../docs-site/ops/deploy/storage-migration-inventory.md#minio-原生源端封闭)、[架构](../../docs-site/dev/concepts/storage-connections.md#原生源端写入封闭)、DEV、README、CLI专用环境变量示例／派生参考及CHANGELOG Unreleased。原始凭据、报告、日志、构建与测试缓存清理，仅保留有界0600汇总。13份owner对应74项容器／网络／卷按精确ID及39次owner标签查询独立确认不存在；三棵隔离代码树已按提交等价整合后清理。

人工3100／8100仍200，原Worker和Beat均active；本片不连接业务数据库测试模式，不操作原对象与文件，也不需要刷新应用。下一步是将当前原生能力绑定到B6窗口和最终B5清单／增量，再按原始要求推进历史准确版本路由、原子切换和带新写入回滚。本片只完成受控本机拓扑的原生停止能力，不冒称完整E07、TLS／多节点部署、生产切换或物理退役通过；不追加其他消费者或训练规模前置。

## E07-B6 Outcome：迁移窗口与受控来源读取

2026-10-26，沿原始S04及§7第4–6步交付独立迁移窗口和窗口内清单／B5后继复制。核心来源`bda7c5ca2`、回归`6a498c125`、真实请求提交修复`aad2f0267`，应用／Worker来源`940555d98`、维护拒绝响应`88cff7b0e`，Web来源`08a6abbb3`与本树正式文档同次集成；38个冻结路径逐字节一致。本片只证明登记访问排空，完整E07继续活动。

- 窗口固定当前默认来源的物理范围、配置修订及父批次／目标。开启原子转draining，实际登记操作归零后显式停止；关闭先阻止新专用准入，有实际操作则保留closing，归零后重试才恢复普通active。权限、CAS、配置修订及任务／Attempt身份持续核验，不按TTL丢弃许可。
- Inventory与后继Copy持久保存配对窗口ID／修订，报告观察信息保留窗口身份；只允许窗口固定父批次与目标。来源专用许可只授予冻结桶的必要SDK只读能力，目标仍要求普通active。普通源准入及预签名写入口保持拒绝，原生线程、Body、客户端、取消与晚到回执继续真实收尾。`source_quiescence_proven=false,migration_ready=false`始终保留；0258保存责任在已有窗口或绑定任务时拒绝破坏性降级。
- 207个当前不同后端用例、106个不同Web用例（26新增）和四项SDK合同通过。覆盖迁移约束、权限／CAS／并发、真实操作排空、跨任务／修订拒绝、独立Session下HTTP持久提交及受控409。真实链路发现开启201后下个请求404，已给两类修改路由显式提交并补独立请求回归；普通清单及停止目标原500已窄修为既有维护错误409。没有改动全局Permit或业务消费者。
- 所属真实HTTP→独立Worker→MinIO在停止来源下完成九桶清单、B5后继准备及24字节对象的准确版本复制。普通源读写准入及真实导入预签名被拒绝，停止目标时复制409；来源对象／版本和父证据保持，注册操作归零。真实浏览器另外开启并停止新窗口、生成新窗口清单、准备／运行第三批并显式关窗，结果一项核验、零失败，关闭后普通登记访问恢复；后继沿旧准确版本复用。
- 概览、开启、详情分别在明暗、1440×1000与390×844截图检查；标签和灰色提示在控件外，原生空选项无说明文字，居中详情可滚动，Escape与返回焦点、无横向溢出通过。实际窗口／清单／复制跳转与继承提示通过；浏览器无页面、控制台及已观察HTTP错误。所属流程63条、人工页面7条未捕获状态请求不计为成功，人工心跳单独本地模拟以保持业务指纹。
- 正式说明已写入[用户指南](../../docs-site/user-guide/datasets/storage-connections.md#迁移维护窗口)、[API](../../docs-site/api/guides/storage-locations.md#迁移维护窗口)、[架构](../../docs-site/dev/concepts/storage-connections.md)、[运维](../../docs-site/ops/deploy/storage-migration-inventory.md#迁移维护窗口中的只读准备)、README及CHANGELOG；OpenAPI、路由索引和SDK控制面分类同步。正常代码／类型／CSS检查、207页frontmatter、208页可读性及完整文档构建通过，最终构建39.36秒。

人工环境仅应用0258并刷新原两个Worker与绑定Beat，仍为3100／8100、每Worker75个任务；doctor健康，真实新窗口历史GET返回空。177张原业务表旧列的9,234行、24个对象版本／删除标记和16份源文件完整指纹保持，未在人工环境实际开启窗口，不重建镜像或修改共享配置／依赖。

所属隔离数据库、Redis、桶及额外目标按owner清理并独立核实；认证、私密清单／检查点、原始截图／日志与测试／构建缓存随本轮清理，仅保留有界脱敏摘要。回退应用须保留0258和窗口／任务／目标责任，不能借降级丢弃未收尾访问。

下一步仍按原始S04完成真实全源写栅栏与已接受上传排空，再接历史定位到已核验准确物理版本的业务读取／原子切换，以及新写入后的回滚；未知生产部署条件保持独立。本片不新增训练、Tracker、会话、审核或其他Epic验收前置，也不把窗口停止冒称完整迁移／退役完成。

## 本轮定稿：E07-B6 迁移窗口与受控来源读取

2026-10-26，基于`82ff05194`沿原始S04／§7第4–6步实施迁移窗口、登记访问排空和窗口内清单／增量复制。普通位置维护、来源读取校验及B5准确版本复制继续复用；不临时重开来源，不扩展业务消费者资格。本片交付完整维护流程的必要接缝，实际全源写栅栏和历史路由仍按原始后续工作实施，不能用窗口状态替代它们。

1. 新增独立耐久窗口，固定默认来源位置、规范化物理范围、配置修订、已有父Copy及目标身份，保留创建人和时间。一个来源最多一个未关闭窗口。只有当前超级管理员和完整Key权限可查看、开启和推进；开启须启用既有维护协议、父批次已准备且真实任务／访问许可已收尾，目标不能落在源九桶范围内。窗口不随AsyncJob清理删除。
2. 开启在同一事务锁定窗口与源Gate，将active改为draining并增加修订，普通准入保持原拒绝。显式停止只有在该源登记操作为零且固定配置仍一致时才能把Gate改为stopped；这仅证明登记访问排空。窗口响应固定`source_quiescence_proven=false,migration_ready=false`，清楚标识外部预签名、其他物理别名、未登记调用与提供方自动过期尚未获得全源停写证明，不增加操作员勾选即可授予资格的入口。
3. 关闭先禁止新的窗口读取；有实际操作时保留closing及阻断状态，显式再次关闭且登记操作归零后才恢复active并增加修订。取消一个任务不关闭窗口，权限撤销和重启不按TTL删除许可。推进使用窗口修订CAS，其他窗口不能接管旧任务。
4. Inventory和Copy增加nullable窗口ID／窗口修订，旧任务合同保持。Inventory请求可显式指定已停止窗口；B5后继由所选清单继承窗口，必须属于窗口固定父批次／目标。专用源许可由窗口、固定源身份、停止Gate及修订、数据库保存的任务／Attempt／task_id和运行状态联合校验，在独立准入事务登记原有StorageOperation；目标仍须普通active许可。准备、claim、checkpoint、重投与收尾沿用当前权限及事实核验，不提供通用allow_stopped参数。
5. 原生来源客户端额外限制SDK操作和Bucket：清单仅允许读取版本／生命周期、对象／版本／multipart列表、HEAD和GET；复制仅允许HEAD／GET。严格检查冻结源桶和端点，原Body、client、原生线程及取消收尾责任保持。目标写入不继承来源例外，晚到目标回执仍耐久保存。窗口中的清单不冒称全源最终封存，部分清单和未证实外部写入继续如实表达。
6. 应用提供窗口历史、详情、开启和修订保护的停止／关闭API，控制台复用居中详情展示状态、登记访问和真实边界，并可为清单选择停止窗口、在后继复制中显示继承。正式API、用户／运维说明和CHANGELOG同步。迁移0258保留窗口责任，存在窗口或绑定任务时拒绝破坏性降级；不修改共享配置、依赖、镜像或版本。

验证只覆盖本片新增行为与相关既有回归：一次性PG中的权限／CAS／并发／未收尾操作、关窗排空、原任务不绕过维护、跨窗口／任务／源修订拒绝、Job清理后窗口责任；真实小对象HTTP→独立Worker→MinIO完成停止源下清单与B5后继复制，拒绝源写入能力和普通准入、目标停止仍拒绝、准确版本及取消收尾保持。界面在明暗／桌面／窄屏检查，保留人工3100／8100和原数据指纹；本片不对人工环境实际开启窗口。按所属清理测试资源及私密产物，正常提交并记录实际Outcome；不把独立全源栅栏或未知生产目标变成本片的虚假通过条件。

## 后续缺口复核：最终停写、源只读与历史切换

2026-10-26，在`566e31c58`上只读核对原始S03／S04、§7第4–7步。当前B5可继续复制，但最终维护窗口仍缺以下实际能力，不由既有绿色验收或更新用途绑定替代：

- `storage_access.validate`拒绝非active，迁移清单／Copy Worker也走普通源许可；最终清单与增量需要绑定窗口、Copy／Attempt、源Gate修订的专用只读许可，不能临时重新开放源。
- import暂存、训练Run及Preview的签名入口只在签发时持许可，缺耐久签发及真实写栅栏／在途排空证据。仅活动许可清零、业务完成或等待URL到期，不能证明已开始的上传结束；本地可实现窗口协议及真实小对象演练，生产外部写入方和栅栏需实际部署条件。
- B5已保存准确目标VersionId，但`managed_original_reader.ObjectSpec`没有预期版本、`original_sources`拒绝多副本且要求旧key与业务路径一致，family resolver也要求原生命名空间。需显式逻辑定位到已核验准确物理版本的路由与原子切换修订，不能伪造family ref或只改Binding。

按原步骤先实现受控停写窗口内的最终清单与B5后继复制闭环，再实施历史路由／切换及新写入后的回滚。此复核不扩大E12删除计量或其他已交付功能的验收范围，不重启消费者专项；真实目标尚未指定的部分保持独立。

## E07-B5 Outcome：后续清单增量追平

2026-10-26，沿原始S03、AC03／AC05及§7最终增量方向完成可独立使用的后续批次。核心`0fc31d928`、应用`0ee84d487`、Web集成`13aa031025`、正式文档及共享任务文案`d27440664`已合入当前主工作树。不将增量复制等同最终停写／切换，不新增训练、Tracker、会话或审核专项前置。

- API仅接受新清单ID，继承原源范围、目标、九用途声明和预算；父批次在真实执行／原生许可收尾后冻结并建立唯一后继。相同父和报告重复提交返回同一后继，父批次仍可查看及下载；清理原清单及复制Job后仍可准备下一批。
- 自包含新检查点保留祖先创建UUID、nonce、目标键及准确版本回执，旧检查点继续可用。复用行先待核验，再完整读回保存的准确版本；新的核验时间与原创建事实分别保留。可变源重新复制，显式源身份冲突不覆盖／采纳；历史去重、未观察到对象及删除标记保持明确，不推断删除。
- 60项核心／CLI、52项API／DB／Worker及迁移、63项不同Web、四项SDK合同通过。新增接口已生成OpenAPI及路由索引，SDK按现有Web专属控制面分类。Ruff／格式、正常提交钩子、207页frontmatter、208页可读性、导航与完整文档构建通过；构建38.31秒。本轮不重跑既有生产选型、硬中断或业务消费者全部验收。
- 所属真实HTTP→独立Worker→MinIO先复制三个对象。源端随后新增一个显式版本、覆盖一个null对象、移除一个准确源版本，并在稳定源上新增删除标记；目标同键另有foreign最新版本。第二批准备时目标版本数不变、0已核验／3待处理／1跳过，差异为复用1、新增1、可变重复制1、未观察到1和删除标记1。稳定对象仍读原准确目标VersionId，新报告中的`is_latest=false`保持。
- 另在新可变目标预置所属冲突，执行准确得到2成功／1失败／0待处理；仅移除该测试冲突版本并重试选中一项后，三对象全部核验。两次执行共读取源22字节，实际新增目标仅新对象及可变对象的两个版本，复用对象不重传。原上传预算保守计算SDK对Body的重复读取，不把其计数冒称网络传输字节。
- 第三批复用两个准确版本，只为可变源新增一个目标版本；第四批用真实不完整清单处理两个可见对象，仍`source_complete=false,migration_ready=false`。父逻辑证据摘要不变，旧未见对象的目标准确版本仍可完整读取；Worker操作前后源对象／版本／规则／multipart事实一致。
- 真实页面完成准备、明确执行、父子跳转、冻结父操作、部分清单与任务深链；认证HTTP下载检查点并核对摘要、准确回执及父证据。准备与详情各在明暗、1440×1000和390×844截图检查，居中滚动、Escape／焦点与横向溢出检查通过。浏览器无控制台错误和已观察HTTP错误；31条未捕获状态的请求不计为成功。共享任务标题及增量准备结果已按实际批次表达。

人工刷新前核对无活动／预留／定时Worker任务，短暂停止准确所属服务后应用0257、生成当前API类型并重启两个Worker和Beat；3100／8100、API新路由及doctor健康通过。177张原业务表的旧列9,234行、24个对象版本／删除标记和16份源文件完整指纹保持，无后端镜像重建或共享环境配置改动。

API隔离树dev／test／e2e的数据库、Redis及27桶均已销毁并独立确认不存在，额外目标桶按随机owner标签和准确版本清理；core／Web树未初始化launcher资源。私密认证、检查点、原始日志、截图和构建／测试缓存随本轮清理，仅保留有界脱敏摘要。应用回退须保留0257及父子证据，已有lineage时拒绝破坏性schema降级；不解除父批次冻结或删除已写目标。

完整E07继续活动：原始完整候选选型、全写入渠道最终停写核验、业务定位切换、新写入回滚及旧实例退役仍未完成；未指定真实部署目标的部分继续独立保留。本轮结果不新增上述生产门禁为其他Epic的前置。

## 本轮定稿：E07-B5 后续清单增量追平

2026-10-26基于`9e2c08a65`复核原始§7第6步、S03／AC03、AC05。B4的新清单仍建立独立初次计划，不能复用已复制副本。本片交付当前应用可实施的连续批次、差异和增量复制；最终停写证明、业务位置切换、观察及退役继续按原始后续步骤实施，不以其作为本片前置。

### 冻结合同

1. Copy新增`parent_copy_id`（可空、唯一、自引用RESTRICT）、`generation`（根为1）及`parent_evidence_sha256`。基于已准备、无活动attempt及未收尾访问许可的父批次，原子预留唯一后继并冻结父批次；父批次摘要／检查点仍可读。相同父批次和相同新报告复用同一子批次，不分叉；准备失败沿该子批次重试。不要求父批次已全部复制、报告完整或生产资格齐备，不以TTL推断原执行结束。
2. `POST /storage/migration-copies/{parent_id}/generations`只接受`{inventory_job_id}`，新报告沿既有认证下载验证，来源范围、目标、九用途处理和预算继承父批次。返回既有`{item,reused}`并派发prepare；审阅后仍显式调用现有run，沿用取消、重投、续跑、单项失败重试与准确版本核验。
3. Item增加`parent_copy_id`、`generation`、`successor_copy_id`、`parent_evidence_sha256`、`read_only`及`generation_summary`；旧响应默认根批次／非只读。差异计数固定为`reused_objects`、`new_objects`、`mutable_recopies`、`source_identity_conflicts`、`previous_only_objects`、`delete_markers`和`history_records`。复用表示保留原目标创建身份，不冒充本批次已核验。
4. 核心新增`Checkpoint.evidence_digest()`，按经过校验的meta、排序当前行、历史行和facts产生稳定逻辑摘要，不依赖SQLite文件页布局；API冻结父证据时与Worker准备时核对同一摘要。新增`plan_generation(checkpoint_path, source_report, catalog_path, target, *, parent, parent_evidence_sha256, handling=None, limits=None, checkpoint_hook=None, max_bytes=None, max_rows=None, strict_closure=False)`；`parent`为已打开Checkpoint，返回既有status并附generation_summary。根计划仍写v1，新子批次写可自包含的v2；现有读取／执行／CLI续跑兼容两种格式。
5. 同端点／桶／键／显式非null版本、SHA／大小／ETag相同，且旧行已有nonce／content或准确回执的对象，保留原`creation_plan_uuid`、目标key、nonce、content和准确VersionId。新批次当前状态统一pending，执行时完整读回旧准确版本再记verified，有回执者源读取／PUT为零；未知ACK继续原身份恢复。旧无物理意图的待复制行可分配新批次目标，不宣称已复制。
6. 新键／新版本正常复制；null或未固定版本保守重新复制到新目标key。相同显式版本却出现不同摘要／大小／ETag记`source_identity_conflict`，不覆盖旧事实。保留新报告中的`is_latest`及删除标记；旧行未出现在新报告记“本次未观察到”，部分报告不能推导删除。历史创建事实和准确回执在v2有界去重保存，跨三批次不重复膨胀同一身份；当前及历史受原有字节／行数预算约束。
7. 逐行创建UUID只能取当前计划或已核验父证据中的祖先；保持目标key的受控前缀、创建身份和准确版本校验，不能为复用而放宽任意路径。持久增量恢复冻结新增身份字段。父批次冻结覆盖attempt、重投、claim及实际执行接缝；冻结前必须真实收尾，不丢弃晚到物理回执。子批次计数从pending基线开始，不能把旧成功数当成本批次结果。
8. UI复用原居中Modal和任务详情：固定父批次／目标／九用途／预算，仅分页选择不同的可下载封存清单；准备后展示差异、父子链接、原批次只读及显式执行。保留URL、分页、返回上下文、撤权与迟到响应隔离，标签和灰色提示在控件外。

### 实施与验证

核心、应用持久责任与Web在基于同一本地HEAD的隔离工作树分工，主树集成后验证。覆盖旧v1、三批次／去重、准确旧版本被新latest遮蔽、零重复PUT、新增和mutable变化、同版本冲突、删除标记／部分报告、未知ACK续做、父子竞态、Job清理后证据及明确权限变化；使用现有一次性PostgreSQL／MinIO／Worker和真实浏览器，不重跑B4所有历史验收，不增加模型、训练测量或其他消费者条件。同步API、用户／迁移文档、SDK分类和CHANGELOG；适用迁移及Worker刷新保留人工3100／8100、原业务和原件。不重建镜像，不改共享配置或分配发布版本。

## E07-B4 Outcome：应用初次复制与续跑

2026-10-25与E09-B18同次交付，从封存源清单生成配对目录和耐久计划，明确审阅后执行初次复制；重投／取消、原计划续跑、单项失败重试、准确版本核验及同主机进程结束证明后的恢复均已接通。123项后端、113项CLI、189项Web与四项SDK合同及真实PG／MinIO／Worker、完整UI链路通过；预算停止和未知覆盖保持准确表达。人工0256、原业务／对象／文件保持及资源清理详见[E09-B18 Outcome](archive/1792661016_dataset-storage-console-boundaries.md#e09-b18-outcome应用初次复制与续跑)。本片完成原始S03／AC03的应用接线；完整选型、增量追平／最终停写、切换／回滚与物理退役仍按原始故事推进，不宣称生产迁移已完成。

## 本轮定稿：E07-B3 当前九用途保存定位目录

2026-10-25，基于`0832098bb`复核原始E07-S02／AC02、AC05及§6。九用途新分配切换已交付，但原迁移目录仍只有24家族，仅接受头像／报告附件schema2，忽略current／cache分支中的保存地址和Creation跨端点暂存。下一片补齐当前0254的真实保存责任目录，不把未知生产目标或全量历史接管增加为前置。

完整范围为现有24家族加MaskFormatUpload、MaskFormatImport和ThumbnailOutput，共27家族；ManagedOriginalCreation的最终地址与staging_ref分别投影，Mask批次的staging_ref只提取地址字段。逐family按实际schema解释：Dataset／Auxiliary的新版定位仍为schema1，TaskOriginal及媒体缓存等按各自当前结构，不能全局放行schema2。prefix与key_prefix分开，单对象地址不得误报为前缀；当前原图缓存使用保存prefix下的payload，Thumbnail与inventory按已保存完整物理键记录，预标inventory相对键只拼接一次保存根。保留版本化回执、mutable null与未固定版本的区别，不从当前用途绑定补猜历史地址。

SQL仍只读、可分页且不读密钥／完整业务文档。新增表按实际引入revision处理旧0244／0245兼容，旧NULL的可定位字段与未知原因原样分列；未知schema、失配字段和未展开冻结文档继续明确报告，不借新支持宣称migration_ready。源清单、目录schema及预复制checkpoint合同保持，目录交给已有copy plan后九用途处理选择与未知覆盖应保留，不新增复制／切换／退役命令。

准确修改范围跨工具、验证与文档，超过8文件：`scripts/inventory_storage_references.py`、`scripts/storage_migration_references.py`、两份既有引用目录纯检查、新的当前schema真实验证脚本及必要复制合同回归；正式运维目录页、DEV、README、CHANGELOG、本计划和实施队列。修改代理在当前HEAD的独立工作树负责解析／纯检查，主树负责当前schema验证与文档、集成和交付。旧工具的原始实测证据保持为历史记录，不把0244／0245资格冒充0254，也不为本片重复旧提供商性能或业务消费者演练。

先用纯检查复现各新结构遗漏，再验证27家族SQL投影、schema1／2、外部／旧端点、根与对象边界、精确版本／null／缺失、坏schema及敏感字段排除。真实检查只使用官方launcher核验且为空的test模式，在0254准备少量覆盖不同地址形式的合成行和对象，以独立只读数据库账号实际运行CLI；比较全部业务表、源对象版本／内容与输入清单指纹，验证写入和秘密读取均被拒绝、私有spill清理、目录可由现有copy plan读取。完成后撤销所属角色并销毁test数据库／Redis／九桶，保留3100／8100和Worker／Beat。Ruff、相关纯检查、文档检查／构建、正常提交钩子及diff检查通过后提交；不改应用、API、数据库schema、依赖、共享配置或镜像，不推送或发布。

回退仅停用本片CLI及清理所属私有输出，没有业务或对象位置要撤销。E02停用影响尚未覆盖新保存target的独立缺口继续按其原始S05／AC08推进，不扩展Tracker、会话、审核、模型或规模测量；本片不代表历史搬迁、生产切换／回滚、供应商选择或整项E07完成。

> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 已按 [backlog promotion gate](backlog/README.md#promotion-gate) 复核当前实现与源端分项；整套实施授权沿用当前目标。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

> 优先级：P1；M0 开展选型与兼容试验，M2 完成生产迁移。
> 负责人角色：后端、运维、测试；产品负责人确认维护窗口与迁移范围。
> 依赖：PoC 可独立启动；生产切换依赖 EPIC-01/02/05 的位置定位、用途绑定和读取兼容，以及 EPIC-12 的恢复演练。

## 下一片定稿：E07-P1 双候选本地 S3 契约与运行基线

2026-10-25，基线 `860128595`，沿整套实施授权推进原始 E07-S01／§5／AC01 的双候选本地实测。当前无真实生产部署目标，隔离 PoC 仍可直接实施；它与 E07-B2 初次复制、E12 增量账目及 C6 测量分别推进，不互相新增前置。只交付当前锁定构建的同场景报告，不选供应商、不改现有部署或业务用途。

最小路径是运行上游兼容测试列表；本片复用上游矩阵作为输入，用仓库已有 boto3、实际 `StorageService`／签名实现及本地浏览器验证项目用到的参数、代理和错误处理，避免增加另一套依赖与部署服务。根应用、Compose、数据库、Redis、现有 MinIO 和人工3100／8100均保持；候选仅在所属短期容器、网络、卷与本地回环随机端口中运行，不重建任何后端镜像。

### 真实构建锁定与许可

已只读核对官方发布、Registry清单及其config blob的实际SHA。初始锁定linux/amd64的 SeaweedFS `4.49_large_disk_foundationdb`（manifest `3015781a9ae31319033c5f25658270efb23fd0ec44e11ee7f24b378ee42bfc3a`），RustFS `1.0.3`（manifest `128220dc9382bac81940a1a9616ee8c274a3f3b8f647eedbeb50df5bb8b49922`），代理使用已缓存 Nginx digest `dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c`。实际执行始终按digest和显式平台，不按可变tag拉取。

SeaweedFS普通`4.49`镜像标签不存在；所选同版本变体的OCI源码声明是 `b97ed0f1014050c41b3a7f0b8c0c91f579a79998`，与GitHub发行tag提交 `b65832bdc9bbbcf3682465b1be598627ac386206`分别保存。其额外FoundationDB适配器不配置、不启动独立FoundationDB实例；报告保留实际变体，不能替代其他构建。RustFS发行tag提交为 `331fb896285a7686f927baf8d9d4dc2d3d3a10a9`，镜像没有OCI源码label，保留这一未知。首个运行探测另记录实际二进制版本，不能把tag、源码声明与二进制观测混成一项。

两个所选源码修订的原始Apache-2.0许可文件、来源URL与完整SHA进入固定输入；SeaweedFS许可SHA为 `d789d433cc11da163273d1e39be2e8fa67642f9a58ef220d3f258fa9c14ef613`，RustFS为 `8ef87773cd6df917c9cf75bdb3075d92b78ad4cdf8e51d6c98030292442524d8`。来源：[SeaweedFS发行](https://github.com/seaweedfs/seaweedfs/releases/tag/4.49)、[所选构建源码许可](https://github.com/seaweedfs/seaweedfs/blob/b97ed0f1014050c41b3a7f0b8c0c91f579a79998/LICENSE)、[RustFS发行](https://github.com/rustfs/rustfs/releases/tag/1.0.3)、[源码许可](https://github.com/rustfs/rustfs/blob/331fb896285a7686f927baf8d9d4dc2d3d3a10a9/LICENSE)。[SeaweedFS S3矩阵](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)与[RustFS矩阵](https://docs.rustfs.com/en/reference/s3-compatibility)是测试输入，矩阵宣称不代替实际结果。

### 同一测试合同

两候选顺序使用相同确定性夹具、4 CPU／8 GiB容器上限、相同代理配置和协议用例；每候选默认最多256个对象、各方向实际正文累计1 GiB、15分钟协作时限，网络连接3秒／读取15秒、SDK不自动重试，启动／重启分别最多120／60秒。记录主机、Docker、Python／boto3、源码指纹、镜像／平台、实际时间窗口及同时运行的C6等负载，不推断生产SLO或用厂商跑分替代实测。

- 基础及版本：真实PUT／GET／HEAD、元数据、前缀／版本分页、空对象、中文／百分号等应用可用原始键、删除、版本覆盖与固定旧版本读取、mutable null、删除标记、精确版本删除、条件GET／HEAD、并发条件PUT只成功一次。字面`../`类提供方不接受的键由纯寻址检查明确覆盖，不能为通过放松应用路径合同。ETag单独记录，完整SHA才是正文证据。
- multipart：建立、两份至少5 MiB的part、分页核对、错误完成拒绝／正确重试、最终完整SHA、未完成上传中断及abort后absence；中断不能出现误认成功或部分正文。加密能力另报告实际探测与当前平台使用状态，不引入KMS或生产密钥作为本片前置，不将未执行加密场景写成通过。
- 媒体与签名：用当前 `StorageService` 生成内部／ML与浏览器签名，明确绝对公共Host保留及相对前缀剥离／内部Host恢复两条真实Nginx路由。读取实际生成的图片、短MP4与ZIP并核对完整SHA及可解码／成员CRC；Range／Content-Length／Content-Range、条件拒绝、签名篡改、错误凭据和真正过期的原有效签名分别测试。这里验证存储服务及媒体格式，不宣称已有冻结业务版本完成迁移。
- 浏览器：独立临时页面从同源相对路由及不同回环Origin实际执行预签名PUT、GET／Range、CORS预检、Content-Type、允许暴露的响应头和拒绝后新签名重试；记录零新增异常及有界结果，私有签名不进入截图／终端／报告。优先已安装agent-browser及独立所属session，结束关闭该session。
- 生命周期：调用当前应用实际规则配置并回读，证明可重建帧／导出前缀与持久预览、头像及归档的不同范围；只回读规则不能称实际到期已通过。当前原生到期清理、真实TLS入口、磁盘满、多节点故障和滚动升级仍在报告原始矩阵中标为not_run／未资格化，留在E07原始范围，不伪造本片成功，也不阻断独立初次复制。
- 本地运行：固定已确认对象与版本后真实重启、写入中强杀，恢复后确认原有正文及元数据保持，未确认写入只允许缺失或完整对象。停稳后备份所属同提供方数据卷，恢复到新的所属卷并逐保存版本回读；不把这种同版本卷恢复称为MinIO跨供应商磁盘格式迁移。记录同一批小图片／混合大小对象单路与四路PUT／GET延迟、实际字节、HTTP失败、CPU和RSS采样范围，保留共享主机及采样峰值限制。

每个案例都有passed／failed／not_supported／not_run／inconclusive、具体阶段／脱敏错误码、证据和原始§5类别映射。分开执行完成、上述本地基线是否通过、未覆盖类别和完整选型资格；完整§5仍有未执行项时 `selection_ready=false`、`production_ready=false`。失败如实记录，不能删案例、改错误期望、跳过实际浏览器或替换平台实现获得通过。后续按原始矩阵补齐生产拓扑／原生过期／故障及升级证据，再形成供应商选择ADR；本片不把这些原始剩余项变成E12或E07-B2的新门槛。

```text
fixed image + source/license facts -> owned candidate / same proxy
same fixture + current StorageService -> signed access -> browser / internal reader
owned restart + same-provider volume backup ----------> exact body/version checks
all observations ------------------------------------> scoped comparison report
```

### 所属资源、准确范围与验证

专用CLI只使用固定候选与临时生成凭据，不读取或连接已有数据库／S3业务服务。先持久化所属nonce、创建的容器／卷／网络／会话精确ID和镜像，清理前核对checkout绑定、创建记录及owner label，不凭名称前缀猜测归属。凭据只在0600私有文件／子进程env，日志私有且有界；不把Docker完整Env、provider消息或签名URL写入报告。所有退出路径收尾自己的资源，硬杀遗留提供同一manifest的显式cleanup入口，发现其他owner／使用者时拒绝删除。镜像作为固定测试依赖缓存保留，容器／卷／网络／浏览器session、暂存、原始日志与构建缓存按归属清理，仅保留正式报告及有界私有核验摘要。

共15文件：`scripts/storage_s3_candidates.lock.json`；`docs/research/data/storage-s3-candidate-licenses/seaweedfs.txt`、`rustfs.txt`；新 `scripts/storage_s3_contract.py`、`scripts/verify_storage_candidates.py`、`scripts/test_storage_s3_contract.py`、`scripts/test_verify_storage_candidates.py`；新正式 `docs-site/ops/deploy/storage-candidate-poc.md`；现有侧栏 `docs-site/.vitepress/navigation/ops.ts`、运维首页、DEV、README、CHANGELOG、本计划与执行队列。许可路径按仓库根解析，原始文件字节及SHA保持，不作格式归一化。前三份固定输入先随本合同提交；代码修改代理须在此后当前本地HEAD创建的独立树实施，其余应用／冻结八脚本不变。

纯检查针对有意义的报告判定、损坏／不匹配lock及许可、错误响应／分页／正文和元数据、预算、敏感信息排除、资源owner／label不符与清理中断；实际双候选报告按同一入口执行，记录所有失败及未覆盖，不能用fake client替代提供方或浏览器。完成相关代码／文档检查、正常钩子和diff，核对全部资源absence后提交。停止PoC即可回退，不改既有业务配置；完整E07、供应商选择及生产迁移继续保留活动状态。

## 下一片定稿：E07-B2 本地初次复制与可恢复核验

2026-10-25，基线 `67ce6847f`，对应原始 E07-S03／§6、§7.3、§8“切换前失败”及 AC03／AC05。E07-A／B 的源端和保存定位目录已交付，下一片直接实现本地初次复制，不等待未指定的生产目标。原计划 E07-C 仍指生产切换与退役；本片不改绑定、业务定位或冻结manifest，不将复制成功等同于停写、切换或回滚完成。

最小路径是普通复制命令；选择在现有 boto3 与封存输入之上增加逐源版本结果和完整目标读回，因为现有“空目标恢复”不支持历史版本映射、续跑或丢失应答确认。只增加工具及正式手册，不增加服务、公开API、表、依赖、环境变量、镜像或双写。

### 输入与耐久状态

新增 `copy_storage_migration.py` 提供 `plan`、`run`、`verify`、`status`。`run` 默认续跑 pending／uncertain，失败项仅在 `--retry-failed` 时重试，已验证项不重复上传；SIGINT／SIGTERM 暂停，重启沿用同一checkpoint。终端只有固定大小计数及脱敏错误码，逐键结果留在0600私有SQLite。事务与完整同步、所属进程锁保护 schema、计划UUID、两个输入完整摘要、源／目标身份及逐对象意图／回执；汇总从唯一结果导出，不单独累加成功数。本地摘要只检查截断、损坏和意外改写，不宣称签名认证。

`plan` 在每个输入的同一FD中验证、索引并计算完整摘要；E07-B目录必须关联该源报告，不能先验证再重开其他文件。未封存输入拒绝，已封存的不完整输入只能生成部分计划。源对象须有完整SHA核验且在预算内才可复制；定位未知、未展开、删除标记、未完成multipart、未核验及超限明确单列，不能把标记复制为空对象，不能将没有业务匹配的对象称为孤儿。未知业务声明不阻断已知源对象预复制，也不授予业务改写或删除许可。

九用途沿用源报告的 `migrate/rebuild/retain/unreviewed`，可用同格式 `--handling` 私有JSON明确覆盖并固定进计划；同一物理桶只计一次。同桶用途选择必须一致且已评审，否则该桶标为处理歧义；retain／rebuild不上传、不自动清理。分别报告全部源事实、可复制子集、跳过／歧义／未核验／超限和源覆盖；即使可复制子集全部通过，也不宣称全量迁移，始终 `migration_ready=false`。

### 地址和真实复制

源凭据每次从现有部署配置读取，端点须与计划固定源身份一致。目标使用0600私有JSON文件，字段为 `endpoint_url`、`bucket`、`key_prefix`、`access_key_id`、`secret_access_key`；凭据不写checkpoint／报告、不作为命令行值，不新增全局配置。拒绝与源报告任一范围相同的 endpoint／bucket。目标桶须已启用版本控制；CLI不建桶、不改规则、不删对象，演练脚本单独负责创建和清理自己的目标。

目标布局固定为 `<key_prefix><plan_uuid>/<源定位SHA>/<源内容SHA>`，定位SHA包含原 endpoint／bucket／原始key／VersionId。不同源版本保存独立目标地址和实际目标VersionId，不能假定源／目标版本相同；此为预复制映射，不自动修改已有消费者。S3 key不解码、不作文件系统归一化。源GET指定保存版本和条件ETag，完整核对SHA／大小、前后HEAD并真实关闭正文；可覆盖的null与未固定版本分别处理，变化或确认不足即失败／未知。清单已脱敏，必要ContentType／CacheControl等内容元数据由此次受条件约束的真实读取取得并核对，不能原样使用脱敏值。

每项在物理写入前持久保存意图及随机创建nonce，使用 `IfNoneMatch="*"` 上传；目标完整读回并验证SHA、大小、内容元数据及实际不可变版本后才发布verified。丢失PUT应答、写后提交失败或硬杀留下 writing／uncertain；恢复仅在目标创建标识、内容和版本证据均相符时认领同一结果。其他已存在对象拒绝覆盖，不能靠重复创建版本解决应答不明。`verify` 只读保存的目标版本，不上传；核验失败保留原回执与最近错误，不继续显示核验成功。I/O期间没有业务数据库事务。

默认单对象256 MiB，每次实际源读取／目标上传／目标核验各最多2 GiB、最多10,000对象和30分钟协作时限；参数可显式调整但必须为有限正值。先用所属私有磁盘暂存完成源验证，内存流式有界，不引入multipart或SDK后台传输；网络另设连接／读取时限，不声称能抢占所有底层阻塞。正常完成／暂停及重启清理自己的暂存，硬杀遗留只按计划标识和精确路径收尾，不清理其他文件。磁盘满、读写／关闭失败和预算用尽均保留可恢复状态。

```text
sealed source inventory + paired catalog
                 | full digests, one FD per input
                 v
       plan + private durable checkpoint
                 |
       verified source -> private disk temp
                 |
       conditional target PUT + creation nonce
                 |
       exact target VersionId + full readback
                 |
       saved verified receipt (no app cutover)
```

### 实施与验证

共11文件：新 `scripts/storage_migration_copy.py`、`scripts/copy_storage_migration.py`、`scripts/test_storage_migration_copy.py`、`scripts/test_copy_storage_migration.py`、`scripts/verify_storage_migration_copy.py`；现有运维迁移工具页、DEV、README、CHANGELOG Unreleased、本计划及执行队列。修改代理使用基于当时本地主树HEAD的隔离树，复用已提交 E07-B封存接口；原三份源清单及C6八份冻结工具不变，不改应用／Worker／镜像或扩展消费者资格。

纯检查覆盖输入摘要绑定、原始特殊键／版本、共用桶处理歧义、幂等计数、私有checkpoint损坏、预算、流关闭、应答丢失及未知目标冲突。真实新CLI在所属随机MinIO源／目标验证复制前后及PUT确认窗口强制中断、进程重启续跑、失败单项重试；空对象、中文／百分号键、共享用途、历史版本、mutable null与删除标记准确保留，目标逐对象SHA／实际VersionId对账，源对象／版本／规则与业务指纹保持。只验证本片，不重跑源清单家族和全部消费者资格。

完成代码／格式、正式文档检查及构建、正常提交钩子和diff后提交，按归属撤销临时账号并清理桶／对象／暂存及一次性资源，保留有界摘要和人工3100／8100。回退只停用工具并保留目标及checkpoint，不反向删除源或其他目标。双候选SeaweedFS／RustFS同环境报告、最终停写与增量、业务定位切换、新写入回滚和退役仍是原始剩余项，不被本地复制结果替代。

## 本轮定稿：E07-B 已保存业务定位与源清单关联

2026-10-25，基于干净根`b3a4fc549`复核，状态approved，沿整套Epic实施／提交授权推进原始S02／§6。源对象清单已交付，但CLI的数据库观测只含绑定、位置种类和活动Job分组，逐对象业务引用及历史位置仍是unknown。本片交付真实保存owner的私有目录及与既有源报告的离线关联；不改写原源报告，不把该目录等同完整写入方、目标复制或迁移就绪。B8实现及修复`3e24a78c3`／`8855d66d3`已在隔离树完成验收，可作为新schema测试依赖；根应用仍在0244、C6原session27154保持冻结，不能提前合入应用。

### 保存事实与关联合同

新CLI独立读取已保存S3定位，覆盖所有状态，不按当前Binding或可见业务页筛选。以现有`storage_usage_categories.FAMILIES`的16类独立记录为定位起点，包括预测输入、金字塔、视频、导出媒体、训练版本／实验／预览、评估、选样、头像、报告／评论／指南附件、RLE、导出ZIP及审计归档；另覆盖Creation、Retirement、DatasetAssetLocation、TaskOriginal、DatasetAuxiliaryOriginal、DatasetSourcePreview、OriginalCacheEntry与TrainingStorageDeletion中的最终／暂存键、当前定位、历史声明与删除前缀。配置位置、用途绑定及其孤立关联也单列保存。家族列表和键规则可复用，分类器不能替代目录，强制比较当前端点的业务resolver也不能用来丢弃历史声明。

目录明确区分保存声明、当前位置身份核对和源报告实际匹配。先使用保存的endpoint／bucket／完整key或prefix；引用只有storage_identity时，必须与已知候选endpoint计算结果一致才补充地址依据。恢复后的当前位置与创建／退休历史声明分别列出，不相互覆盖。已保存`inventory.objects`按预算展开精确键、版本、大小及SHA；预测输入的相对键仅按其自身保存前缀解释。缺owner、缺地址、未知schema或未展开的私有冻结文档明确计数和原因，不按当前桶猜补，不创建回填或消费者修复前置。

数据库使用单个有超时的REPEATABLE READ READ ONLY快照，静态白名单字段、稳定主键分页；连接只取必要的endpoint／bucket／base_prefix／use_ssl等非凭据配置，不读取secret_enc、authority、认证token或完整业务payload。头像token列是公开头像对象的32位十六进制owner ID，仅作为必要逻辑主键投影；不放开认证／API／authority令牌。行数、页数、JSON展开、报告字节与总时长均有显式预算，超限封闭不完整目录。记录实际迁移revision；0244／0245通过information_schema探测可见白名单列，旧表不投影缺失的object_version_id，不以新ORM读取旧表。

先关闭数据库事务，再流式验证既有源JSONL的摘要／完整性并关联。地址按endpoint＋bucket＋完整key＋版本精确区分，前缀保留真实边界；`null`单列为可覆盖槽位。结果区分准确版本、未固定版本的同键声明、前缀范围、所选源之外、未观测、冲突和无法定位。两个观测窗口不构成原子快照；目录没有匹配也只能称本目录未匹配，不能称孤儿或授予删除许可。关联过大的本地索引可使用所属私有临时文件并在结束清理，不把全部清单载入无限内存。

新目录使用独立schema、0600独占创建及受摘要覆盖的summary／tail；终端只输出计数，不输出路径明细、凭据或签名地址。分别报告注册家族枚举完成、定位未知／未展开、源报告覆盖、冲突和截断；退出码不得把完整枚举偷换成所有业务定位正确。`migration_ready=false`及未知外部写入方／预签名上传／停写状态始终保留。现有三个对象清单脚本、其schema和C6冻结工具不变，无应用、公开API、数据库迁移、依赖、镜像或发布版本修改。

```text
saved owners / historical declarations -> read-only DB snapshot -> private reference JSONL
selected source JSONL ------------------> digest + streaming join -> scoped matches / unknowns
                                                                    no copy or cutover
```

### 准确文件与实施顺序

共11文件，跨脚本、数据库读取适配、报告合同和正式运维说明，作为同一目录能力交付：新`scripts/storage_migration_references.py`、`scripts/inventory_storage_references.py`、`scripts/test_storage_migration_references.py`、`scripts/test_inventory_storage_references.py`、`scripts/verify_storage_references.py`；现有`docs-site/ops/deploy/storage-migration-inventory.md`、DEV、README、CHANGELOG Unreleased、本计划及执行队列。不得扩为应用消费者改造。

修改代理使用从当前本地HEAD创建的独立树。为实际验证0244／0245，可仅在该树集成已验收B8两个依赖提交，父树保持不变；冲突仅处理已知执行队列状态，保留父树已完成C5／千样本C6与B8暂存事实。核对应用载荷与B8固定提交一致后，用仓库recipe准备该树本地venv及生成类型。最终交付只将本片11文件形成独立提交，父树可在C6运行期间集成tools/docs；B8应用仍需待冻结结束后独立合入和刷新。

先验证纯解析、精确／前缀／版本及双报告完整性，再在该树核验归属的空test环境准备少量schema1／2、退休／孤立定位及同键不同端点／版本陷阱。新CLI实际使用只读数据库账号，关联真实小源报告，核对全部业务与对象指纹不变；在只有旧事实时往返0244／0245，分别验证缺列与新列分支，结束恢复head后按owner销毁。源端全版本、消费者链路和浏览器已验收部分不重跑。

分层检查采用该树虚拟环境执行新增`test_*storage*references.py`，实际资格使用`verify_storage_references.py --report <private-path>`并沿现有工作树test归属守卫。Ruff／格式、受影响文档frontmatter／链接／可读性与构建、正常提交钩子和diff检查通过后提交；具体命令、预算和退出语义同步正式运维页。清理该树test及有界临时文件，保留父树3100／8100、两Worker／Beat和C6 test。回退仅停用新CLI及处理自有目录文件，没有业务状态、对象复制或用途绑定要撤销；完整E07及原始剩余要求继续保留。

## 当前实施：E07-A 源端只读迁移清单

2026-10-25，基线 `848605a7a`。用户已授权推进整套Epic及当前环境可实施部分，尚未指定真实迁移目标。本片对应原始E07-S02／§6，并为AC02提供源端事实；不把未知目标当作源端盘点的阻塞，也不据本片标完完整S02或E07。

### 当前实现与依赖复核

- `dataset_assets.PURPOSE_BUCKET_ATTRIBUTES` 已定义九用途；`storage_usage.configured_scope` 对当前配置的物理桶去重，C3已有持久分类盘点。它们没有输出可逐对象核验的迁移清单。
- `storage_backup_bundle` 已有独立SHA／大小、九用途映射及一致恢复清单，但备份合同不能代替包含源端历史版本、删除标记、生命周期与未完成multipart的迁移观测。
- E02-B14已支持头像新写入位置切换。当前配置源、已保存绑定及历史位置的覆盖必须分别说明，不能把九个环境桶当成所有实际位置。
- `storage_maintenance_impact` 能提供本地依赖事实，不证明所有API、Worker、外部客户端或旧预签名上传已经停止。清单中的活动作业只能是观测到的潜在写入方，未知覆盖保留；它不是切换许可。
- E12-C5仍在原进程测量。本片只新增独立脚本和文档，不修改该进程加载的业务代码、六份冻结测量工具、运行配置或依赖；人工3100／8100及两个Worker／Beat保留。

### 合同与准确范围

源端CLI使用显式部署配置，逐一记录九用途及其共享物理桶；首次只对所选择配置源出具观测，其他保存位置与绑定缺口明确列出。只调用源端读取API，不写对象、规则、用途绑定或业务行。获取生命周期、版本控制和未完成multipart事实；未授权、不支持、分页异常与无配置分别表达，不能将失败变成零或空规则。

对未版本化对象分页列举；对已启用或暂停版本控制的桶列举全部版本及删除标记，保留精确VersionId、IsLatest、ETag、大小与时间。正文按版本／条件GET流式计算独立SHA-256，验证响应及前后HEAD，真实关闭Body；ETag单列，不冒充内容摘要。multipart只登记未完成上传事实，不完成或清理它。对象数、页数、单对象／总字节及单次读取时间均有显式预算；超限和变化留下不完整记录，不自动重启或声明全量通过。

私有JSONL清单独占创建并以摘要尾记录封闭，包含观测窗口、源范围、逐桶配置、对象版本及核验结果；不写凭据或签名URL。目标位置和复制状态保持未开始；九用途处理选择接受明确的迁移／重建／保留输入，缺失选择标为未评审，不自动决定缓存可删。业务引用、其他历史位置与全写入方覆盖未证明时保持未知，不把源端事实完整等同于迁移准备就绪。

运行时适配层只读查询已保存绑定与当前AsyncJob潜在写入方，先结束数据库观测再执行物理I/O；不持业务锁跨源端读取。结果明确其查询时间、截断及未知外部客户端／预签名上传，不能据此宣布写入窗口已封闭。

```text
九用途配置 + 保存绑定/作业观测 -> 只读源端清单 -> 私有JSONL + 风险摘要
                                      |
                            S3配置/分页/版本/条件GET
```

### 文件、验证与回退

实施文件固定为 `scripts/storage_migration_inventory.py`（纯读取/报告核心）、`scripts/test_storage_migration_inventory.py`、`scripts/inventory_storage_migration.py`（运行配置/DB适配）、`scripts/test_inventory_storage_migration.py`、`scripts/verify_storage_migration_inventory.py`（所属一次性实际演练）、`docs-site/ops/deploy/storage-migration-inventory.md`，以及DEV、README、CHANGELOG、本计划和执行队列。运维首页及侧栏加入正式手册导航；晋升修复backlog导航、路线图与基线链接。总数超过八文件，以同一源端清单合同交付；主要为脚本和文档，没有新表、迁移、公开API、UI、依赖、镜像或发布版本。

核心与CLI检查覆盖共享桶、九用途完整性、版本/删除标记、多页与循环标记、生命周期缺失/拒绝、精确版本与条件读取、正文/响应不符、预算、真实关闭及清单完整性。实际演练使用与C5不同的所属test模式，真实MinIO的中文键、历史版本、删除标记和未完成multipart；运行CLI时使用真正只读S3凭据，核对运行前后对象、规则及业务行不变，并验证源端写删拒绝。静态检查、文档构建、正常提交钩子及差异检查后按归属清理；不重复已有浏览器矩阵。

回退只需停止新CLI并保留或删除本轮自有清单；没有复制或切换状态需要撤销。测试失败如实保留不完整范围，不能改松源端验证来获得通过。后继按原始S02补齐尚未知的业务引用/历史位置与写入方事实；候选锁定、目标复制、全量切换、旧冻结版本和退役继续保留原始S01／S03–S06与AC要求。

## 1. 为什么做

本项目拟评估现有对象存储的替代方案；社区维护状态作为运维评估输入，在晋升时重新核查。当前平台在配置、部署和 UI 中直接使用 MinIO 命名；但后端使用 boto3，对 S3 兼容能力做契约验证后可以逐步解除供应商绑定。[MinIO 官方仓库](https://github.com/minio/minio)、[当前存储服务](../../apps/api/app/services/storage.py)、[部署配置](../../docker-compose.yml)

原地目录数据集与更换对象存储是两条可独立推进的工作。即使原图全部保留外部目录，平台仍需要存放头像、标注附件、预览、导入导出产物和冻结训练内容；因此不宜把退役 MinIO 简化为删除一个容器。

## 2. 选型结论状态

以下保留来源材料的候选方向，不表示完成本项目选型。许可、维护状态、稳定版本及兼容矩阵应在隔离 PoC 锁定镜像时形成证据；本次未部署候选。

| 候选      | 当前证据                                                                         | 本项目建议                                                                                                   |
| --------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| SeaweedFS | 提供 S3 网关；官方兼容矩阵作为测试输入，许可、通知和复制能力按所锁镜像逐项核查。 | 优先 PoC，重点评估图片小文件、部署运维、代理访问和应用生命周期契约。                                         |
| RustFS    | 官方兼容矩阵明确为经过测试的 S3 子集；许可与候选稳定镜像在 PoC 锁定时记录。      | 与 SeaweedFS 并行 PoC，重点验证故障恢复、升级、长期运行和应用真实流量。                                      |
| Garage    | 桶版本和保留能力按官方兼容表及所锁版本单独核查。                                 | 如选择轻量跨站点对象存储，且应用级不可变键可满足版本冻结要求，可进入备选；不能默认承接依赖桶版本 ID 的方案。 |
| Ceph RGW  | 多站点部署和运维成本按实际拓扑核查。                                             | 已有 Ceph 基础设施与运维经验时优先评估；当前部署规模未知，不作为默认新增依赖。                               |

证据：[SeaweedFS 仓库](https://github.com/seaweedfs/seaweedfs)、[SeaweedFS S3 API](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)、[RustFS Releases](https://github.com/rustfs/rustfs/releases)、[RustFS 兼容矩阵](https://docs.rustfs.com/en/reference/s3-compatibility)、[Garage 兼容矩阵](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/)、[Ceph 多站点](https://docs.ceph.com/en/squid/radosgw/multisite/)。

文档标注支持某 API，不代表本项目使用的全部参数、代理路径和错误处理都兼容。厂商基准不能替代本项目测量。许可证合规与镜像分发方式在正式选型记录中确认，不把许可类型直接等同于总体成本或适配质量。

## 3. 目标、范围与不做事项

- 形成可复跑、可比较的 S3 契约和运行验证结果，做出可解释的候选决策。
- 把平台所有受影响用途迁移到新位置，保留业务 ID、可读取对象和冻结版本校验值。
- 降低供应商名称在代码、配置与 UI 中的耦合，同时保持旧配置过渡兼容。
- 提供完整暂停写入、最终增量、切换、观察和回滚过程。

本期不进行存储引擎磁盘格式转换，不直接把旧 MinIO 数据目录挂给新软件，不要求迁移外部只读来源，不引入未经验证的多端双写。厂商磁盘兼容声明不作为本项目迁移证据；采用 S3 对象级复制和独立内容核验。[RustFS 官方说明](https://github.com/rustfs/rustfs/blob/main/README.md)

## 4. 用户故事

| 编号    | 用户故事                                                                     | 交付物                   |
| ------- | ---------------------------------------------------------------------------- | ------------------------ |
| E07-S01 | 作为维护者，我能用相同场景对候选执行应用级兼容测试并看到失败差异。           | 契约测试集、候选报告。   |
| E07-S02 | 作为运维，我能盘点所有桶、用途、对象、生命周期规则及活动写入方。             | 迁移清单和风险记录。     |
| E07-S03 | 作为运维，我能分批复制并断点续跑，失败项可定位，不覆盖未知的新对象。         | 可恢复迁移任务。         |
| E07-S04 | 作为平台负责人，我能在维护窗口切换全部写入和读取，并确认用户关键流程仍正常。 | 切换演练和发布记录。     |
| E07-S05 | 作为训练负责人，我能在迁移后打开旧冻结版本，校验内容与清单保持一致。         | 版本回归证据。           |
| E07-S06 | 作为运维，我能按明确的前提回滚，并在观察期结束后有依据地退役旧实例。         | 回滚手册和退役检查记录。 |

## 5. PoC 契约与决策门槛

| 类别       | 必测行为                                           | 通过条件                                     |
| ---------- | -------------------------------------------------- | -------------------------------------------- |
| 基础对象   | PUT/GET/HEAD、前缀分页、中文及特殊字符键、对象删除 | 字节、元数据、分页边界和错误码符合应用契约。 |
| 浏览器上传 | 预签名 PUT、CORS、过期签名、Content-Type、失败重试 | 在实际入口域名和代理拓扑中通过。             |
| 大对象     | multipart 建立/上传/完成/中断清理                  | 中断不形成误认成功的对象；重试不丢数据。     |
| 媒体读取   | Range、条件读取、Content-Length、断点和并发        | 图片/视频/训练包流程均可验证。               |
| 校验和     | ETag、独立 digest、multipart 和加密场景            | 不将 ETag 长度作为 MD5 真实性证明。          |
| 生命周期   | 平台当前临时文件过期与持久预览保留                 | 临时产物按规则清理，持久引用不误删。         |
| 冻结版本   | 已有训练 manifest、原件、标签与打包文件            | 内容校验一致，旧版本可下载/消费。            |
| 运维       | 重启、磁盘满、节点故障、备份恢复、滚动升级         | 报错和恢复行为有记录，无已确认写入静默丢失。 |

对象版本、标准 S3 通知、跨站点复制、WORM 等按真实需求列为“必须/可选/不使用”，不能把宣传的完整兼容性作为唯一评分。若训练冻结采用应用不可变对象键，桶版本功能可与训练版本分离，但必须证明冻结对象不会被覆盖或误清理。

决策采用先过硬门槛、再比较运维成本和性能：核心数据正确性失败直接阻断选型。吞吐、延迟、资源消耗在同一数据集、硬件、并发和镜像摘要下测量；数值目标由 M0 基线确认，本文不提供虚构跑分。

## 6. 迁移范围与数据清单

迁移按 `StorageBinding` 用途逐项核对：`annotations`、`datasets`、`bug-reports`、`media-cache`、`audit-archive`、`import`、`export`、`avatars`、`training`。当前存储 API 展示的桶范围不能当成完整迁移范围。[当前存储 API](../../apps/api/app/api/v1/storage.py)

每条清单至少记录来源位置、桶/键、对象版本（如有）、大小、内容类型、业务引用、独立校验值、目标位置、复制状态和验证时间。ETag 作为源端元信息单独保存。对没有可靠校验和的旧对象，读取并计算独立 digest；任何抽样验证必须明确未覆盖范围，不能标记全量内容已核验。

按用途决定哪些临时对象可重建：有持久业务引用的预览不能被直接当作可丢弃缓存。若源桶启用版本控制，需先识别业务引用到的版本与保留要求，迁移对应内容并建立新位置映射；不能只同步最新键值后宣称历史已迁移。

## 7. 切换步骤

1. **准备**：固定候选版本/镜像摘要；备份数据库、密钥恢复材料、位置配置和业务清单；在隔离环境完成恢复演练。
2. **初始化目标**：创建桶/前缀与权限、CORS、生命周期规则；先以测试用途验证访问域名。
3. **初次复制**：源保持当前服务，按清单分页复制；记录 checkpoint，不修改源对象和业务 ID。
4. **进入维护窗口**：关闭新上传签发、导入、训练打包、媒体生产及其他写入入口；drain 相关 worker，并处理在途 multipart。
5. **封闭写入窗口**：等待旧预签名上传过期或通过源端权限/入口阻断其写入；确认所有写入方均受控。仅 UI 禁用上传按钮不够。
6. **最终增量与核验**：补齐变更、核对键/大小/digest/业务引用；有差异不得切换。
7. **切换**：更新用途绑定与既有对象位置映射；保持切换版本一致，先冒烟读取，再恢复受控写入。
8. **观察**：记录新写入和来源；检查标注、导出、头像、视频资产、冻结版本及后台任务；保留旧实例只读和恢复点。
9. **退役**：满足观察期、业务确认与恢复演练后，先移除旧实例流量和写入权限；旧数据按明确保留决策处理，不自动删除。

观察期长度由项目实际写入频率和恢复目标评审决定，不能以“进程启动成功”作为退役依据。

## 8. 回滚边界

- **切换前失败**：暂停迁移，保持源服务；修复目标后从 checkpoint 重试。
- **已切读、未恢复写入**：恢复上一用途绑定与位置映射，核对数据库切换版本，回到源服务。
- **新端已接收写入**：禁止直接切回旧端。先再次停写，把新端差异反向复制并验证，或用可验证的对象位置路由保留两端读取，再执行受控回退。
- **新端数据受损**：按已演练的备份/副本恢复路径处置；不能把缺失对象自动映射到内容未知的旧对象。
- 数据库快照回滚必须与对象清单版本协同，避免撤销用户已完成标注或恢复过时位置映射。

## 9. UI 与 API 边界

迁移状态属于存储位置详情：阶段、对象数/字节、失败清单、最近 checkpoint、可操作动作。复用全局 AsyncJob 详情；数据集页仅显示本数据集受影响的读取/写入状态与深链。

管理 API 草案需包括迁移计划、dry-run 清单、启动/暂停/恢复、核验报告和受条件约束的切换。普通用户无权直接修改端点或发起退役。UI 中把“MinIO”改为实际配置的供应商/类型，“HTTP 开发环境”改为实际连接安全状态。

## 10. 验收标准

| 编号     | 可验证标准                                                                                           |
| -------- | ---------------------------------------------------------------------------------------------------- |
| E07-AC01 | 至少两个主候选有同版本锁定、同环境的兼容报告；所有必须项通过或有经确认的替代设计后才能选定。         |
| E07-AC02 | 九类用途都有迁移/重建/保留的明确记录；不存在只迁移 datasets 而遗漏训练、导入导出等用途。             |
| E07-AC03 | 随机中断复制与进程重启后可续跑；成功计数不重复累计，失败对象可单独重试。                             |
| E07-AC04 | 最终切换前关闭所有在途写入渠道，并证明旧预签名 URL 不能继续改变源数据。                              |
| E07-AC05 | 清单内要求保留的对象完成规定级别的内容核验；有未核验对象时报告明确且切换门槛不能被“总字节相同”代替。 |
| E07-AC06 | 迁移前后的样本 ID、标注关联、冻结 manifest 摘要与训练包校验保持一致，原件读取成功。                  |
| E07-AC07 | 真实浏览器入口和内部 worker 分别通过上传、Range、预览、训练下载及过期 URL 测试。                     |
| E07-AC08 | 演练覆盖切换后产生新写入再回滚；回滚完成后这些新对象及其业务引用仍可读且校验一致。                   |
| E07-AC09 | 旧实例退役前有恢复演练记录、保留清单与观察结果；没有执行未经确认的源数据物理删除。                   |

## 11. 工程任务与完成定义

- [ ] 盘点存储调用与九类用途，修复 ETag/digest 混用并建立兼容契约。
- [ ] 搭建候选 PoC，固定镜像摘要，产出数据正确性、性能和运维成本报告。
- [ ] 完成供应商选择 ADR，记录未采用候选原因和后续退出路径。
- [ ] 实现迁移清单、断点、核验和绑定切换；制定完整写入方停止策略。
- [ ] 演练切换前失败与切换后新写入回滚，确认数据和数据库引用一致。
- [ ] 更新部署示例、环境变量兼容说明、存储 UI 文案和退役手册。

完成定义：新对象存储承接平台托管用途，应用通过供应商中立契约工作；旧 MinIO 可按明确保留策略退役，已有目录数据集独立于此迁移继续运行。

## 本项目推进切片与晋升条件

试验先具备隔离桶/前缀、专用凭据和可清理容量；切换另需所有写入者清单、备份/密钥材料、停写窗口、清单覆盖和回退方案。默认对象级复制+停写最终增量，不引入未验证双写。候选试验和生产选型均未完成。

| 切片    | 独立结果         | 交付范围                                                                                 | 验收 / 回退边界                                          |
| ------- | ---------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| E07-A   | 源端只读清单     | 当前配置源的九用途、全部版本与删除标记、规则和multipart、完整SHA及有界DB观测。           | 本分项已完成；未知覆盖保留，不是完整S02或迁移许可。      |
| 候选PoC | 隔离候选报告     | SeaweedFS/RustFS 使用同数据与代理拓扑完成 S3 契约、性能/恢复对照，锁镜像摘要与许可文件。 | 不改变现有流量；正确性硬门通过；报告不把宣称当实测。     |
| E07-B   | 全用途清单与演练 | 定位、读取和 E12 恢复门通过后，可续跑复制、停写、最终核验及带新写入回滚演练。            | 九用途完整；旧 PUT 无写入窗口；回滚保留切换后新对象。    |
| E07-C   | 生产切换与退役   | 另行确认维护窗口/观察期，保留旧端只读和恢复点。                                          | 未确认保留策略或仍有引用时不删旧数据；退役门独立于切换。 |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_public_url.py](../../apps/api/tests/test_storage_public_url.py)、[test_datasets_upload_zip.py](../../apps/api/tests/test_datasets_upload_zip.py)、[test_training_datasets.py](../../apps/api/tests/test_training_datasets.py)、[test_training_storage_deletion.py](../../apps/api/tests/test_training_storage_deletion.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)、[storage-buckets.md](../../docs-site/dev/reference/storage-buckets.md)、[env-vars.md](../../docs-site/dev/reference/env-vars.md)、[DEV.md](../../DEV.md)、[README.md](../../README.md)、[.env.example](../../.env.example)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome — E07-A 源端只读迁移清单

2026-10-25，本地源端分项已交付；核心集成`aefb7d245`，CLI、实际适配修正及正式手册见本Outcome所在提交。不分配发布版本。九用途按共享桶去重，记录全部版本／删除标记、规则与未完成上传，条件读取和前后HEAD核对后完整计算SHA；私有JSONL独占创建，受摘要覆盖的summary与tail必须一致。数据库只读观测先结束，其他绑定、潜在作业及未知覆盖分别表达。

最终40个不同辅助用例通过，覆盖核心、CLI、尾部状态／计数篡改、真实流EOF后的socket边界、明确null版本与非null精确回显、实际对账时间归一和空演练夹具守卫。新增守卫用例首轮误断言异常基类，已改为实际ValueError并通过；守卫不改变已验证的读取核心。

真实PostgreSQL／MinIO演练使用所属test环境和独立只读S3账号，经正式CLI的每页1项完成九桶、12个对象版本／217字节、1个删除标记和1个未完成上传，所有版本SHA与预期相符，22页、零issue；真实PUT／DELETE均403。已保存但不被配置源覆盖的avatars绑定和一个潜在export作业准确出现；限额清单有效封闭但complete=false，正式CLI退出1。运行前后源对象、版本、规则、上传事实及165表13行合成配置／作业完整指纹保持；此结果不代表生产迁移或冻结版本回归。

首轮实际适配暴露EOF后设置socket超时和null版本响应头省略，修正并补回归；后续两轮演练夹具分别错误比较verified_at、从可省略的PUT响应头推断null槽位，均修正后最终通过。没有把失败计为成功，没有放开写权限、关闭条件读取或把null槽位称为不可变版本。每轮自有test资源均清理，实际创建的临时账号独立确认撤销。

正式知识见[运维手册](../../docs-site/ops/deploy/storage-migration-inventory.md)、DEV、README及CHANGELOG Unreleased。Ruff／格式、文档frontmatter／可读性、完整文档构建及只读复核通过；最终资源独立清理、提交钩子与diff结果随本次交付记录。人工环境及C5原进程保持，未改变应用／Worker或六份冻结工具，没有重建镜像。

剩余仍按原始E07：完整业务引用、历史位置和全写入方覆盖，九用途最终处理评审，候选锁定／对照PoC、实际复制与续跑、停写最终核验、切换后新写入回滚、旧冻结版本及退役资格。本片仅完成选定配置源的观测，不勾完完整S02、AC02或工程迁移项，也不新增Tracker、会话或审核前置。

最终独立复查：所属test数据库、Redis和九桶均不存在，私有原始清单／日志、构建产物和测试缓存已清理，仅留有界核验摘要。C5所属e2e资源及人工环境未操作。

## Outcome — E07-B 已保存业务定位目录

2026-10-25，本片在隔离树实现，交付提交见本Outcome所在提交；只增加五个工具／检查脚本并同步六份现有文档，不改应用、SDK、迁移或原三个源对象清单脚本。正式知识见[运维手册](../../docs-site/ops/deploy/storage-migration-inventory.md#已保存业务定位目录)、DEV、README和CHANGELOG Unreleased。不分配发布版本，B8仅为测试依赖祖先，父树应用仍按C6冻结约束独立集成。

24类保存归属使用全状态静态投影与稳定键分页；JSON对象回执有界展开，私有冻结业务文档保留未展开原因。旧表可见列探测兼容0244／0245，单个只读重复读快照在源报告处理前退出。独立JSONL以0600独占创建并封闭摘要，源报告只打开一个描述符进行校验、索引和完整文件摘要，私有SQLite索引随临时目录清理。原始S3键不解码／规范化，保存声明、当前登记身份、准确版本／可变null槽／未固定键／前缀及未知结果分列。

42项不同纯核心／CLI检查通过，覆盖登记家族、白名单与旧列、事务关闭及总超时、原始键／前缀／版本、摘要与同描述符路径替换、内容回执冲突、未知与预算封闭、实际写出计数、私有输出及临时目录清理。Ruff／格式、文档frontmatter／可读性、完整构建和链接检查已通过；最终正常提交钩子与diff检查随本次提交执行。

实际只读DB资格两轮通过：0244枚举24家族、2条声明（准确版本1、未固定键1）；0245枚举24家族、8条声明（准确版本2、可变null1、未观测1、源外1、前缀范围1、未定位1、未固定键1）。保存schema2报告附件在当前连接端点改变、位置停用后仍按保存端点准确匹配；退休及孤立位置关联、敏感配置／文件名排除、0600、私有spill清理及只读UPDATE／敏感列SELECT两次拒绝通过。CLI前后165表、源端14个对象版本的完整字节指纹及配对源报告摘要保持；最终head0245、12行及零临时reference角色由独立所属环境读取确认。PostgreSQL16.14、boto3／botocore1.42.96、MinIO RELEASE.2025-09-07T16-13-09Z；共享宿主机服务，非生产／远程CI资格。MinIO拒绝字面dot-segment PUT，实际夹具使用不同大小写的百分号键；字面`..`与重复斜线由纯报告用例验证，不扩大提供方声明。

所有轮次仅使用子树`5d342caffb761f8f:test`；结束恢复head、撤销确切临时角色并按confirmation销毁所属数据库／Redis／九桶，随后独立后端检查确认不存在。源报告、目录、SQLite、合成载荷、构建和检查中间产物清理；保留隔离Git树供集成。未启动API／Web／Worker／浏览器，未操作父树服务或冻结载荷。

范围保留：完整S02／E07没有勾完。目录完整枚举不等于完整历史业务定位，私有冻结文档、缺归属、未知schema和未固定版本保持明确未知；外部写入方、旧预签名上传、停写、复制、目标后端与生产迁移均无新增证明，migration_ready始终false。未匹配不推出孤儿或删除结论，不新增历史回填或消费者改造门槛。工具回退为停用新CLI并按保留规则处理自有目录，无绑定／业务状态要回滚。

## Outcome — E07-B2 本地初次复制与可恢复核验

2026-10-25，以原主树 `860128595` 创建隔离树，仅交付定稿的5份新工具／检查和6份文档，不改应用、数据库schema、Worker、依赖、环境配置、镜像、绑定或冻结manifest。复用E07-B同FD封存读取及SourceIndex，完整绑定源清单／配对目录，私有SQLite持久保存处理选择、逐源版本意图／nonce、实际目标版本及完整核验回执。

- 34项核心与8项CLI检查，共42项不同检查通过：原始键／版本、输入配对及损坏、同桶处理歧义、跳过事实、私有文件与锁、幂等计数、字节／对象预算、源变化、流关闭、应答丢失、保存确认版本续跑、目标历史冲突及核验失败保留回执；参数和目录摘要不能把私有字段带到终端。
- 所属空test在0244运行新CLI，九用途包含一个共用桶；5个源对象版本覆盖空对象、中文／字面百分号／`%2F`键、同键历史版本和mutable null，另保留1个删除标记和1个未完成multipart。当前MinIO拒绝含字面`../`段的PUT，因此该键语义保留在纯检查，实际夹具不绕过服务端限制。
- `before_item`、`before_put`、`after_put`、`after_item`四个硬退出窗口逐一以新进程续跑，均得到5项verified且每目标键只有一个版本；PUT确认窗口按创建nonce、完整字节与实际版本认领。SIGINT与SIGTERM分别暂停并重启成功，单失败项默认不重试，仅显式旗标重试；既有verified不重传。
- 实际共观察40个目标版本，其中39个由CLI完整SHA／大小／版本／元数据核验，1个为故意预置的foreign冲突；夹具另独立GET核对四个硬退出计划的20个目标版本。源账号实际写／删拒绝2次；源对象、版本、规则、未完成上传、两份输入摘要及165张业务表指纹不变。未知目标未被覆盖，不声明业务消费者或生产目标资格。
- Ruff／格式、正式文档frontmatter／可读性／导航、标准`pnpm docs:build`、正常提交钩子与`git diff --check`通过。只保留有界脱敏汇总；临时账号、独立目标桶与精确计划暂存已清理并核对不存在，所属test数据库／Redis／九桶随后由官方confirmation销毁，复核独立缺席。人工3100／8100、Worker／Beat及C6原进程未改动。

结果仅为初次预复制：`migration_ready=false`始终保持。SeaweedFS／RustFS双候选同环境报告、真实部署目标、最终停写／增量、业务定位切换、新写入回滚、旧冻结业务资格与退役仍是原始剩余项。回退仅停用本工具并保留checkpoint和目标，不反向清理源或其他目标。

## Outcome — E07-P1 双候选本地 S3 契约与运行基线

2026-10-25，在基于 `8df3de5c4` 的隔离树交付，提交见本 Outcome 所在提交；只改合同指定的 12 份 authored 文件，另三份固定输入沿用 `39f12a889`，未改应用、Compose、后端镜像或冻结八工具。正式命令、完整案例表、预算与清理入口见[候选本地验证](../../docs-site/ops/deploy/storage-candidate-poc.md)，同步 DEV、README、侧栏、运维首页及 CHANGELOG Unreleased；不分配发布版本。

最终同一测试器修订于 UTC 04:05:25–04:06:58 顺序运行：SeaweedFS 19 passed、0 failed、6 not_run，本地基线通过；RustFS 18 passed、1 failed、6 not_run，唯一失败为显式 SSE-S3 AES256 PUT 返回 `InvalidRequest`，本地基线未通过。当前平台没有请求 SSE，此失败仅表达本次无外部 KMS 的实际探测，未修改候选代码、删除案例或把失败改为通过。50 条记录的执行完成与本地基线分开，`selection_ready=false`、`production_ready=false`。

SeaweedFS 使用锁定变体，镜像默认 FoundationDB 配置显式覆盖为所属数据卷 `/data/filerldb` 的 LevelDB2；配置 SHA 为 `1b4134bfbfb74ad72eb256148aac9c572d179ded70dbad26ba2cbeff93757387`，恢复使用同一配置，不启动 FoundationDB。实际二进制 `version 8000GB 4.49 7154732 linux amd64`、OCI revision `b97ed0f1014050c41b3a7f0b8c0c91f579a79998`、发行提交 `b65832bdc9bbbcf3682465b1be598627ac386206` 是三种不同证据。RustFS 实际 `1.0.3`，二进制输出 git commit `331fb896285a7686f927baf8d9d4dc2d3d3a10a9`，镜像没有 OCI revision label；输出与发行提交相同仍不构成独立构建证明。原始许可 SHA 保持，许可仅对应已锁源码声明，不冒充二进制证明。

实际 boto3 及当前 StorageService 验证内部／ML和绝对公共 Host、相对前缀恢复内部 Host 两条 Nginx 路由；PNG、短 MP4 和 ZIP 逐完整 SHA 与解码／CRC 通过。四个真实浏览器场景完成 PUT、GET、206 Range、CORS 预检、响应头暴露、403 拒绝后新签名重试，页面新增异常为空。版本、null、删除标记、并发条件创建、multipart 错误完成重试／abort和当前应用实际生命周期配置回读均通过。原生到期、真实部署 TLS、磁盘满、多节点、滚动升级及已有冻结业务消费者各候选明确 not_run；配置回读不是到期证明。

两候选实际重启、写入中强杀后均完整回读 6 项确认对象（含 2 个不可变版本），未确认 32 MiB PUT 在仅发送 262,144 字节后均不存在；停稳数据卷备份分别 15,038,464／16,241,152 字节，恢复到新的同提供方卷后逐保存版本核对，不称跨供应商磁盘格式迁移。每性能单元相同 8 对象、2,752,792 字节，单路／四路 PUT／GET 均零 HTTP 失败；报告保留实际字节、延迟、31／127 个 CPU／RSS 主进程样本及短窗口限制。Docker 29.9.2、Python 3.12.11、boto3 1.42.96，112 逻辑 CPU／270,062,034,944 字节宿主机内存；C6及其他工作、部分文档构建共享宿主机，不推断生产 SLO。

开发试跑纠正了测试器的 Docker 缺失资源大小写判断、boto3 的 bytes→BytesIO 包装、可选镜像 Cmd 字段、重启随机映射端口、HTTP 头大小写与默认 FoundationDB 启动适配；准备不足的早期结果不计最终候选验收。恢复守卫拒绝空夹具或缺不可变版本，报告绑定两个测试器自身及实际 StorageService／签名／lock完整 SHA。34 个不同纯回归通过，含 lock／许可损坏、正文／元数据／分页、预算、敏感信息、owner／label／外部使用者、清理中断、原子写硬杀残留及真实签名的字面点路径合同。实际执行报告保留执行时的脚本 SHA；其后只补 manifest 读取入口的 nonce 格式守卫，覆盖所有资源已不存在时仍不得绕过 nonce 校验删除目录的分支，仅运行对应纯回归，不重跑提供方案例，也不把最终工具修订冒称原执行字节。

Ruff／格式、正式文档 frontmatter／可读性／导航、完整标准文档构建与链接检查通过；正常提交钩子及最终 diff 检查随本次提交完成。每次退出按持久 manifest 核对精确 ID、checkout、镜像和 label，已确认全部所属容器、卷、网络与浏览器 session 结束，私有正文／凭据／原始日志和构建缓存清理，仅保留固定镜像、有界报告及所属核验摘要。人工 3100／8100、数据库／Redis／MinIO、Workers／Beat 与 C6 原句柄 27154 未操作。完整 E07、供应商选择及生产迁移继续活动，不增 Tracker、会话、审核、模型资格或其他前置。

## Outcome — E07-B3 当前九用途保存定位目录

2026-10-25，基于`0832098bb`补齐原始E07-S02／AC02、AC05及§6的当前保存定位目录。实现来源`875e55a54`、`fcbf75283`，最终集成与当前真实库验证脚本随本Outcome提交。正式命令和边界同步[迁移清单手册](../../docs-site/ops/deploy/storage-migration-inventory.md#已保存业务定位目录)、README、DEV及CHANGELOG Unreleased；不改应用、API、schema、依赖或部署配置，不分配版本。

目录由24类扩至27类，新增Mask上传、Mask导入批次与Thumbnail责任，逐家族识别九用途实际保存结构。Dataset／Auxiliary的schema1、其他当前schema2、旧NULL及未知结构分开；Creation跨端点暂存沿独立staging_ref，不沿最终原件端点。原图缓存的payload、完整物理inventory键、预测相对键、保存版本及key_prefix受控根均按实际合同解释；非所属前缀内的inventory对象保持未知，现有copy plan不得将其升级为准确业务引用。报告格式及预复制checkpoint合同不变，migration_ready始终false。

146项不同纯／CLI检查分两次新进程通过：本片目录／复制106项，原源清单40项；一次合并进程执行受既有SQLAlchemy模拟导入顺序影响出现两项失败，未冒称该次全绿或改动无关测试。旧0244／0245形状兼容由纯检查覆盖，历史实测记录保留；旧资格脚本不冒充当前head验证。Ruff、格式、正式文档frontmatter／导航／可读性、标准文档构建及diff检查通过，正常提交钩子随最终提交执行。

当前0254实际验收仅使用主树所属空test：SQL枚举全部27家族，其中15家族有合成记录，检查17条预期定位，生成22条引用（准确版本10、未固定键6、前缀3、源外1、未定位2）。当前登记端点不同且位置停用时仍使用保存地址；跨端点暂存、未知历史及越界inventory保留各自事实。现有copy plan接受8条准确关联，越界项不成为准确声明。CLI前后172张表、15个源对象版本完整指纹和输入清单SHA保持；写入与秘密列读取分别实际拒绝，0600输出及私有spill清理通过，确切临时角色已撤销。该夹具只验证目录，不代表全部业务工作流或迁移切换。

已按`1822b025369a33f4:test`销毁所属测试数据库、Redis和九桶，独立后端检查确认全部不存在，资源manifest及运行会话均移除；临时目录、源报告和复制计划由上下文清理，仅保留有界核验摘要。人工3100／8100与两个Worker／Beat保持原运行环境，无本片应用刷新或镜像重建。E02剩余的S05／AC08保存target维护影响独立推进；E07完整选型、实际迁移、停写／增量、业务切换、新写入回滚和退役仍未完成，不新增训练测量、Tracker或全库历史接管前置。

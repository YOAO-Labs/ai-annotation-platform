# EPIC-02：存储位置、连接配置与业务用途绑定

> 状态：E02-A completed locally；E02-B1/B2/B3/B4 completed locally；用户已授权持续推进和提交，不分配发布版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](backlog/README.md#promotion-gate)。[草案总览](backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](backlog/1792661016_dataset-storage-architecture.md)和[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

> 优先级：P0（M1 基础）；负责人角色：后端、运维，前端协作。
> 依赖：EPIC-01 的标识与适配契约；EPIC-11 的最小权限规则必须随 M1 一起交付。
> 下游：EPIC-03 原地注册、EPIC-05 读取、EPIC-07 迁移、EPIC-09 存储控制台。

## 本轮实施定案：E02-B5 预览、样本评估与主动学习输出归属

在干净基线 `2d49fa56e` 继续整套实施授权。剩余training用途消费者分别持有三类独立输出：TrainingPreview的 `projects/{project}/previews/{preview}/` 图片和报告，SampleEvaluationRun的 `projects/{project}/sample-evaluations/{evaluation}/` 各attempt报告，以及ActiveLearningRound的 `projects/{project}/active-learning/{round}/media/` 跨attempt复用候选图片。本片整体固定这三个根前缀；输入训练版本、来源模型和候选原件继续由已有独立所有者负责。

0222为三个现有记录增加可空内部storage_ref，沿用0220/0221的严格envelope与不可变/受限恢复触发器，purpose分别为training-preview、sample-evaluation、active-learning。复用已实现的共享分配/候选/端点和键检查，薄适配器保持三类键形状；只有显式新建入口分配，new_attempt、重放和重试不得默默为旧NULL记录猜测。原冻结spec、上传/推理wire、内容摘要及模型/人工证据不变，不新增服务、配置、凭据、依赖或镜像。

全部输出签名、上传、回读、报告缓存命中前核验、候选PNG读取/复用和临时报告候选清理使用保存桶并严格检查所属前缀/attempt或ordinal。NULL及端点/身份变化在物理I/O前返回明确冲突；旧记录仍可查看元数据、取消或按原围栏确认失败/退出。主动学习遇到未知归属不得先读原件重造候选；重试不改变固定派生SHA或原推理请求/预算身份。通用JSON读取/内部下载的默认桶只保留给仍明确未转换的用途，本片三个所有者不使用默认值。

历史仅通过项目负责人显式storage-pin接管当前保留副本，分别置于原三类资源下。记录及当前attempt须终态、无保留CPU租约/活动claim，预览须等已有签名PUT窗口关闭。旧attempt须终态，或为非current、更旧generation、claim已清空且completed_at已保存的封存lost；评估正常终态保留的claim不是活动claim。这只证明DB发布围栏，不能声称旧物理线程已关闭。候选只读持久binding；核验全部前缀，按独立发布/上传清单或冻结候选派生SHA检查实际完整字节、大小（有独立记录时）、metadata及已保存语义，未知额外键、已发布缺失、变化均拒绝。旧评估报告用独立发布回执核验身份，不假定跨重试共享Sample行仍保存每次旧报告的执行证据；当前报告继续完整语义复核。失败/取消的未发布对象可缺失，存在仍须核验；主动学习已持久media/成功候选要求其PNG存在，其余已知成员的残留PNG按冻结SHA核验。只从保留副本验证，不读来源补证或修复，也不反序列化权重。沿用Reader、共享进程容量、维护permit、真实关闭及发布前权限/Key、记录/attempt/候选、binding/Gate修订复核。

保留现有耐久清理责任：Preview行在进程退出与PUT窗口关闭后才可过期清理；SampleEvaluationRun行保留deletion_requested_at直到保存前缀删除完成，并继续检查主动学习保留引用。未知归属保留行并明确拒绝物理删除，后台逐项失败不阻断其他合格项。主动学习本来没有删除入口，本片不新增。离线恢复把三个归属纳入同一bucket/identity白名单，保护全部嵌套业务/证明事实；已有任一新归属时拒绝破坏性降级。

本片超过8文件，按迁移/领域、预览消费者、评估及主动学习消费者拆到基于当前本地HEAD的独立工作树；主树负责显式接管、旧夹具、恢复、正式文档/合同与实际验收。最小选择是分别保存bucket，但无法识别端点变化或保留绑定/历史核验事实，因此复用现有严格envelope；不引入另一套通用对象表或复制冻结语义。最脆弱前提是旧清单足以独立解释当前副本；不满足时保持NULL与读取/清理拒绝，不能从对象文件名推断可信内容。

```text
新建 Preview / Evaluation / Round -> 对应固定 storage_ref -> 专属输出前缀
            |                              |                 |
       独立输入 owner                 显式旧副本核验     回读 / 缓存核验 / 复用
                                           |
                        原有耐久清理行 / 受限离线映射
```

验证包括三个创建入口、重试/重放NULL拒绝、独立输入与输出/来源桶、同键不同桶、报告/PNG完整回读、候选回滚与提交不确定性、PUT未过期/CPU租约/活动claim拒绝、清单缺失/额外/变更、撤权/Key变化/Gate排空/取消真实收尾、逐项清理失败及恢复白名单。只使用核实owner的一次性test资源；真实HTTP/MinIO、两个当前Worker和浏览器验证输出路由及保留副本，不声称真实模型框架训练资格。正常hooks、Ruff、生成API/SDK合同、文档及diff检查通过后提交。人工3100/8100、两个Worker/beat、既有业务与原件/冻结字节保持；迁移及源刷新只在所属环境执行。回退保持新增事实和未知归属拒绝，关闭新写入即可，不恢复全局桶猜测；完整用途切换继续关闭。

## 本轮实施定案：E02-B4 训练实验输出的固定归属

在干净基线 `bfcd8d083` 继续执行已有整套实施授权。当前TrainingRun的权重、检查点、评估/原生配置/观测摘要及脱敏日志仍按全局training_bucket读写；模型和初始化只引用这些原对象。下一片固定每个实验的projects/{project}/runs/{run}/整个前缀，以同一owner覆盖所有attempt、重试续训、模型安装/下载/初始化、评估及删除责任。数据版本仍使用已有独立归属；增强预览自己的输出、基础权重及其他用途另行收敛。本片不开放整个用途切换，不改Executor/共享训练合同、冻结config/provenance/初始化pin，不重建镜像。

0221为TrainingRun增加可空内部storage_ref，沿用现有locator envelope，purpose为training-run且prefix严格按项目/实验UUID限定。新建API、自动策略、参数搜索三入口在首次执行/I/O前显式固定；普通new_attempt/续训不能为NULL旧位置自动猜测。所有run输入输出URL、物理读取和派生脱敏日志使用保存端点/桶，模型通过RESTRICT来源run/attempt解析归属，不复制物理信息进冻结语义。先复核所属attempt键再I/O；NULL或身份变化明确拒绝，但取消/故障收尾仍保留原资源租约规则。

历史只接受负责人显式POST /projects/{project_id}/training-runs/{run_id}/storage-pin。全部attempt终态且无保留GPU租约后，沿用有界条件完整读取/真实收尾的Reader及维护permit；候选仅取已持久绑定。逐项核对DB已发布或上传清单的SHA/大小/attempt身份，已发布对象缺失、未知键及未解释上传拒绝，不反序列化权重。脱敏日志只在受支持当前确定性变换可从独立核验的源日志重建完全相同字节时接管；未知历史修订不猜测。发布前复核操作者/Key、实验及全部attempt指纹、候选/Gate修订，保存当前保留副本证据，GET不写接管。

删除同事务将run归属复制至独立tombstone，提交后清理；NULL旧run/dataset意图不做物理删除。受限离线恢复只映射bucket/identity，保护全部其他嵌套事实，新增run合成恢复证据。有run归属/意图时迁移拒绝破坏性降级。回退保留additive事实与读取拒绝，不重新采用全局桶。

本片超过8文件：领域/迁移、实验流/观测和模型/初始化/评估分配到当前HEAD的独立工作树；主树负责旧副本接管、清理/恢复、集成/旧夹具、正式文档与真实环境。复用现有owner/Reader/角色权限，不新增依赖、环境变量、凭据或服务。

```text
API / 自动策略 / 参数搜索 -> persisted run.storage_ref -> attempts / uploads / finish
                                      |                        |
旧实验显式核验 -> 同一归属             |                        v
                        模型 / 初始化 / 续训 / 报告 / 日志读取
                                      |
                  committed tombstone -> 同端点清理 / 离线受限映射
```

验证覆盖新建三入口、旧NULL拒绝、同键不同桶、续训和初始化分开归属、模型/报告/日志、旧副本完整/缺失/冲突/撤权/维护/真实收尾、提交失败零删除、失败意图持久、受限迁移/恢复。使用核实owner的一次性test环境，真实API/MinIO上传、摘要核验及URL下载证明路由，不以合成executor回执声称真实框架训练资格。正常hooks、Ruff、OpenAPI/SDK、文档与diff检查通过后提交；清理任务产物，保留人工3100/8100、两个Worker/beat及既有业务字节，再记录Outcome。

## 本轮实施定案：E02-B3 训练版本冻结前缀的固定存储归属

在干净基线 `3ce93cad0` 上复核并实施，沿用整套 Epic 持续实施和提交授权。现有版本仅保存 package_key，ZIP、独立冻结图片/掩码、派发与预览源、重试及删除均可能重新读取当前 training_bucket。新片固定版本自有前缀的归属，不开放整个 training 用途绑定切换，不修改 Executor/共享训练合同，不重建 API/ML 镜像。

0220为 TrainingDatasetVersion及TrainingStorageDeletion增加可空内部 storage_ref；结构保存已登记平台Location/配置修订、training绑定代次、精确endpoint/bucket/prefix/物理身份及new或verified-retained来源。新版本在首次物理写入前于创建事务固定；所有重试、取消恢复、包发布及未知提交应答清理始终使用该归属。prefix严格为projects/{project}/datasets/{version}/，版本字段不可被普通更新改写。解析旧归属只核对实际同端点client，不按当前training_bucket或新绑定重新解释；端点、未知身份或非法子键在首个I/O前拒绝。

迁移不根据环境变量回填历史。增加项目负责人显式POST /projects/{project_id}/training-datasets/{version_id}/storage-pin，GET保持零接管写入；候选只取已持久training绑定及平台位置，不顺带bootstrap。排除pending/running，先取消并等实际构建退出；使用同一版本advisory执行锁，在物理核验前后比较版本状态/attempt/snapshot/manifest/package与候选指纹，最终CAS保存归属与核验范围/清单摘要，不改snapshot、manifest或对象字节。已有pin幂等，普通重试不自动接管未知旧位置。失败/证据不足保留所有对象与记录。

显式接管有界盘点整个精确前缀，条件完整读取、SHA/大小、关闭及末尾盘点复核后才提交。ready必须验证发布ZIP完整摘要/大小、唯一安全成员、内外manifest一致、逐图/标签，以及必需保留媒体/掩码；合法媒体集以全部snapshot样本为准，去重不能把未入ZIP样本误判垃圾。多余未知键、无法解释的旧attempt包、缺失必需文件或冲突均拒绝。failed/cancelled允许尚未物化成员和空前缀，但已有副本须对应固定snapshot的独立SHA及实例内容；只有对象自身metadata.sha256而无独立内容证据时返回legacy_content_identity_unproven，不扩大为已证明历史原始位置，也不隐含新增源读取。接管只表示核验当前保留副本，不证明独占或未来不可变。

下载、analysis图片/掩码（包括sample evaluation）、训练assignment.dataset_url与增强preview源URL统一使用版本归属，不改历史preview.spec/input_digest或冻结run.config。删除在原事务复制storage_ref至独立tombstone，提交后才物理清理，身份不符或未知旧dataset清理意图不执行；训练运行产物的原清理合同独立保留。新版本尝试清理仍先复查已发布package引用，提交确认丢失不能删除已发布包。

离线恢复明确验证源版本及意图的归属，再在既有物理映射白名单中改bucket/identity并记录映射账本；不得改prefix、接管证据、snapshot、manifest、包SHA和业务ID。数据库只允许拥有表的恢复工具在显式事务标记下进行这一受限物理映射；运行角色不能改已固定归属。未知旧dataset tombstone保持待核对，不凭当前配置补造端点后删除。

```text
version creation -> persisted storage_ref -> export Worker -> retained media/masks + ZIP
legacy explicit pin -> bounded full verification -> same persisted storage_ref
                                                            |
           download / image-mask read / run source / preview source
                                                            |
          committed deletion intent(ref) -> same-origin cleanup
          offline restore(ref) -> validated physical mapping ledger
```

范围超过8文件。领域/迁移子代理在当前HEAD隔离树负责两模型、0220、training_dataset_storage解析/分配及DB回归；消费者子代理在独立树负责Worker、analysis、training run与preview源及针对性回归；主树负责API显式接管、严格旧副本核验、删除/恢复、集成测试及正式文档。无新依赖、环境变量、凭据owner、镜像或用途切换。现有Fernet/MinIO凭据不复制到ref；3100/8100、业务数据、两个Worker与beat保留。

验证：新创建/重试跨当前桶变化仍使用原位置，同键陷阱拒绝；图片/原生RLE/派发/预览/下载各入口；旧ready与部分/空接管、去重、元信息自洽但无独立SHA拒绝、未知attempt/键、变更/预算/关闭异常不接管；删除提交失败零I/O、失败意图保留、重试不碰其他桶及发布应答未知保护；迁移/不可变约束、恢复映射与源事实保持。仅用核实owner的一次性test命名空间，实际Celery+MinIO构建/取消恢复/旧副本接管/选桶与隔离恢复；正常hooks、Ruff、OpenAPI/SDK合同、文档与diff检查。按owner清理测试与等价集成树，刷新所属Worker并确认原人工内容不变后追加Outcome。

回退保留additive列和固定事实，未知身份不降级回全局桶；已有固定归属时迁移拒绝破坏性降级。完整E02还需其余用途owner、切换/迁移及浏览器写入资格，E10真实框架加载集合对账仍需另行部署资格，本片不称完成。

## 继续实施定案：E02-B1/B2

基线 `61d8ed982`，工作区干净，迁移单头 0209。E03/E04、严格原件读取、来源诊断及筛选冻结已落地；本轮不重复它们，也不因本轮交付开放用途绑定切换。人工环境 3100/8100 与其业务数据保留；Orca 中继不可用，修改串行完成。不构建镜像、不改 Compose、不引入依赖。

- B1：0210 为 StorageConnection 增加独立 revision，既有行初始化为 1。GET/列表返回 revision；PATCH 必填 expected_revision，缺少返回422，旧修订返回409。所有接受的编辑递增连接修订；仅实际配置变化或显式轮换凭据才递增关联位置 config_revision。旧 API 调用者必须先读取修订；不提供忽略 CAS 的兼容旁路。
- B2：更新配置或凭据时使用候选副本，复用白名单、Fernet 和现有 S3/SFTP 连接测试。外部 I/O 在 connection/location 行锁外执行，整体等待有上限；S3 只列至20项且关闭客户端，SFTP 认证/通道设超时并按迭代器最多取20项，不全目录列举。这里只证明 API 节点能认证并列举，不证明 read/range/write/预签名或扫描 worker 能力。
- 验证成功后按 connection → location 加锁，重读账号/管理权限、API Key 原证明、连接 CAS、最新白名单与位置引用；失败、超时、撤权或竞争均不覆盖原配置/密文。受引用连接仍拒绝实际配置改向，保持名称编辑与凭据轮换。错误只返回固定分类，不回吐底层异常/凭据/签名 URL；现有测试入口同步使用有界、安全分类并拒绝旧修订结果。
- UI 编辑固定打开时的 revision，保存失败保留草稿；冲突明确要求取消后重新打开，不自动把旧草稿套在新 revision 上。名称单独编辑不发送配置；配置/凭据保存显示验证说明。列表测试结果按 revision 隔离，防止旧成功显示在新配置上。

范围超过8文件，准确 owner：`apps/api/app/db/models/storage_connection.py`、`alembic/versions/0210_storage_connection_revision.py`、`schemas/storage_connection.py`、`services/storage_connection.py`、`api/v1/storage_connections.py`、`tests/test_storage_connections_api.py`、`tests/test_storage_locations_api.py`、新增 `tests/test_storage_connection_updates.py` 与 `tests/test_migration_0210_storage_connection_revision.py`；Web `api/storageConnections.ts`、`components/connections/StorageConnectionsPanel.tsx` 及其测试，受新增必填 revision 影响的测试 fixtures；OpenAPI 快照/本地类型、README、CHANGELOG、三份连接器用户/API/架构文档与两份执行计划。若读取发现其他契约消费者，按必要兼容更新记录。

```text
编辑表单（固定 revision） -> PATCH + expected_revision -> 候选配置/密钥
                                                        |
当前权限/白名单 -------> 有界 API 节点列举验证（行锁外） <-+
                                                        |
账号/Key/白名单复查 -> connection CAS -> location 引用保护 -> 原子发布
                                                        |
                       独立连接 revision + 必要的位置 config_revision
```

验收：隔离 test 数据库验证迁移往返/历史密文不变、缺失修订、两方旧表单竞争、失败/超时保留旧值、验证中并发更新/删除/撤权/白名单变化、名称不失效探测、引用改向拒绝及安全错误；复用旧连接/位置/原地 S3/SFTP 回归，Web 验证固定修订和保存草稿、测试结果隔离，SDK/OpenAPI、类型、lint/CSS、文档与 diff 检查。真实隔离 e2e 环境用受控本机 MinIO 端点验证正确/错误凭据轮换、旧版本冲突与原值保留，检查编辑弹窗在桌面/窄屏和明暗主题的截图、溢出、控制台/请求。只清理本轮测试资源。随后迁移人工环境并确认可用，不重建镜像。

回退：先回退客户端/API 后可保留 additive revision 列；需要降级迁移时只移除 revision，不触碰密文和位置数据。已生效的外部凭据不自动撤销旧凭据；外部凭据撤销及运行任务兼容窗口由部署者控制。完整 E02 仍保留用途历史定位、绑定切换、写入/浏览器能力资格及停用影响工作。

## 本轮实施定案：E02-A

基线 `fde06cd33`。动态注册采用“部署允许根 + 数据库内位置/子目录”，不把每个数据集路径写进环境变量。新增根内子目录无需重启；新增未挂载宿主机目录仍先由运维映射。当前没有经用户授权可用于验收的真实业务根，本轮用隔离临时目录验证，不改实际 Compose volumes。

最小替代是仅配置一个根并做字符串前缀判断；它不能满足动态注册、节点可达性和路径替换保护。本轮复用 E01 四表中的 StorageLocation/Binding、现有连接器授权与 Fernet，并使用 Linux openat2 的 beneath/no-symlinks 限制。默认允许根为空；不支持安全打开的节点明确不可用。

### 范围与契约

- 新配置 `STORAGE_FILESYSTEM_ROOTS` 是 root handle 到部署绝对路径的 JSON 映射，默认 `{}`。只在服务端配置保存绝对根；API 仅收 handle 和规范相对路径。受控根下拒绝符号链接、跨子挂载和非常规文件；只读代码不写探针或缓存。
- Alembic 0195 扩展 StorageLocation 的 connection_id/enabled，新增逐节点探测记录；老 S3/SFTP 连接保持原 ID、owner/global 权限和加密凭据 owner，只关联位置、不复制秘密。旧连接更新增加位置修订、失效旧探测；删除仅清理无引用关联，有内容/绑定引用则拒绝。
- 新 `/api/v1/storage-locations` 提供过滤列表、详情、超管注册 filesystem、带 expected_revision 的元数据/授权/启停更新、单层目录浏览、超管节点探测与探测查询；`/roots` 只列允许根句柄，`/bindings` 只读列出全部九用途，不在 GET 中 bootstrap。
- filesystem 位置仅 super_admin 管理；project_admin 仅按 global 或显式 user ID 授权浏览；员工的任务读取权限不授予目录浏览。连接关联位置继续委托旧 can_use_connection。输出不含绝对根、连接凭据或原始文件系统异常。
- 目录浏览默认 100、最多 200 项，一层最多检查 5000 个原始条目；超限明确失败。游标绑定位置、修订、路径、目录身份和条目摘要，目录变化要求重读。每页复核权限，临时 FD 始终关闭。
- 节点探测分别记录 api 与指定 Celery hostname。使用 Celery worker_direct 专属队列，执行端校验真实 hostname、权限和修订；离线/超时/晚到/旧修订不能成为有效证据。list/read/range 分开报告；只有显式 sample_file 的常规文件会被只读采样，空目录不会把 read 标成已验证。证据有效期 5 分钟。
- 根和位置子目录按每个节点的路径指纹、dev/inode 固定身份；不同节点不直接比较 dev/inode。首次 worker 探测信任部署者的同名根映射，后续变化拒绝静默重绑；它只证明节点能力，不证明多节点字节相同，该门由 E05 内容核验负责。
- 不增加 DatasetSource、原图 HTTP resolver、原地注册任务、用途切换或 UI 页面；后继 E04/E05/E03/E09 消费这批接口。现有复制导入保持可用，S3/SFTP 不能凭关联位置冒充原地读取已验收。

### 文件 owner 与验证

本切片超过 8 文件：模型/迁移（dataset_asset、models 注册、0195）、配置（config、.env.example）、filesystem 安全打开器、storage_location 服务/schema/router、storage_connection 服务和路由、Celery probe/配置、audit、API 快照/生成客户端、SDK 覆盖清单、专项测试、正式文档/ADR/CHANGELOG/DEV 及本系列导航。不新增依赖、密钥或外部服务。

```text
部署根映射 -> 安全打开器 <- API 注册/浏览 <- 当前位置授权
                 ^             |
                 |             v
指定 worker 专属队列 -> 节点探测 -> StorageLocation + 版本化节点证据
既有连接器 ----------> 关联位置（权限/凭据仍由原 owner 管理）
九用途固定绑定 ------> 只读展示；历史对象定位不切换
```

验证覆盖正常目录/中文空格、新增子目录、路径穿越/绝对路径/链接/替换/特殊文件、预算/游标/FD 释放；授权与撤销、配置 CAS、旧连接更新/删除、九用途缺失与共桶、GET 零写入；API 可达 worker 不可达、指定节点与过期/旧修订拒绝。使用隔离 test 数据库及临时根；运行旧连接器与 E01 回归、迁移往返、OpenAPI/SDK 契约、文档构建及 diff 检查。若启用本 checkout worker，核对任务注册并执行真实指定节点探测，结束清理所拥有的进程、资源和构建产物。

回退应用时保留新位置元数据并关闭新登记入口；旧复制导入与固定平台桶继续可用。仅无内容/绑定引用的关联位置能清理，不能为回退删除原目录。本轮不分配发布版本，不构建后端镜像。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                                      | 已有能力、缺口与复用约束                                                                                        |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| [storage_connection.py](../../apps/api/app/services/storage_connection.py) | 复用 can_use_connection/assert_connection_admin；当前 owner/global、create 写 project_id=None，未实现项目共享。 |
| [crypto.py](../../apps/api/app/core/crypto.py)                             | 复用 Fernet；访问凭据轮换与 CONNECTOR_ENCRYPTION_KEY 轮换分别处理，后者需要重加密和恢复材料。                   |
| [connector_guard.py](../../apps/api/app/services/connector_guard.py)       | 保留白名单与多入口复检；客户端实际连接仍会重新解析 DNS，不能将 DNS pinning 写成已有能力。                       |
| [sftp.py](../../apps/api/app/services/sources/sftp.py)                     | 现有测试主要验证列举；list/open 不能直接当作本地安全根打开器或持续原地读取证明。                                |

候选修改入口：[storage_connection.py](../../apps/api/app/db/models/storage_connection.py)、[storage_connection.py](../../apps/api/app/services/storage_connection.py)、[storage_connections.py](../../apps/api/app/api/v1/storage_connections.py)、[config.py](../../apps/api/app/config.py)、[docker-compose.yml](../../docker-compose.yml)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 问题与代码依据

当前外部 S3/SFTP 连接配置已提供个人和全局作用域、密钥加密等能力，适合复用；它主要服务拉取导入，尚不能表达所有原图长期存放位置。当前 `project_id` 字段不能直接解释为已经实现项目级连接共享。[连接模型](../../apps/api/app/db/models/storage_connection.py)

平台内部业务对象通过全局 `StorageService` 和多个桶分流。应让“数据在哪里”和“平台某类文件写到哪里”成为两个可明确维护的概念。[存储服务](../../apps/api/app/services/storage.py)

Docker 部署已支持选择 MinIO 宿主机数据目录；本 Epic 新增的是受控目录和多位置注册，不是修补“容器不能持久化”的问题。[部署配置](../../docker-compose.yml)

## 2. 目标与边界

目标是建立一份可信的存储位置目录：管理员配置物理连接与允许根，数据管理员只在有权限的位置中选取数据源。平台托管文件通过用途绑定写入指定位置，已有对象仍按其实际位置读取。

本期包括：

- 服务端只读目录、已有 S3/SFTP 连接配置归一化、默认托管 S3 位置。
- 能力声明与探测、配置版本、凭据轮换、停用影响分析。
- 平台九类现有存储用途的显式绑定和兼容初始化。
- 为 Agent 类型预留协议，不要求 M1 实现 Agent。

本期不包括：在页面任意浏览宿主机、远程执行挂载命令、自动开放所有目录、给数据管理员返回明文凭据。SFTP 配置可被统一管理，但持续预览和训练读取要通过 EPIC-05 能力测试后才开放，不能因“可导入”就宣称“可原地使用”。

## 3. 用户故事

| 编号    | 用户故事                                                                                | 交付切片              |
| ------- | --------------------------------------------------------------------------------------- | --------------------- |
| E02-S01 | 作为管理员，我能把已挂载目录登记成只读存储位置，限定根目录，供授权人员选取子目录。      | M1                    |
| E02-S02 | 作为数据管理员，我能看到可用位置及其读写、扫描、预览能力，不能选择不受支持的模式。      | M1                    |
| E02-S03 | 作为运维，我能配置内部访问端点与浏览器访问方式，验证上传和读取，而非只验证能否连接。    | M1                    |
| E02-S04 | 作为管理员，我能维护托管文件用途绑定，同时保留老对象的实际定位。                        | M1/M2                 |
| E02-S05 | 作为运维，我能轮换凭据和停用位置，并看到受影响的数据集、任务和版本。                    | M2，最小停用规则进 M1 |
| E02-S06 | 作为项目负责人，我能申请使用指定来源而不接触全局连接密钥；授权边界由 EPIC-11 统一定义。 | M2                    |

## 4. 建议模型

以下字段和接口均是设计草案，尚非现有实现。

| 实体              | 关键字段                                                    | 约束                                                                          |
| ----------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `StorageLocation` | `id/name/kind/config_revision/site_id/enabled_state/health` | 名称可改，ID 稳定；种类为 `filesystem/s3/sftp/agent` 等；启用状态与健康分开。 |
| 位置非密钥配置    | `root_handle/endpoint/bucket/base_prefix/access_mode`       | 对普通用户屏蔽绝对根、内部地址和不必要的拓扑信息。                            |
| 位置密钥引用      | `credential_ref/credential_revision`                        | 复用现有加密方案；页面只返回“已配置”和修改时间。                              |
| 能力快照          | `list/read/range/write/delete/presign/version_id/checksum`  | 区分声明、验证结果、验证时间和具体执行节点。                                  |
| `StorageBinding`  | `purpose/location_id/prefix/generation`                     | 决定新写入目标，不追溯改变旧 `AssetLocation` 或历史对象引用。                 |

九类初始用途：`annotations`、`datasets`、`bug-reports`、`media-cache`、`audit-archive`、`import`、`export`、`avatars`、`training`。首次迁移绑定到现有端点和现有桶，保持对象键不变。一个用途可以映射同一位置内独立桶或前缀；是否共享桶由生命周期和权限需要决定，不强行合并。

`StorageLocation` 是物理连接边界，`DatasetSource` 是某个数据集在该位置上的接入规则。一个位置可被多个数据集引用；一个数据集可在后续关联多个来源。

历史头像、附件、审计归档、训练包等没有 DatasetItem 的对象，也必须保存可解析的 `StoredObjectRef` 或固定 `binding_generation`。该逻辑引用可复用既有业务字段，不要求全部新增成数据集样本。某用途的历史对象尚不能固定定位时，禁止独立改其新写入绑定；可以在完成整用途迁移和对账后原子切换。

## 5. 功能与失败语义

### 5.1 受控目录

- 根目录由部署者预先配置或挂载，应用以只读权限访问；数据集创建者只选位置和相对路径。
- API/worker 哪些节点能访问该根必须显式探测。API 能看到而 worker 看不到时，位置不能标记为“可扫描”。
- 相对路径规范化后必须仍在允许根内；默认不跟随符号链接，尤其拒绝越界链接、`..` 跳转和设备文件。
- 路径校验与实际打开之间要防止路径替换；实现需采用受控根下的安全打开方式，不能只做一次字符串前缀判断。
- 使用固定 root handle 关联部署挂载。根目录改到另一处必须生成配置修订和重新验证，不得让现有引用悄悄指向同名新文件。

### 5.2 连接和能力探测

- `list` 成功不等于 `read` 成功；只读来源不执行写入探测。
- 托管位置的写入探测仅在专用测试前缀内创建并清理探针对象，记录残留失败。
- 浏览器预签名 URL 需验证签名、外部域名、代理路径、CORS 和 Range；内部 worker 读取单独验证。
- 连接超时、认证失败、根目录未挂载、无读取权限分别呈现；日志不包含密钥或完整签名 URL。
- 不支持能力用明确错误和前端禁用提示呈现，不能默默退回一次全量复制。

### 5.3 变更、轮换和停用

- 并发编辑采用配置版本条件更新；测试失败不覆盖最后有效配置。
- 轮换新凭据先验证，短暂兼容现有任务；失效后重试应重新取凭据，不在任务参数里长期存密钥。
- 停用位置先阻止新接入和新写入；既有读取如何继续需显式策略。强制停用必须给出影响清单。
- 有引用的位置不能物理删除配置；先归档。移除连接配置不删除源文件。

## 6. API 与界面契约草案

| 建议操作       | 输入/输出重点                          | 页面归属                         |
| -------------- | -------------------------------------- | -------------------------------- |
| 查询位置列表   | 权限过滤、能力、健康摘要；不得回吐凭据 | 存储页；数据集向导仅提供选择器   |
| 创建/修订位置  | 位置类型、受控根或连接配置、配置版本   | 存储页                           |
| 测试位置       | 按执行节点返回分能力探测结果和时间     | 存储页                           |
| 浏览来源子目录 | `location_id + relative_path + cursor` | 数据集接入向导；受服务端权限约束 |
| 变更用途绑定   | 旧/新绑定、影响摘要、有效版本          | 存储页管理功能                   |
| 查询引用影响   | 数据集/活动任务/冻结版本计数与授权深链 | 存储页详情                       |

界面不把“连接正常”描述为“所有数据可用”。数据集只显示来源位置名称、原图可用性、最近扫描时间和“查看存储位置”；详细健康、凭据、容量仅由存储页维护。

## 7. 验收标准

| 编号     | 可验证标准                                                                                                                 |
| -------- | -------------------------------------------------------------------------------------------------------------------------- |
| E02-AC01 | 登记只读根后，授权用户能浏览根内子目录并选取；提交根外路径、越界符号链接或无授权位置均被服务端拒绝。                       |
| E02-AC02 | API 可访问而扫描 worker 无法访问的目录，显示具体执行节点不可用，并阻止开始原地注册任务。                                   |
| E02-AC03 | 只读来源不能被选为托管写入目标；允许从它读取并复制到另一个有写入能力的目标。平台不向只读源目录写入探针、缩略图或临时文件。 |
| E02-AC04 | 九种现有用途完成初始化后，已有上传、标注附件、训练包、头像和媒体读取仍定位到原对象。                                       |
| E02-AC05 | 改变用途绑定只影响后续新对象；历史对象依据其位置记录读取，且数据集名称变更不改变路径。                                     |
| E02-AC06 | 根目录或端点修订必须通过验证和影响检查；同名新根不自动被视为已有 Asset 的等价副本。                                        |
| E02-AC07 | 凭据轮换后新请求成功，旧凭据不出现在响应、日志、任务详情；轮换失败仍可使用上一有效配置。                                   |
| E02-AC08 | 停用被引用位置时返回授权范围内的影响清单；既有引用保留且可标记不可用，外部原件不被删除。                                   |
| E02-AC09 | 同一配置被两人并发编辑时，后提交的旧版本请求被拒绝，不覆盖已经生效的新配置。                                               |
| E02-AC10 | 某用途新写入切到位置 B 后，位置 A 上的历史头像、附件和训练包仍由固定位置/绑定代次正确读取；历史定位未完成时切换被阻止。    |

## 8. 迁移与回滚

1. 以兼容配置加载现有全局存储端点，生成默认位置和用途绑定，不移动字节。
2. 将现有连接器逐条映射到新位置或关联记录，保留原 ID 映射、owner/global 语义和加密密钥。
3. 先双读配置/单一写入，新位置模块通过灰度开关对试点项目开放。
4. 新位置创建的数据由新读取链路处理；回退旧 UI 不得令此类数据失联，必要时关闭新接入并保留新读取兼容服务。
5. 回滚配置时恢复上一有效修订，不能把已产生的新对象重新解释到旧绑定；对象位置记录是读取依据。

## 9. 测试、风险与待决策

- 测试位置类型与能力组合、只读 mount、worker 挂载缺失、凭据轮换、目录边界与并发配置更新。
- 在真实部署拓扑验证内部/外部 URL；单测不能替代反向代理和浏览器链路验收。
- 确认 M1 的受控根由环境配置还是管理员 UI 登记：建议环境配置允许根，UI 只能从中登记，减少误选风险。
- 确认个人连接成为团队数据源时的授权迁移：建议由有管理权限者显式转换，不能因项目关联就自动分享连接。
- 确认 `StorageBinding` 首期是否只读展示并通过配置变更：建议读写切换跟随 EPIC-07 迁移控制流程。

## 10. 工程任务与完成定义

- [ ] 定义位置、用途绑定、配置版本和能力响应结构及数据库迁移。
- [ ] 为九类用途逐一盘点消费者、现有定位字段和历史对象引用，补齐非 DatasetItem 对象的固定定位与切换门槛。
- [ ] 兼容现有连接器和默认 S3 配置，编写幂等映射与回退操作说明。
- [ ] 实现受控目录解析、执行节点探测和有界目录浏览。
- [ ] 实现配置测试、轮换、归档与引用影响查询；接入 EPIC-11 审计。
- [ ] 对接数据集来源选择器、EPIC-09 位置详情、EPIC-05 读取路由。
- [ ] 通过上述验收，交付目录部署示例、权限说明、操作手册及兼容测试记录。

完成后，用户能选“数据在哪个位置”，应用能判断“这个位置能做什么”，原图无需先进入托管桶。

## 本项目推进切片与晋升条件

运维提供一个授权只读根及 API/扫描/读取节点挂载映射；后端固定部署允许根接口。根变更必须产生配置修订。S3/SFTP 持续引用分别通过能力验收后开放。

| 切片  | 独立结果           | 交付范围                                                                               | 验收 / 回退边界                                                            |
| ----- | ------------------ | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| E02-A | 兼容连接与只读位置 | 现有连接关联位置、受控 root handle、配置修订和节点探测；九用途先兼容初始化并只读展示。 | 只读源不写探针；现有 owner/global 不扩大；API 可达而 worker 不可达时阻断。 |
| E02-B | 轮换和受控用途变更 | 能力测试、停用影响、配置版本更新；历史定位固定后才开放新写入绑定切换。                 | 新对象写 B、旧对象读 A；失败轮换保留上一有效修订。                         |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_connections_api.py](../../apps/api/tests/test_storage_connections_api.py)、[test_connector_crypto_guard.py](../../apps/api/tests/test_connector_crypto_guard.py)、[test_storage_public_url.py](../../apps/api/tests/test_storage_public_url.py)、[StorageConnectionsPanel.test.tsx](../../apps/web/src/components/connections/StorageConnectionsPanel.test.tsx)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[connector-security.md](../../docs-site/dev/reference/connector-security.md)、[storage-connections.md](../../docs-site/user-guide/datasets/storage-connections.md)、[docker-compose.md](../../docs-site/ops/deploy/docker-compose.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](../README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

- Landed changes: E02-A dynamic filesystem registry, legacy connector association, ACL/CAS, read-only directory pagination, versioned per-node capability probes and read-only nine-purpose bindings. Included in the commit containing this outcome; no release milestone assigned.
- User documentation: `docs-site/user-guide/datasets/storage-connections.md`; current UI includes dynamic locations, source diagnostics and verified reference registration/reading delivered by successor slices.
- API and developer documentation: `docs-site/api/guides/storage-locations.md`, `docs-site/dev/concepts/storage-connections.md`, `DEV.md`, generated environment/API references and SDK coverage manifest.
- Deployment documentation: `docs-site/ops/deploy/docker-compose.md`; unified roots and explicit container/image distinction. No real business mount or backend image was changed.
- ADR: `docs/adr/archive/0092-controlled-storage-registry.md`. CHANGELOG: Unreleased entry added.
- Verification: 29 focused tests passed, including two real direct workers with readable/missing-root configurations; 106 compatibility checks passed and the shared-bucket case passed after correcting its fixture owner. SDK OpenAPI contract: 4 passed. Fresh/reversible/forward/restore migration validation passed through 0195 with no leftover owned databases. Final real offline-worker timeout and referenced-location rollback guard passed. Documentation build, ADR/frontmatter and internal-link checks passed. Base/production Compose parsed with empty/synthetic root maps.
- Remaining work: E02-B binding-switch safety and non-Item historical object identity, location retirement/impact and full write/browser capability qualification. E04 scanning, E05 strict reads, E03 registration and E09 primary UI are now delivered in their own plans; they are not remaining E02 prerequisites. This Epic stays active.

### E02-B1/B2 继续交付

- 本提交完成独立连接 revision/CAS（0210），候选配置与凭据在行锁外验证、保存前重新核对账号/管理权/原 Key/修订/白名单/引用，失败不覆盖旧值。名称编辑及相同配置不使位置证据失效；显式凭据轮换会失效旧探测。受引用位置仍禁止实际配置改向或删除。
- S3 只列至20项并关闭客户端；SFTP 按迭代器采样并在截止时关闭传输，严格位置复用部署固定公钥。请求等待预算与后台 SDK 收尾分别说明，不把列举通过当作完整读写或多节点资格。
- UI 固定打开时的修订；候选失败和409保留草稿及内联原因。SFTP 历史缺省 auth_type 的改名不发送配置；测试记录按修订隔离。嵌套编辑支持独立 Escape、焦点返回与窄屏滚动。
- 正式文档：README、CHANGELOG Unreleased、`docs-site/user-guide/datasets/storage-connections.md`、`docs-site/api/guides/storage-connections.md`、`docs-site/dev/concepts/storage-connections.md`；API 快照与本地生成类型同步。未分配发布版本。
- 本地验证：API 127项（含独立提交的并发/删除/撤权/白名单/引用竞争、密文保留、SDK错误脱敏、SFTP截止、0210往返及S3/SFTP/导入回归）；Web 11项、SDK合同4项，共142个不同用例。类型检查、完整Web lint/CSS检查通过；保留4条既有ESLint警告及2条既有样式建议。文档196份frontmatter与198页导航通过。最终提交运行完整hooks和diff检查；不代表远程CI。
- 实际运行：隔离3200/8200经真实MinIO验证错误凭据返回400且保留原revision=1/可用凭据，正确现有凭据显式保存使连接revision=2/位置revision=2，随后外部并发改名推进至4，旧表单返回409并保留草稿。最终GET确认名称为并发赢家、位置修订仍2、已有凭据测试成功；没有更改共享MinIO凭据或创建外部IAM账号，独立新凭据的原子替换由专项用例证明。
- 浏览器检查1440×1000/390×900明暗主题截图，无横向溢出；窄屏可滚至操作区。实际重新打开、取消、Escape及焦点返回已核验；等候表单真正卸载后再操作，避免将弹窗过渡帧当成页面故障。当前会话请求除预期409外没有其他失败，控制台无错误；首次会话的预期400已由保留草稿及原凭据仍可用证明。
- 人工3100/8100保留原业务数据并应用0210，原管理服务刷新当前checkout API/两个worker，运行时端口覆盖固定为原地址；beat保留。不重建镜像。隔离test/e2e资源和本轮日志/截图/密钥/缓存在交付前按归属清理。
- 整个E02仍未完成，不归档本计划；下一步按总队列收敛引用消费者和历史对象定位，再开放受控切换。

### E02-B3 训练版本固定存储归属

- 0220迁移 `2c8d6c3d7` 与领域实现 `e301ea3ff` 为训练版本及独立删除责任保存内部 storage_ref。新建和自动策略在首次存储I/O前固定；消费者 `91161af0d` 统一构建/重试、独立图片/掩码、包下载、训练/增强派发和模型试用的输入定位。后续全局training桶变化不重新解释已保存归属，实际端点变化和越界键在I/O前拒绝；权重、运行产物仍使用其原独立合同。
- 本交付增加项目负责人显式 storage-pin、严格旧副本验证和删除/恢复集成。GET不接管，迁移不猜测历史位置；候选只取已登记绑定。核验整个版本前缀及ready包、全部snapshot媒体/掩码（含未入包去重成员），缺少独立内容身份、遗漏分区/成员、伪造RLE消费证据、未知attempt或额外键均拒绝，不读取当前原件补证。版本执行锁、维护permit、进程容量和发布前权限/Key/上下文复核覆盖真实收尾，关闭未知仍保留责任。
- 删除先提交原归属的tombstone，再清理原桶/前缀；提交失败零I/O，存储或确认失败保留重试责任。未知旧版本意图不执行物理删除。离线恢复只有表owner配合显式事务标记可改bucket/identity；prefix、endpoint、来源/核验证据及原snapshot/manifest/ZIP不变。有pin时拒绝破坏性降级。
- 正式文档提交 `73b9e75cf` 更新README、CHANGELOG Unreleased、训练用户/API/架构、存储架构与恢复操作说明；本交付同步OpenAPI、生成路由索引及SDK维护风险分类。不分配发布版本，不改依赖/配置/Compose或镜像。
- 本地验证按classname/name去重：322个API/DB/纯用例、13个恢复工具用例、4个SDK合同用例，共339项通过；检测合同不适用的RLE receipt用例明确跳过1项，不计成功。覆盖数据库不可变/受限恢复、创建/自动策略、严格旧副本、Key撤销/轮换/过期/缩权、同键不同桶陷阱、删除提交及重试。Ruff检查/格式、OpenAPI/生成类型、Web类型检查、正式文档构建与正常提交hooks通过；这些为本地结果，不代表远程CI。
- 实际隔离18100/13100与两个新启动Worker/MinIO：检测、原生RLE各构建3图并下载核对包SHA；改变当前用途桶后仍读取原独立图片/掩码。复制出的NULL历史ready版本分别完整核验4/7对象并接管，重复请求保持原证据，删除只清理其专属前缀；另两条空前缀cancelled历史版本显式接管后重试至ready。真实浏览器打开两类版本/样本预览，图片加载及RLE画布正常，无控制台错误、失败资源请求或横向溢出；本轮未改UI样式，不将这些结果声称为完整视觉矩阵。
- 0220隔离恢复演练销毁合成源后恢复151表93行、九桶20对象，核对2个版本和1条tombstone的固定归属映射、冻结媒体、嵌套保护事实及原件历史，cleanup=verified。生产RPO/RTO与共享外部站点资格仍未验收。
- 人工3100/8100已应用0220并刷新所属两个Worker/beat，最终doctor健康。原1条旧ready版本经显式部署核验4对象后保存归属，不改snapshot/manifest/包字节；8Dataset、12Item、8Source、12Task、7Annotation、1TrainingVersion、24对象和12源文件与交付前完全一致。一次性test数据库/Redis/九桶已销毁并独立确认不存在；测试凭据、日志、截图/缓存和已等价集成的隔离树在交付前清理。
- 完整E02仍保持执行中：其他非Item用途的历史定位、受控新写入切换及完整浏览器写入能力尚未交付；不归档整项。E10真实框架加载集合回执、C2全部固定内容读取/独占与物理回收仍单独过门，不因本片已固定训练输入位置而视作完成。

### E02-B4 训练实验输出固定存储归属

- 0221迁移 `5a97d42b3` 和领域实现 `d921b82f7` 为TrainingRun保存内部固定归属，严格覆盖项目/实验整个前缀与全部attempt。API、自动策略、参数搜索三个新建入口显式分配；普通重试和续训不为旧NULL记录猜测位置。数据库限制正常改写，恢复仅允许owner配合事务标记映射bucket/identity，有运行归属或意图时拒绝破坏性降级。
- 消费者 `b58ad11e3`、`2d0087134` 统一上传、完成核验、原生配置、观测摘要、检查点/续训、脱敏日志、评估、模型注册/安装/下载和初始化。数据集输入仍独立解析；模型通过同项目来源Run/Attempt及已发布权重解析，不把物理位置复制进冻结config、初始化pin或provenance，不修改Executor/共享训练协议。未知位置的失败/取消准确回执仍可保留原GPU租约边界并收尾，成功回执不得绕过存储核验。
- 集成 `275a6c7dc` 增加显式Run storage-pin：只核验全部attempt终态、无保留GPU租约的旧实验，候选只取持久绑定；以独立DB发布/上传清单核对全前缀实际SHA、大小和attempt metadata。已发布对象缺失、成功执行遗漏、未知额外键及变化均拒绝；失败/取消的未发布对象允许缺失，存在则必须核验。派生日志须从独立核验源重建当前脱敏结果并匹配字节/修订。维持有界读取、共享进程容量、durable permit与真实关闭；发布前重新核对当前管理权/Key、所有attempt、候选及Gate修订。GET不接管，重复显式请求保持原事实。
- 删除同事务复制固定归属到独立tombstone，提交后按原桶清理；NULL旧实验返回409且不创建意图，不执行物理I/O。旧NULL run/dataset意图保持责任并拒绝物理清理。恢复映射同时覆盖Run与两类意图，仅排除许可的物理子字段，保留全部嵌套语义/证据。既有夹具 `80bbd0a56` 及本集成改为规范Run/Attempt键和独立输入/来源/输出桶；并发研究用例改用唯一资源并清理空闲租约。
- 正式文档 `20595c95e` 更新README、CHANGELOG Unreleased、训练API、实验/模型用户指南、训练架构和存储恢复说明。OpenAPI、生成类型/路由索引与SDK维护风险分类同步；未修改版本、依赖、共享配置、Compose或镜像。
- 本地按classname/name去重：455个API/DB/纯用例、15个恢复工具用例、4个SDK合同用例，共474项通过，最终无未解决失败。包含不可变/受限迁移、三入口、独立来源桶、NULL/越界拒绝、撤权/轮换/过期/缩权、对象与Gate变化、取消等待真实线程、未知关闭保留责任，以及删除提交/重试。Ruff/格式、生成合同、Web类型检查、完整文档构建、正常提交hooks和diff检查通过；这些是本地结果，不代表远程CI。
- 实际隔离18100/13100、两个新Worker及MinIO：输入与输出分桶，真实HTTP签名PUT六类产物、完成摘要核验、全部下载、脱敏日志、评估及模型注册/权重下载通过。私有核验进程将当前桶改为另一已拥有桶后，六个原产物的完整流核验、评估和模型URL仍指向保存桶；真实Celery处理Run tombstone只清理保存前缀，其他桶同名诱饵保留。复制的历史NULL实验在接管前拒绝下载/删除，显式核验5对象后幂等读取/清理通过。本片使用合成执行器回执与权重字节，未执行模型框架训练，不构成GPU或框架消费资格。
- 实际浏览器打开完成实验的产物页并点击下载，下载权重SHA与独立清单一致；截图、无横向溢出、页面错误与失败资源检查通过。未改UI样式，不声称完整明暗/窄屏视觉矩阵。离线演练在销毁合成源后恢复151表96行、九桶22对象，包含2个版本、1个Run及2条删除意图；运行产物SHA、固定归属/嵌套保护事实及既有原件历史保持，cleanup=verified，生产RPO/RTO仍未资格化。
- 人工3100/8100已应用0221并刷新所属两个Worker和beat，最终doctor健康；8Dataset、12Item、8Source、12Task、7Annotation、1TrainingVersion、0Run/Attempt/Model、24对象及12源文件与基线一致，原snapshot/manifest/包与storage_ref保持。一次性test数据库、Redis及九桶已销毁并独立确认不存在；浏览器已关闭，五个干净且patch等价的隔离树/分支已移除，任务凭据、日志、截图和测试缓存清理。
- 完整E02继续执行，不归档整项。下一片先复核训练预览、样本评估及主动学习等非Run输出的历史定位；完整用途切换继续关闭。E10实际框架消费回执、真实跨站点/迁移目标、生产独占与物理回收仍按各自合同推进。

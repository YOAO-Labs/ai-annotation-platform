# EPIC-05：统一文件访问、预览与缓存

> 状态：原始图片读取、预览、缓存与核心消费者范围已完成本地验收并归档；E05-D 补齐签名端点合同。真实目标部署资格另行核验，不分配发布版本。
> 转入日期：2026-10-22；本地核验基线：`e169d9ef53a9eda60abd4e67bf553ab4e6ab618e`。
> 实施前须通过 [backlog promotion gate](../backlog/README.md#promotion-gate)。[草案总览](../backlog/1792661016_dataset-storage-roadmap.md)、[架构契约](../backlog/1792661016_dataset-storage-architecture.md)和[交付验收](../backlog/1792661016_dataset-storage-delivery-acceptance.md)共同约束本系列。

| 项目          | 内容                                                     |
| ------------- | -------------------------------------------------------- |
| 状态          | 读取与缓存治理已交付，收口原始验收矩阵及端点兼容         |
| 日期          | 2026-10-22（Asia/Shanghai）                              |
| 优先级 / 阶段 | P0：M1 文件访问与预览；P1：M2 缓存治理；P2：M3 就近读取  |
| 负责角色      | 后端、媒体服务、前端、运维与测试                         |
| 最小依赖      | E01 Asset/位置契约；E02 可达性；E11 授权切片             |
| 下游          | E03 原地目录标注、E06 多站点、E08 样本浏览、E10 训练读取 |

## 最终实施定案：E05-D 签名端点兼容

基线 `fa6b945bb`。对应原始第 4 节和 AC08：现有 `prediction_inputs` 使用 SigV4，却在签名后改 Host；旧公共地址测试仅核对假签名字符串，真实 Worker 测试又令 ML Host 等于签名端点，未覆盖差异。修复只在签名前选择 browser/internal/ml 访问视角，保留不可变版本、精确有效期、原物理客户端与既有许可／关闭归属；不新建存储连接、不修改模型或 Tracker 流程。

相对 `/minio` 签内部端点，由代理恢复该 Host 并去前缀；绝对公共地址签公共 origin，带前缀时要求代理去前缀并保留公共 Host。ML 配置为空时使用内部端点，不能意外继承浏览器地址。真实 MinIO 验证不同 Host、中文与保留字符、版本绑定、上传、代理子路径，以及签后改 Host 的实际拒绝；独立 Worker／HTTP 消费补差异 Host，浏览器使用现有同源代理读取一次性对象。沿用人工3100/8100，空闲后刷新两个受影响 Worker，不重建镜像。

### 原始验收矩阵核对

本表复核已有实现与各 Outcome 记录，不把只读核对当作新跑测试。Tracker M2、完整视频跨来源分段缓存和模型质量专项不增加为本轮条件。

| 原始标准               | 已有证据／本次剩余                                                                                                               |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| AC01 统一读取          | `dataset_content`／`dataset_references` 覆盖受控目录、旧托管图片及逐适配器资格；S3/SFTP 的本地合同资格与真实远端部署资格分开记录 |
| AC02 预览与原图状态    | `test_directory_policies` 及 B2-B 实际来源离线浏览器记录，预览不能替代原图                                                       |
| AC03 变化拒绝          | `test_dataset_content` 的读取前／读取中变化及内容 receipt 发布检查                                                               |
| AC04 当前授权与根边界  | `reference_context`／`key_authority` 及 B2-A 当前 Task／账号／Key 撤权回归                                                       |
| AC05 内容独立缓存      | 不可变 Asset/SHA/generation；同路径覆盖仍读固定旧内容的完整缓存回归                                                              |
| AC06 清理与重建        | 缺失预览按需重建、精确派生键清理；B1/B2 退休不删源原件、唯一托管原图或训练副本                                                   |
| AC07 none 与临时回收   | none 不预约持久缓存，快照完成／取消／异常及迟到读取回收回归                                                                      |
| AC08 网络与签名        | E05-D 已通过不同 Host 的真实 MinIO、独立 Worker/HTTP、浏览器同源代理和容器网桥传输；绝对公共部署的浏览器 CORS 仍按目标部署核验                                                                     |
| AC09 Range／并发／配额 | 图片 Range 明确416；独立预约、去重、租约、固定TTL、LRU和删除失败持续计费已验证                                                   |
| AC10 恢复核验          | 同内容明确复验保留旧Task/标注；变化内容不得仅凭连通性续用旧Asset                                                                 |
| AC11 派生身份          | 预览有独立摘要和父Asset，严格原图快照及训练冻结不回退缩略图                                                                      |

## 已完成实施定案：E05-B 原图缓存与配额

基线 `f1b3bb89a`。完整 E05-B 仍按本计划第 6—10 节执行，分两段接入，不能把内部资源所有者当作离线标注已经开放。

**B1：持久归属与生命周期。** 迁移 `0213` 新增不随业务对象级联删除的完整缓存账本和有期限的读租约；数据库预留 `on_demand` 策略值，公开 schema/表单仍保持 `none/preview`。位置的原图缓存额度默认零，必须由后续管理入口显式配置；TTL 默认一天，可配置一分钟至三十天。预览配额不混入本段原图额度，也不将对象桶盘点当作物理磁盘容量。

上传前以独立事务预约实际完整字节，按位置序列化并发预约；`uploading/ready/deleting` 均计费。键含 Asset、SHA、格式和独占 generation。PUT 后重新读取实际对象、完整校验 SHA/图片，再复验来源/位置策略、修订、额度与目的绑定，单独发布 `role=original_cache` 的 AssetLocation；不覆盖任何既有托管原件或未知 locator。服务不接收外部路径，不删除源文件、`managed_original` 或训练产物。

捕获缓存也走严格全文件快照；读租约防止正常读取期间淘汰，失联租约到期后允许恢复。TTL 从预约时固定，访问只更新 LRU，不无限续期。清理先移除精确旧代 locator，再删除自有精确 key；删除失败继续占用额度并持久重试。失败或失联的 PUT 保留七天恢复窗口并重复删除可能迟到的对象，目的服务器指纹变化时拒绝向新服务器发删除请求。该窗口不承诺处理无限期迟到的远端写入。

范围：新增 `db/models/original_cache.py`、`services/original_cache.py`、`workers/original_cache.py`、迁移 `0213_original_cache_ownership.py` 和专项测试；修改模型注册与 Celery 路由/周期调度，同步存储架构、桶用途、运维说明和执行队列。使用 disposable test 模式验证独立连接、并发预约/去重、额度、对象损坏、策略变更、取消/关闭、删除失败、迟到 PUT、业务删除及旧代隔离；另验真实 MinIO 与实际 Worker。刷新人工 Worker/beat 前核对空闲与数据，沿用 3100/8100，不重建镜像。

**B2：统一读图、授权与产品入口，接续实施。** 拆开引用的逻辑授权与来源物理可达性，缓存命中仍在 I/O 前后核对 Task/Item/Asset/SHA、接受修订、来源/位置启用和当前 actor/Key；只有物理来源连通性可由完整同修订缓存替代。接入 `reference_snapshot`、缓存满时的可解释按需读取/LRU、位置配置/用量/清理与来源 effective policy，同时补齐 preview 的三策略兼容、状态 API、真实浏览器验收、API/SDK 生成物和全部调用方回归。缓存发布对业务锁使用有界等待，调用者持有同一来源锁时允许跳过可选保留，不得为了填充缓存提交调用方的业务事务。Range 仍须明确拒绝或实现正确响应。B1 不开放这一条产品读取路径，也不改变已有严格原图或训练门禁。

### B2-A：统一引用读取与当前授权

基线 `66ab63d59`。本段拆分逻辑身份与来源物理读取，将 B1 的完整缓存接入 `reference_snapshot`，公开按需配置仍不提前开放。`reference_context` 继续核对任务/来源管理授权、原始不可变引用、最新接受修订、Asset/SHA、来源/位置启用与配置；来源节点/连接配置只由 `reference_source_context` 在实际读源时解析。质量检查保持物理来源与 `capture_for_quality`，不把缓存命中视作来源恢复。

缓存命中在读前、读后重核逻辑身份与原 Key proof，并实际完整验证缓存字节；无需来源挂载，但必须能从当前 reader 访问受控缓存目标。缓存失效、损坏或不存在时回到原来源资格，绝不回退预览。旧 Asset 可读取其已验证旧副本，不接受同名新文件。最终 mutation/prediction/export 仍使用既有业务生命周期锁，不锁定无关 Probe。

首次严格源读取后尝试独立保留，不提交调用方事务；外部 Connection 事务继续保留严格源路径。额度满时最多淘汰八代可重建完整缓存，确认删除后才重新预约；单文件大于总额度直接跳过保留，不无效清空其他缓存。额度/上传/删除失败仍可返回经验证的请求快照；请求容量 429/超时不退休健康副本。HTTP Range 保持416。

修改 `services/{dataset_references,dataset_content,original_cache,reference_bindings}.py`；新增真实独立事务消费者专项，并抽取现有测试世界构建 helper 供 SAVEPOINT 与 committed 两种隔离方式使用。验证缓存离线、旧内容、当前 Task/账号/成员/Key、策略收回、并发容量、LRU/删除失败、调用方未提交状态、质量读源、训练/推理捕获、实际 ASGI 标注与真实 MinIO；回归引用预测/修复/原生结果、导出/训练、S3/SFTP、质量和明确同内容接纳。更新正式内容 API/架构与调用方说明，刷新受影响 Worker，保留人工3100/8100，不构建镜像。

后续 B2-B 继续位置配置/用量/受控清理、来源 effective policy、预览三策略兼容、缓存与来源状态分离、浏览器验收和 API/SDK 生成物；实际独立 worker/HTTP 的缓存预测投递仍需资格验收。此处不缩减完整 E05-B 的验收范围。

### B2-B：缓存配置、用量与状态入口

基线 `8408dd61e`。公开三策略按 `none < preview < on_demand` 取来源请求和位置上限的较小值；按需原图包含预览能力，登记预估、预览生成/读取与管理诊断统一遵循该语义。只有位置管理员配置原图额度/TTL，默认零额度；按需位置必须显式正额度，TTL按秒保留一分钟至三十天的精度，来源管理者不能提高位置预算。目录创建及所有受控引用位置的居中详情共用缓存表单，远端凭据保持原owner。

用量仅统计原图账本，分别显示上传中、已登记及待删除的完整字节，不当作桶或磁盘容量。清理请求携带位置预期修订，仅退休有归属的可重建完整缓存，跳过活跃读租约，保留上传与失败回收责任；实际对象删除由现有周期owner执行，删除前继续计费。样本保留来源历史观测，另列当前完整缓存记录/到期时间；登记记录不是实时可读证明，读取仍复核实际字节和权限。来源未见/已改变且有合格完整副本时可尝试核验读取，不报告来源恢复。

同步API/OpenAPI、前端生成类型与SDK覆盖合同，补相应用户/接口/架构文档和CHANGELOG。验证策略矩阵、配置权限/修订/严格整数边界、预览兼容、计费与租约清理、状态观测及真实独立Worker/HTTP/MinIO缓存预测；浏览器检查明暗主题、桌面/窄屏、表单/清理交互、错误与溢出。沿用人工3100/8100，刷新受影响worker，不重建镜像；隔离测试资源与任务产物验收后清理。

## 已完成实施定案：E05-C2 预测输入与结果资格

后台引用原生资格实施（基线 `ee7730c0f`）：本段完成受控实例分割整图根阶段 → 严格引用原图/同快照 ROI → 专属逐结果 receipt → 原子候选写入、两个失败重试及当前候选采纳。普通 `_Receipt`、同步交互和 mask repair 的发行者与范围继续隔离；原生子阶段仍显式拒绝。首个对象 PUT 前整体核对最终 portable RLE、任务尺寸/native gate、冻结根模型/实际实例/运行时、项目绑定类别与逐 shape lineage，不在最终阶段重跑类别 remap。

生成资格由活输入 owner、session/非嵌套事务、Task/内容、actor/Key 和精确写入 payload 绑定；服务层独占 reserved metadata 字段并存生成时的候选/稳定元信息摘要与自包含执行事实。采纳重验实际对象、当前原图与权限，不依赖已经过期的 Job/Snapshot 或仍在线的生成模型；普通几何和旧无证据原生候选不因此获得资格。验证包含全部入口、篡改/关闭/撤权/取消及多结果回滚，真实 HTTP/MinIO 与一次性 Worker；保留人工 3100/8100，不构建镜像，清理隔离资源后提交。

后台原生候选读取与画布接入（基线 `357c856d4`）：新增按 Task/Prediction/原始 shape_index 定位的图片 RLE 静态读取，复用任务可见性、annotations:read、全局 read gate 和静态内容 ETag/错误合同。权限、归属、驳回/替代和尺寸检查先于 304；普通已采纳预测保留原像素读取，交互采纳审计记录不当作候选。来源离线仍可读平台既存 RLE；不借此开放引用后台原生推理或放松采纳原图核验。

前端从已过滤的 AI 候选派生 descriptor，与 Annotation 共用任务级 Worker、缓存与资源预算，保留 predictionId/shapeIndex 的稳定身份；加入像素层、孔洞/分离部分精确命中、状态/重试、选中聚焦和来源/置信度过滤。切题、分页、驳回、采纳和最终对比模式不得复活旧候选或重复加载为人工对象。完成实际浏览器明暗/桌面/窄屏验证并清理任务自有资源；SDK 仅标记 workbench-internal exclusion，同步 OpenAPI、生成类型、路由索引与用户/API 文档。保留人工 3100/8100，不重建镜像。后台专用原图/模型 payload 资格、两个重试入口仍作为下一段必要工作，完整 E05 不归档。

后台原生掩码继续实施（基线 `f2f38c69f`）：先修复共用执行与持久化前置条件，再接引用结果资格及画布候选。受控分割仍按现有 HTTP 合同只输出整图根阶段原生 RLE；平台从经摘要核验的 RLE 计算紧像素外接框，供后续属性 crop 或全图 bbox prompt 使用，原掩码的洞、分离部分和内容身份保持不变。类别过滤、非零根编号、直接父身份、兄弟缓存和深层属性链须一致；drop_box 后同步裁剪 portable 对象集合。尚无像素回映合同的下游原生输出须记录明确阶段失败，不得静默丢形状或丢对象。

所有带 portable Mask 的预测在任何对象 PUT 前检查当前 native gate、任务尺寸、全部对象摘要/非空及封装预算；沿用 Task → 排序内容锁和共享对象 GC，不按失败请求删除共享 SHA 对象。本前置增量不放开普通 reference receipt，不宣称引用后台原生候选已可采纳或已在画布可见。后续仍需后台专用 payload receipt、严格原图结果资格、图片候选内容读取/像素层、两个重试入口及真实浏览器验收。测试使用 disposable test 模式，刷新当前工作树 Worker 并保留 3100/8100、beat；不重建镜像。

本增量验收：383 个不同后端用例通过，按各模块最新完整结果去重；包括非零根、深层属性/中间几何、逐包对象归属、共享 SHA 与删框、所有 PUT 前拒绝、opaque 模型解析及分割子阶段拒绝、检测子阶段缺省 crop、混合工具采纳和原引用门禁。真实 MinIO 的普通及 gzip RLE 完成物化、读回与 region 采纳，测试对象已回收。管线来源明确的混合 shape 按自身工具和阶段模型选择项目类别/alias 策略，不将后续检测框写入 region。下游 `brushlabels` 同样明确记录阶段失败。独立只读审查发现的缺省投递回归已修复并补端点到 worker mode 合同测试。

人工环境普通 Worker 与维护 Worker 已刷新，两个节点均空闲且注册 batch task；分别向 `ml.cpu` 和 `maintenance` 派发无业务对象的任务均为 SUCCESS，清除本次结果键，业务表计数及输入 lease 数保持不变。API8100/Web3100 健康，beat PID 保留。文档生成同步、196 页 frontmatter 与 198 页导航检查通过。本增量没有真实模型推理或浏览器像素验收，也没有新迁移、公开 schema、依赖或镜像构建；完整后台原生资格与 UI 继续实施，E05 不归档。

本轮基线 `54c2374bf`：在已完成的尝试隔离与原子提交上接入引用图片确定性修复、局部 SAM 重跑、恢复/严格回滚及候选采纳/驳回。计划固定原图与 SAM 模型声明/默认变体/参数，每个尝试保存当前 actor/Key；Worker 必须使用自身锚点严格捕获原图，不回退 API 节点或预览。Mutation/Prediction 分别使用精确 payload 的私有事务 receipt，SAM 实际路由 setup 复核模型，结果短事务锁住当前模型启用及路由归属。候选生成时保存结果/元信息摘要，采纳核对精确单元、源标注与当前原图；重复和已驳回候选拒绝，源离线仍可驳回。引用视频/Tracker、后台原生掩码及持久缓存继续独立推进，完整 E05 不归档。保留人工 3100/8100 和 beat，不重建镜像。

基线 `4af982c12`，工作树干净。重新核对批量/工作台预标、模型快照、snapshot retry、ROI、候选写入及采纳守卫。C1 完成不等于 ML 消费者获得引用资格：当前模型快照不冻结原图、ROI 另读原件、取消与结果提交没有共同锁边界，失败重试还有两个入口。沿用人工环境 3100/8100；不重建后端或 ML 镜像，不改共享配置，不依赖尚未指定的远端站点。

C2 最终合同：入队固定实际任务、任务版本、原发起 Key proof、模型快照及 Item/Asset/SHA/来源修订；真实 worker 节点捕获严格快照，经独占临时输入交付现有 HTTP 协议，根阶段、geometry 和 ROI 共用同份字节。最终事务与取消共锁，复验后以服务器专有 receipt 写候选；失败重试采用新授权但保留原模型/内容。采纳使用当次权限和严格原图证明，不把历史 Task.version 永久相等作为采纳条件，保留 draft 预标后分派/激活的合法流程。交互、predict-test、mask repair 按同一合同分别获得资格，不全局移除 `resolve_task_url` 的引用门禁。

当前增量定案：同步交互与 predict-test 使用独立请求 UUID 和清理账本，经 API 节点严格原图快照投递现有 HTTP 协议；原发起 Key、Task.version、绑定与当前权限在响应前复验。predict-test 保持项目管理者的可读测试语义；交互保持任务编辑、分派、锁和 AI 开关语义。原生掩码候选凭证与续点会话绑定原图 Item/Asset/SHA/尺寸，采纳重新严格读图并以独立私有事务 receipt 接入既有原子采纳 owner。批量 mask repair 继续单独实施其入队、取消、结果事务和确定性修复资格；本增量不把它视为已完成。

本轮先实施 C2-A 共用输入资源所有者，并实际接入现有多阶段预标及 snapshot retry 的 ROI 上传。上传前独立提交回收记录，使用服务生成的 attempt UUID 前缀；正常结束主动清理，删除失败持久重试，过期/失联尝试持续扫除可能迟到的写入。记录不随 job/task/project 删除级联消失，固定存储身份不匹配时拒绝访问新后端。原图快照转移所有权及同字节 image loader 也由该 owner 提供，后续 C2-B 接入引用、receipt、取消事务与重试/采纳；C2-A 不宣称已开放引用预标。

C2-B 首个前置增量处理既有结果事务缺口（基线 `5b012036f`）：图片覆盖删除后移，与全部候选、元信息和显式 partial 失败记录同事务；写入异常先完整回滚，再记录失败。成功数和费用在提交后累加，回滚后的下一题按固定 UUID 刷新状态，路由客户端按 registry ID 重新解析。取消 API、逐题写入、最终批次/作业完成及迟到失败回调共用 fresh AsyncJob 行锁；作业元信息合并保留已提交取消标记，broker revoke 在数据库提交后执行。视频逐帧仍由独立 owner 负责，保留原派发前清理合同。该增量不开放引用门禁，Task/内容/Key 冻结、同快照投递、专有 receipt 和重试/采纳仍是 C2-B 的后续必要部分。

当前继续主路径（基线 `167db664f`）：后台 bulk/工作台单题图片预标固定实际 Task 列表（包括空列表）、Task.version、Item/Asset/SHA/尺寸、来源修订和原发起 Key proof，并固定模型执行快照；pending 幂等重发复用首次证据。实际范围中已无引用时只固定范围，不额外要求读图 scope 或模型快照。抽取导出与预测共用的内容绑定和生命周期锁，业务授权仍分别校验。worker 使用真实节点严格读取，原图、geometry 与 ROI 共用一次捕获的字节，经独占临时输入交付旧 HTTP 协议；逐阶段复查资格，最终事务签发专有凭证才放行引用候选写入。凭证绑定当前 session、非嵌套事务、Task 与内容，不作为公开请求参数或持久授权；公开候选只保存必要身份与执行溯源，原 Key proof 不回传。引用禁止进入 frame 旧清理分支；资格/内容失败不适用模型软失败策略。两个重试入口与预测采纳继续在本主路径之后按同一合同接入，当前不绕过其门禁。

继续重试/采纳（基线 `53ea800c9`）：两个失败重试入口共用准备与设备路由；引用重试预建 durable job，固定新 actor/Key 和当前 Task 版本，同时保留原模型快照及逻辑 Item/Asset/SHA/尺寸。无原绑定的历史引用失败不猜测恢复。worker 在原件和模型 I/O 期间不持 FailedPrediction 行锁，最终复验 attempt/job/失败记录与当前资格，候选、partial 替代、失败删除和 retry 作业终态同事务。失败回滚后原子记录计数与作业失败，私有证明不出 DTO。采纳显式核验 URL Task=Prediction Task，按当前可编辑权限和严格原图核对候选内容身份，用 session/事务/Task/Prediction 绑定 receipt 放行；不要求历史推理 Task.version 永远相等。引用原生掩码和其它未获资格入口继续门禁。验证含两个 API 入口、真实 worker/HTTP/MinIO、当前授权与原内容变化、重复派发、最终写入失败整题回滚、draft 后分派/激活采纳及跨任务拒绝；沿用人工环境并清理独立测试资源。

范围超过 8 个文件：新增 `db/models/prediction_input.py`、迁移 `0212_prediction_input_leases.py`、`services/prediction_inputs.py`、`workers/prediction_input_cleanup.py` 及专项测试；修改模型注册、`StorageService` 可选有界客户端配置、`workers/{tasks,predictions_retry,roi,celery_app}.py` 和适用现有测试。同步 `docs-site/dev/concepts/prediction-pipeline.md`、存储架构/桶用途文档、运维清理说明、CHANGELOG、本计划与执行队列。没有新公开接口、依赖或环境变量。

验证使用已核验的 disposable test DB/Redis/九桶：覆盖上传前跨连接持久化、attempt 隔离、正常/异常/取消关闭、删除失败重试、过期恢复与迟到 PUT、存储身份变化、资源预算、同快照 ROI、不误删源原件和其他前缀，以及旧管线/模型快照/失败重试回归。真实 MinIO 对象与实际 worker 清理任务单独验收。迁移升级与有记录降级拒绝均验证；刷新本 checkout worker 与 beat 后检查任务注册/队列，保留人工数据。最终检查 diff、文档及提交 hooks，清理任务所属测试资源再提交。完整 C2/E05 继续执行中。

## 已完成实施定案：E05-C1 引用图片导出

基线 `883468e58`，工作树干净。依据当前 export worker、packaging、严格 reference reader、三个排队 API 和最终授权守卫重新审计。本切片已获“推进整个 Epic”授权；没有真实跨站点或迁移目标不阻塞本地实施。Orca 中继连接拒绝，串行修改当前 checkout；保留人工验收 3100/8100、dev 数据和 beat，不重建镜像，不改共享 `.env`、MinIO endpoint 或凭据。

本轮覆盖现有所有异步图片目标（COCO、YOLO det/obb/seg、AAP JSON、Label Studio Brush、Binary/Indexed PNG）的 reference/managed 混合包。排队封存实际任务范围与绑定、发起人及原 API Key 证明；引用原件用真实 worker 节点的 strict snapshot 核验后写入唯一 ZIP 路径。包内图像、标签和格式元数据共用路径映射；托管图保留签名回源方式。同步 VOC、视频/点云、ML 消费者继续维持现有资格门；不把 C1 声称为完整 E05。

涉及超过 8 个文件、超过 3 个组件：新增 `apps/api/app/services/exporting/reference_media.py`；修改 `services/dataset_references.py`、`exporting/{packaging,service,scope}.py`、`mask_formats/image_export.py`、`workers/export.py`、`api/v1/{projects,batches,data_manager,async_jobs}.py`、`schemas/async_job.py`、`apps/web/src/api/asyncJobs.ts`。测试落于 `test_export_reference_media.py`、`test_export_packaging.py`、`test_export_final_guard.py`、`test_export_cache_worker.py` 及现有导出合同/流式/掩码回归。正式文档更新 `docs-site/user-guide/reference/export-formats.md`、`docs-site/dev/concepts/storage-connections.md`、API 对应页、`CHANGELOG.md`、本计划与总执行计划；按实际 schema 变更同步 OpenAPI/SDK 合同，不增加未需要的迁移、服务或依赖。

```text
项目 / 批次 / Data Manager 导出请求
          |
当前导出权限 + 原 Key proof + 固定任务/内容绑定
          |
job 私有授权清单 -> worker 当前范围/撤权/取消复查
          |
strict reference_snapshot(worker:<hostname>) -> 临时快照 -> ZIP 原图
          |                                      |
同一映射生成标签/格式元数据 <-----------------------+
          |
独占导出对象 -> 最终短事务锁/复验 -> cache + ready 原子发布
          |
成功关闭快照；失败/取消清临时 ZIP 和未发布对象
```

真实 worker 验收修正：所有图片目标的实际 ZIP 已生成，但 `export_artifacts.format VARCHAR(20)` 无法保存完整多目标列表，暴露既有后台多格式字段预算缺口。本轮增加 `db/models/export_artifact.py`、`alembic/versions/0211_export_artifact_formats.py`，将格式摘要无损扩展为 Text；0211 升级保留旧值，降级遇到超过 20 字符的实际行明确拒绝，不能截断已有目标。test/e2e/dev 各自按 owner 应用迁移，不改共享业务库或重建镜像；补真实 DB 长目标回归与迁移检查。

兼容：默认 reference reader 授权保持任务可见性；导出仅通过内部回调复用项目 EXPORT_ANNOTATIONS 能力，显式选择仍逐项可见。不借用管理员身份。引用包禁止跨 job cache 命中；托管缓存保留。历史队列缺少原证明时不得读取引用。私有清单不出现在 AsyncJob DTO，幂等复投不替换原证明。最终检查覆盖账号、项目 owner、成员、原 API Key、源/位置、Task/Item/Asset 和取消；不持 SQL 行锁等待原件 I/O。

验证：先 `dev:worktree doctor --mode dev`，仅在已核验的 disposable test/e2e DB、九桶和 Redis 中运行。API 用 checkout `.venv` 执行新增内容/范围/权限/取消/资源清理回归，加现有各格式、缓存、最终守卫和流式回归；Web 取消按钮按实际控制验证，必要时运行 CSS lint/tsc。真实 worker 验收至少完成引用及混合 ZIP 的摘要、路径和失败不发布；确认实际 worker 已刷新且任务签名正确。`git diff --check`、相关 docs/OpenAPI 检查及提交 hooks 必须通过；清理 task-owned 资源并提交。

回退：关闭新图片引用导出入口，保留已有托管导出、原件和内容账本；不把 opaque reference key 交给旧平台桶签名器，不删除历史业务样本。E05-B 持久缓存、Range、配额治理及 E05-C2 预测投递单独实施；E01 历史导出存储 binding 仍是明确后续项。

## 本轮实施定案：E05-A 读取基座

基线 `013a0ac0d`，工作树干净；dev 已停止，使用本 checkout 的隔离 test 模式。不改共享环境或业务挂载，不构建镜像。0197 以独立字段和数据库不可变触发器保存内容回执。E03 尚未创建任何原地业务样本。

最小方案是直接按路径返回文件，但可变文件在核验后仍可能被覆盖。本轮复用 `openat2`、节点探测、现有项目/任务授权和 Python TemporaryFile：先完整读入有界临时快照、核对 SHA-256，再发送该快照；同一快照 owner 支持固定托管 S3 位置。无需新服务、凭据、依赖或配置项。

- 扫描条目 POST verify / GET content 仅限当前超管，要求 completed Run、匹配来源/位置修订、普通匹配文件和 API 当前 read 探测。内容核验结果独立存入 Entry.verified_content，不改原观测、不创建 Item/Asset/Task、不裁决 missing。只接受单帧 JPEG/PNG/WebP，限制像素和字节；历史摘要不改写。
- API 只读取本节点已验证挂载，使用其 anchor；worker 的 list 证据不代表 API 可读。首次核验要求与实际扫描同内核主机，并检查固定来源根/文件 identity；首次摘要只证明本次捕获字节。跨主机或未知首次证据、API 未挂载时明确不可用；已登记 SHA-256 可跨节点验证同内容，不经 Redis 传文件，也不自动代理到 worker。
- 严格托管读取使用 `/files/tasks/{task_id}/content?sha256=...`，当前任务可见性 + 当前 verified Asset + 固定 legacy StorageLocation / replica 的完整内容身份。只选择原件，不拿 thumbnail 回退。旧 file-url 保留签名合同，但补任务可见性；新严格入口不签可覆盖 key；content-identity 返回受权摘要和 unchecked 原图状态。
- 固定单文件64MiB、32M像素、读循环10秒，每进程最多4个快照（含响应期），每请求最多等15秒；不持久缓存。I/O 在数据库锁外；完成后重读权限、API Key 状态/身份/scope、配置和绑定再交付。超时/取消设置停止标记，实际读线程最终关闭资源并释放名额；阻塞内核 I/O 不能被线程 deadline 强制终止。响应正常/断连/异常均 finally 关闭快照。
- 内容 GET 必须带期望 SHA-256，不接受客户端提供任意 locator；返回 no-store、nosniff、摘要与精确长度。Range 本轮明确拒绝，交 E05-B。
- 本轮为独立管理员核验与严格任务读取 API。现有工作台 URL、标注提交内容上下文、原地样本登记及 UI 激活属于 E03 联合验收，尚未启用；不宣称完整 E05 或 E03 已交付。

范围超过8文件：filesystem_storage、快照 owner、读取 service、dataset_sources/files API及schema、Entry模型/0197迁移、专项与兼容测试、OpenAPI/SDK清单/API导航、正式API/用户/开发/运维文档、ADR0094、CHANGELOG及计划导航。具体新增 owner 为 `services/content_snapshot.py`、`services/dataset_content.py`，相关原有文件就地复用。

```text
超管扫描条目 / 当前任务访问者
             |
      当前授权 + 固定内容/位置
             |
   openat2目录 / 固定托管S3
             |
  有界 TemporaryFile -> SHA-256 / 图片核验
             |
   重读权限/修订 -> 快照响应 -> finally清理
```

验证：隔离 DB/临时目录/owned S3 对象；核验/重放/修改/替换/损坏/撤权、未挂载/过期/错误节点、越界/链接/FIFO、大小/像素/时间/并发预算、取消/响应异常资源回收、任务绑定和无权限请求无 I/O、真实 S3 覆盖拒绝。回归 E04、位置/旧存储/任务可见性；0197可逆与有数据降级拒绝、全迁移验证、SDK快照与文档检查。清理本轮资源并提交。回退关闭新增入口，保留历史摘要和账本；不删除源文件或把新相对路径交旧 S3 resolver。

## 本项目基线与改造落点

下列为固定提交的静态证据，尚非运行/浏览器验收结果。

| 当前 owner / 代码锚点                                            | 已有能力、缺口与复用约束                                                                                                                                   |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [storage.py](../../../apps/api/app/services/storage.py)             | 复用 generate_download_url/\_public_url/rewrite_host_for_ml_backend/resolve_task_url；现有签后重写需连同 nginx/Host 合同验证后替换，不能直接删除兼容分支。 |
| [dataset.py](../../../apps/api/app/services/dataset.py)             | list_items 固定 datasets 签名；media、导出、ML、训练也有全局桶假设，调用方清单不能只含列表。                                                               |
| [image_pyramid.py](../../../apps/api/app/services/image_pyramid.py) | 复用金字塔 generation 和派生 owner；videos/ 短期缓存与 thumbnails/playback 的持久引用分开，不能按桶统一淘汰。                                              |
| [nginx.conf](../../../infra/docker/nginx.conf)                      | /minio/ 代理路径和 Host 是部署合同，浏览器/API/worker/ML 分别验证。                                                                                        |

候选修改入口：[storage.py](../../../apps/api/app/services/storage.py)、[dataset.py](../../../apps/api/app/services/dataset.py)、[media.py](../../../apps/api/app/workers/media.py)、[image_pyramid.py](../../../apps/api/app/services/image_pyramid.py)、[training_dataset.py](../../../apps/api/app/workers/training_dataset.py)、[nginx.conf](../../../infra/docker/nginx.conf)。新增表、适配器、迁移、测试和准确文件范围在晋升时固定，不直接由逻辑实体生成数据库。

## 1. 要解决的问题

数据集原图可以不在统一对象桶，但浏览器、标注工具、导出 worker 和训练 worker 仍需要一致的访问方式。后端必须负责定位、授权、内容身份校验和访问失败解释，不能要求前端理解服务器磁盘路径。

现有 StorageService 已有签名 URL、内部/外部地址处理及任务 URL 解析。应把这些入口逐步迁移到统一解析契约，保留现有媒体派生资产生命周期约束。[代码依据：StorageService](../../../apps/api/app/services/storage.py)

## 2. 产品原则

- 用户访问的是一个确定的 Asset 内容修订，存储位置只负责提供该内容。
- 原图是否可读、预览是否可用、缓存是否存在分别显示。
- `reference` 不强制首次复制原图；校验读取可使用受限临时缓冲，预览/热点/训练副本另有明确策略。
- 缓存未命中不能把逻辑上存在的样本显示成已删除。
- 禁止把来源 URL、凭据、绝对路径直接发送给无权限客户端。
- 连接健康不等于具体文件可用，具体文件可用也不代表某个用户有访问权。

## 3. 范围与用户故事

M1 覆盖图片原图、缩略图、标注读图和受控文件响应；现有视频功能保持兼容。完整的视频跨来源分段缓存、转码调度和大范围跨站点预取可在后续阶段扩展。

| 编号    | 用户故事                                 | 结果                                 |
| ------- | ---------------------------------------- | ------------------------------------ |
| E05-S01 | 标注员打开目录中的图片时无需下载再上传   | 获得受鉴权的浏览器可访问地址         |
| E05-S02 | 用户遇到源离线但缩略图还在时看得懂状态   | 明确“仅预览可用”，不声称原图正常     |
| E05-S03 | 用户在同名文件被覆盖后不会拿旧标注标新图 | 访问校验内容身份，变化时阻止错误配对 |
| E05-S04 | 管理员独立控制预览和热点缓存             | 原图策略与缓存策略互不混淆           |
| E05-S05 | 训练或导出 worker 使用同样的内容身份     | 根据调用用途获得可达、校验的读取路径 |
| E05-S06 | 来源恢复后用户可以继续操作               | 状态重新核验，必要缓存自动重建       |

## 4. 统一访问决策

建议入口 `resolve_access(asset_id, purpose, actor, range?)`。purpose 至少区分 preview、annotation、download、export、training，授权与返回方式按用途决定。

1. 校验用户/服务身份对数据集、样本与操作的权限。
2. 查找该 Asset 的允许位置，区分原始引用、托管副本与缓存副本。
3. 排除已知缺失、内容改变、禁用或不适用于当前用途的位置。
4. 根据可达性和内容验证方式选择确定的一份内容。
5. 返回短期签名地址、受鉴权网关路径或服务内部读取句柄。
6. 记录结果、失败原因与实际字节，不将敏感 URL 写入普通日志。

### 各来源的默认访问方式

| 来源                     | 默认方式                          | 内容保证要求                                          |
| ------------------------ | --------------------------------- | ----------------------------------------------------- |
| 受控文件系统/NAS         | 受鉴权文件网关或读取 worker       | 对可变文件采取已校验快照/缓存；不能仅凭路径签名       |
| 有不可变对象版本的 S3    | 绑定具体版本的短期签名 URL 或代理 | 版本与 Asset 摘要对应，签名与 endpoint 兼容性测试通过 |
| 无版本保护的可变 S3 对象 | 校验后缓存/代理读取               | 不把可被覆盖的 key 当成不可变内容承诺                 |
| 既有平台托管对象         | 复用现有访问并逐步引入 Asset 引用 | 业务写入禁止覆盖既有内容键                            |
| 远端 Agent               | 经授权的站点读取通道              | E06 验证节点身份和内容，能力不足时明确不可用          |

后端不得通过替换已签名 URL 的 host 猜测可访问地址。内部 endpoint、外部 endpoint、反向代理路径和签名域名需作为部署契约测试；已工作场景保留兼容，未经验证不改变其行为。

## 5. 可变源的内容一致性

来源文件可能在 stat、读取和浏览器显示之间被覆盖。文件大小/时间检查和“打开同一个文件描述符”都不足以证明整段响应与已登记内容完全一致。

M1 严格方案：对需要与标注绑定的可变原图，读取到受限临时快照或已验证缓存，完成摘要核对后才向客户端提供该快照。临时文件按空间配额与超时清理；`cache_policy=none` 表示不持久保留缓存，并不禁止请求内必要的临时缓冲。

来源在读取中变化、摘要不符或读不完整时，不交付成功原图，也不沿用旧标注。返回 `SOURCE_CHANGED` 或对应校验错误，交 E04 重新核对。已验证缩略图可作为历史预览，必须明确不能替代原图完成精确标注。

若部署能够提供文件系统快照或可信不可变源，可采用更高效路径，但能力必须显式声明和验证。普通目录默认不享有这种假设。

## 6. 缓存策略

| 策略      | 行为                                            | 适用场景                     |
| --------- | ----------------------------------------------- | ---------------------------- |
| none      | 不持久保存预览/热点原图；允许请求期临时校验     | 严控副本、访问较少           |
| preview   | 持久保存版本化缩略图/预览，原图仍来自来源       | 默认图片浏览体验             |
| on_demand | 请求时保存已验证原图或热点块，受额度与 TTL 约束 | 慢速来源、重复标注与局部离线 |

缓存键至少含 Asset ID/内容摘要、派生规格与处理器版本。不得只按原路径命名，否则同名覆盖会命中旧缓存。缓存元数据记录产生时间、验证状态、大小、最近使用和所属授权范围。

完整原图缓存属于 AssetLocation，可在摘要一致时替代来源原图；缩略图/转码属于 DerivedArtifact，拥有自己的摘要并关联原 Asset。`purpose=preview` 可读取派生物，`annotation/training/download-original` 必须选择完整内容，不能仅凭关联同一 Asset 就互换。

位置级默认策略、空间配额、允许区域和清理由存储页管理；DatasetSource 只选择这些约束内的本来源策略。后端计算并返回 effective policy；数据集管理员不能通过来源覆盖扩大配额或绕过禁止缓存规则。

缓存淘汰只移除可重建副本，不删除源原件；引用中的唯一托管原图和冻结训练数据不能被当作普通缓存清理。已有长期引用媒体与临时媒体的生命周期区别继续保留。

并发请求同一派生物使用去重锁或幂等任务，避免大量重复生成。缓存满时降级为可解释的按需读取/排队，而不是填满系统盘。

## 7. 状态与 API 草案

建议按响应分别返回：`original_availability`、`preview_availability`、`verification_state`、`last_checked_at`、`action_hint`。不要以单个“在线”字段覆盖所有状态。

| 建议操作                                       | 输出                                         |
| ---------------------------------------------- | -------------------------------------------- |
| `POST /assets/{id}/access`                     | 当前用户用途下的短期访问方案，不返回长期凭据 |
| `GET /assets/{id}/availability`                | 原图、预览与校验状态，以及观测时间           |
| `POST /assets/{id}/verify`                     | 高优先级校验任务，复用 AsyncJob              |
| `POST /assets/{id}/preview-jobs`               | 预览生成/重建任务                            |
| `POST /storage/locations/{id}/cache-evictions` | 预览影响后清理可重建缓存，权限归 E11         |

上述接口仅为草案。下载与读取代理需支持取消、流量上限、Range 的正确响应或明确不支持；不支持时不能伪造 206。

## 8. UI 边界与授权

数据集页面的样本检查器显示“原图可用 / 仅预览可用 / 暂不可用”和具体操作，例如重试、查看来源、查看变化。普通标注流程不展示缓存服务器配置。

存储页负责位置级缓存用量、默认策略、配额/上限、清理和位置健康。数据集来源页可选择约束范围内的本来源策略，并显示生效结果、派生用量摘要及跳转；不复制位置级配置表单。

鉴权通过后生成的签名 URL 在有效期内可能仍然可访问，不能承诺撤权后所有已签链接立即失效。敏感操作使用短有效期或每次鉴权代理；撤权立即阻止新签发，并记录适用的失效窗口。

## 9. 验收标准

| 编号     | 可执行验收                                                                                                                                                                              |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| E05-AC01 | M1 中文件系统与旧托管 S3 图片通过统一入口完成标注读图；M2 对每个新增持续引用适配器单独通过同一契约后再开放。客户端不拼绝对路径或桶名，不把既有 SFTP 导入当作已验证的持续读取            |
| E05-AC02 | 原图离线而预览存在时显示“仅预览可用”，精确标注/训练不能假装原图已读取                                                                                                                   |
| E05-AC03 | 来源文件在校验前或校验过程中被覆盖，服务拒绝返回与预期 Asset 不一致的成功响应，旧标注不配新图                                                                                           |
| E05-AC04 | 未授权用户不能通过猜 Asset ID、调用需鉴权的他人访问入口或根外路径获得文件；撤权后不再签发新链接。直接预签名 URL 属于短期持有者凭证，其已签发窗口按 E11 验证，不宣称转交后仍能逐用户阻断 |
| E05-AC05 | 同一文件路径的新内容生成独立缓存键，不能命中旧内容预览；旧版本仍可读取自己的有效副本                                                                                                    |
| E05-AC06 | 清理缓存后可重建预览；源原件、唯一托管原图和冻结训练副本保持不变                                                                                                                        |
| E05-AC07 | 配置 cache_policy=none 时不产生持久热点缓存；临时校验缓冲在正常完成、取消和异常后被回收                                                                                                 |
| E05-AC08 | 从浏览器、API、worker、ML 服务各自网络环境验证访问 URL；无内部地址泄露或签名域名不匹配                                                                                                  |
| E05-AC09 | 正确处理 Range、取消、并发生成和缓存配额；无 Range 能力时按约定降级或报错                                                                                                               |
| E05-AC10 | 来源恢复后重新核验状态，只有摘要一致的副本可重新服务旧 Asset，不把“连通”直接等价为内容恢复                                                                                              |
| E05-AC11 | 缩略图的摘要与原图不同且仅作为派生记录存在；原图位置全部不可读时，读取解析器不会把缩略图当成 annotation/training 的完整原图返回                                                         |

## 10. 测试、迁移与回滚

测试覆盖中文文件名、路径编码、代理子路径、签名过期、跨域配置、读取中断、内容变化、缓存损坏、磁盘满和并发请求。视频相关旧链路至少完成回归，不因图片目录接入而改变长期派生资产的过期策略。

调用方按预览、标注、导出、训练顺序迁移，并保留旧托管访问兼容分支。回滚时可以关闭新访问方案，但不能让外部路径误入旧桶拼接逻辑。缓存格式含版本，可放弃新缓存并重建；不能把缓存回滚当作原图恢复措施。

## 11. 风险与待决策

- 严格校验的首次读取会增加延迟和临时空间，需要代表性图片尺寸基准后设定配额。
- 默认 preview 策略能改善浏览，但离线标注是否允许使用热点原图需明确权限与可用性规则。
- 边缘缓存与本地训练预取属于 E06/E10；不在 M1 自动复制全部原图。
- 对超大文件是全文件校验后提供 Range，还是要求可验证的不可变版本，需按媒体类型分别确定。

## 12. 工程任务与完成定义

- [x] 实现 access resolver、用途授权和统一错误映射。
- [x] 实现文件系统网关及可变源校验快照路径。
- [x] 实现预览派生、版本化缓存键和并发去重。
- [x] 补齐缓存额度、清理、临时文件回收与状态 API。
- [x] 替换前端直拼 URL 与业务层全局桶假设。
- [x] 验证浏览器/worker/ML 的网络和签名兼容矩阵。
- [x] 通过源变化、撤权、缓存损坏与原件不误删验收。

完成定义：所有核心消费者以确定内容身份读取文件，用户能区分“可看预览”和“原图可用”，原地目录无需首次复制就能进入标注主流程。

## 本项目推进切片与晋升条件

运维固定临时磁盘、并发和代理拓扑；测试固定四类消费者的实际网络。内容修订必须贯穿读取和标注提交的服务端校验，不能只禁前端按钮。

| 切片  | 独立结果         | 交付范围                                                                       | 验收 / 回退边界                                                         |
| ----- | ---------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| E05-A | 同内容读取和状态 | 旧 S3 与只读目录统一授权解析；可变源先限额临时快照并核验摘要，再交付标注原图。 | 读取中变化不能返回成功错图；原图/预览分离；成功/取消/异常都清临时缓冲。 |
| E05-B | 配额与原图缓存   | 版本化缓存、去重派生、配额清理、Range/取消；用途与持久引用分别保留。           | 离线只选同修订完整副本；清理不碰唯一原件/冻结内容。                     |

前置合同不等于等待前置 Epic 全量完成。每个切片合并后系统仍须可用；含新来源的回退保留兼容读取和授权，不能把其相对路径交给旧 S3 拼接逻辑。预计多数切片涉及超过 8 个文件，实施时明确大范围边界与准确清单。

已有回归入口：[test_storage_public_url.py](../../../apps/api/tests/test_storage_public_url.py)、[test_storage_event_loop.py](../../../apps/api/tests/test_storage_event_loop.py)、[test_image_pyramid.py](../../../apps/api/tests/test_image_pyramid.py)、[test_training_dataset_bridge.py](../../../apps/api/tests/test_training_dataset_bridge.py)、[test_media_frame_seeking.py](../../../apps/api/tests/test_media_frame_seeking.py)。这些仅为旧行为回归起点，新合同/状态/故障场景须补有意义的用例；不能以旧测试通过替代本 Epic 验收。分层命令、真实浏览器矩阵和产物清理见[交付验收](../backlog/1792661016_dataset-storage-delivery-acceptance.md#本项目验证与文档交付)。

实现时同步：[storage-buckets.md](../../../docs-site/dev/reference/storage-buckets.md)、[index.md](../../../docs-site/user-guide/datasets/index.md)、[network-security.md](../../../docs-site/ops/deploy/network-security.md)，以及适用 API 指南/快照/前端类型、`CHANGELOG.md` Unreleased。新增配置同步 `.env.example`、`pnpm docs:gen-env-vars`、`DEV.md`；涉及 SDK 合同按对应 skill 核验。正式 ADR 在实施时分配可用编号。

完成实施后按[计划规范](../README.md#required-completion-steps)追加 `## Outcome`，列实际提交、正式文档、验证证据、清理与剩余限制；草案清单不等于已实现。

## Outcome

- Landed changes: E05-A read foundation, included in the commit containing this outcome. Scan entries gain immutable content receipts through migration 0197; inventory observations remain separate. Administrator verification and strict managed-image task reads share bounded temporary snapshots, SHA-256 and image validation, current authorization and post-I/O rechecks. Existing task file URLs now enforce canonical task visibility. No release milestone assigned.
- Filesystem evidence: scan claim records the actual executing kernel host; initial verification requires that host and the pinned source/file identities. API reads require their own current node read proof and anchor. Established digests can be checked on another qualified API node; old or unknown initial host evidence requires a fresh probe and scan. Source-directory replacement, mutable S3 overwrite and API Key revocation/rotation/expiry/scope loss fail closed.
- Resource bounds: 64 MiB and 32 million pixels per image; four admitted snapshots per API process, retained through response completion. Reads have a ten-second cooperative deadline and fifteen-second request wait; responses have a thirty-second deadline. Cancellation, late completion and response failures close owned files and release admission. A blocked kernel read cannot be forcibly interrupted; its admission remains held until the reader exits.
- User documentation: `docs-site/user-guide/datasets/index.md`. API documentation: `docs-site/api/guides/verified-content.md`, `docs-site/api/guides/dataset-sources.md`, OpenAPI snapshot, route inventory and SDK coverage manifest. No SDK method or version change.
- Developer documentation: `docs-site/dev/concepts/storage-connections.md`, `docs-site/dev/concepts/audit-and-notifications.md`, `DEV.md` and `README.md`. Operations documentation: `docs-site/ops/deploy/docker-compose.md`. ADR: `docs/adr/archive/0094-verified-request-content-snapshots.md`. CHANGELOG: Unreleased entry added.
- Verification: final focused snapshot, scan/API, managed S3, real direct-worker and populated-migration suite: 36 passed. Existing E04, filesystem, storage-location, asset and task-visibility compatibility cases: 71 passed. SDK OpenAPI contracts: 4 passed. Python lint/format, OpenAPI export/code generation, documentation build, ADR/frontmatter/navigation and rendered-document link checks passed. No live browser or remote CI acceptance is claimed.
- Migration: full fresh, reversible prefix/suffix, legacy-forward and dump/restore validation passed through 0197. An empty receipt column permits downgrade; populated receipts reject it. Validation databases were removed with no owned leftovers.
- Cleanup: task-owned worker, S3 objects, temporary fixtures and logs were removed; isolated test database, Redis and buckets were destroyed, and generated documentation/test outputs were removed. No backend image build, business mount change or shared development runtime refresh.
- Remaining work: E03-A with E08-A/E09-A must register in-place business samples, expose the user flow, integrate authenticated browser image reads and bind annotation submissions to exact content. Workbench, ML, export and training consumers have not migrated. E05-B persistent cache, preview derivation, quota management and Range remain pending. The API foundation does not complete E03 or the full E05; this plan remains active because these contracts still constrain later slices.

### E05-C1 Outcome

- 落地范围：本节与实现同次提交。全部现有异步图片目标支持托管/引用混合；私有原授权与固定 Task/Item/Asset/SHA 贯穿读取、打包和最终发布。引用原图及 YOLO 配套副本在 ZIP 内核验，COCO/AAP/掩码路径一致；原 Key、任务版本、来源/位置/连接修订变化时拒绝发布。完整 E05 仍未完成，不归档。
- 取消与发布：新增 export 取消入口及实际 UI 按钮。上传对象到 commit 前均由本任务负责清理；取消不会发 export.failed，cache、job result、ready 通知在最终授权后同事务提交。显式 reviewer 选择最终锁定批次和任务，独立连接证明并发归档被 share lock 阻止，归档完成后拒绝发布。原件 I/O 不持这些最终行锁。
- 格式与容量：快照、manifest 和 ZIP 关闭路径及 entry/文件数/临时空间配额通过回归；大范围选择、标注、预测和样本查询使用有界参数，33,001 个真实 Task 通过。迁移 0211 无损扩展完整目标列表，旧值保留，含长列表时拒绝降级；实际短值降级、再升级通过。
- 本地测试：最终 API 127 项、Web 33 项，共 160 个不同用例通过；涵盖所有图片目标、S3/SFTP 合同 fixture、缺失 worker、内容变化、原 Key 轮换/scope、来源停用、任务版本、上传后撤权/取消/异常、配额关闭和纯托管批次旁有引用。Ruff、Web lint/CSS token、TypeScript、OpenAPI check、196 页 frontmatter 和 198 页导航检查通过。沿用既有 4 条 ESLint warning 与 2 处语义层级建议，无新增 lint 错误；不声称远程 CI。
- 真实验收：独立 e2e 3200/8200 的真实 API → Celery → MinIO 完成全部 8 种图片目标的混合 ZIP；1 份引用原图、1 份托管图、4 个包内引用副本，SHA、YOLO 路径及 COCO 128×96 尺寸核对通过。原始多目标字段失败的已上传对象确已移除，未留可复用缓存。实际 fetch_images.py 使用显式平台 base-url 成功校验引用并回源托管图片；明暗 1440×1000 / 390×900 截图已逐张检查、取消按钮可见，无横向溢出、浏览器异常或失败请求；窄屏实际点击取消后 API 为 cancelled 且无下载 URL。
- 运行环境：人工环境 3100/8100 应用 0211 并刷新本 checkout 的 API、普通/维护 worker，doctor healthy；beat 保留。没有重建后端/ML 镜像，没有改共享 .env、MinIO endpoint、凭据或原件。
- 清理：本轮独立 test/e2e 数据库、Redis 和各自九个桶已由环境所有者销毁；专用浏览器已关闭，临时验收脚本、截图、ZIP、日志与本 checkout 的测试缓存已清理。人工环境及业务数据保留。
- 正式文档：`README.md`、`docs-site/api/guides/{export,verified-content}.md`、`docs-site/dev/concepts/storage-connections.md`、`docs-site/user-guide/reference/export-formats.md`、`docs-site/user-guide/datasets/{index,import-images,prediction-import-export}.md` 与 CHANGELOG Unreleased。公共 schema 未变，OpenAPI 快照核对一致，无 SDK 方法或版本变更。
- 后续边界：同步 VOC、预测/采纳仍保留引用门禁；E05-C2 需独立实现投递与结果资格，E05-B 持久缓存/Range/全局配额、E01 历史 artifact binding、硬崩溃与存储删除失败的持久回收继续后续治理。导出桶七天生命周期兜底清理遗留桶对象；硬崩溃后的本地 ZIP 恢复清理仍需后续实现。已签发链接和已下载包不承诺实时撤回。S3/SFTP 为合同测试，不冒充真实远端网络资格或生产性能基准。

### E05-C2-A Outcome

- 落地范围：本节与实现同次提交。`prediction_inputs.py` 提供上传前独立提交的 attempt 回收账本、独占前缀、固定 bucket/endpoint 身份、预算、原图快照接管及同字节 ROI loader；实际接入批量/工作台多阶段预标及模型快照重试。移除旧无账本 crop uploader，保留纯函数注入/内联模式；Task、原件授权和候选资格仍归原 owner。
- 生命周期：正常完成、部分失败、普通异常及取消关闭输入和图像并主动清理；删除失败保持持久重试。过期失联或不确定 PUT 的记录继续保留到活动期限后七天，反复扫除迟到写入。专用 SDK 只尝试一次；连续取消仍等待已启动 I/O 完成才关文件。旧 ROI 前缀采用原生命周期，不从未登记对象猜测删除范围。
- 数据与迁移：0212 创建独立且无业务级联外键的 lease 表。非空降级拒绝；检查前取得表排他锁，独立连接证明新 lease 无法穿过空表检查而后丢失。空表降级、再升级通过。存储身份不符只记录错误，不改账本或追随新后端。
- 本地验证：221 个不同 API 案例通过，涵盖输入恢复、非零 root/多级 ROI、旧管线校验、项目/工作台范围、模型快照/失败重试、存储 URL/事件循环；最终迁移并发保护的专项复跑通过，不重复计数。实际 localhost HTTP 503 测试证明 SDK 只发一次 PUT，剩余 1/17/30 秒签名不对齐延长。Ruff、OpenAPI 快照核对、196 页 frontmatter、198 页导航及 diff 检查通过；未声明远程 CI 或新增 UI 浏览器验收。
- 真实验收：独立 test DB、Redis、MinIO 九桶中的真实 Celery `cleanup` worker 注册并消费新任务；原图快照上传后从 S3 取回并核对 SHA。过期尝试首次回收保留账本，迟到 PUT 再次回收并按到期窗口移除账本；活动 attempt、邻近前缀与独立源对象均保留，正常 close 移除对象和账本。最初验收脚本将 Celery 节点直达队列误判为业务订阅，已修正脚本；应用路由未因此改变。此处没有进行真实 ML HTTP 网络/推理资格验证。
- 运行环境：确认没有运行中业务 job 后，人工 3100/8100 应用 0212，刷新本 checkout 的 API、普通/维护 worker 与 beat；最终 doctor healthy，HTTP 均 200，新任务已注册到 cleanup，实际 beat 派发后执行计数为 1，lease 表为空。没有重建任何后端/ML 镜像或修改共享 .env、凭据、endpoint 和业务原件。
- 正式文档：`docs-site/dev/concepts/{prediction-pipeline,storage-connections}.md`、`docs-site/dev/reference/storage-buckets.md`、`docs-site/ops/deploy/docker-compose.md`、`docs-site/user-guide/projects/ai-preannotate.md` 与 CHANGELOG Unreleased。没有新公共接口、依赖或环境变量。
- 清理：独立验收 worker 已退出，任务所属对象与账本已清除；test 数据库、Redis 和九桶由环境所有者销毁。临时脚本、SDK 测试 HTTP 服务、日志/XML、pytest 中间文件与本 checkout 测试缓存均已清理；人工环境、身份文件和业务数据保留。
- 剩余工作：C2-B 入队固定 Task/内容/Key、严格引用投递、服务器 receipt、取消与候选写入原子边界、两个重试入口及采纳仍需实施，交互/predict-test/mask repair 逐入口资格验证。输入账本不是授权证明，也不完成全局配额或持久原图缓存；本计划继续执行，不归档。

### E05-C2-B 结果事务前置 Outcome

- 图片覆盖删除与全部候选、元信息和显式 partial 失败记录同题提交；异常先完整回滚再保存失败，成功数和费用只计已提交结果。刷新 Task 物化计数避免覆盖后沿用旧值；回滚后后续 Task 和路由 backend 继续从稳定 UUID 解析。
- fresh AsyncJob 行锁覆盖取消、题目开始/结果提交、作业元信息合并、最终批次/作业完成和迟到失败回调。推理前释放作业锁；取消标记先入库，broker revoke 随后尽力执行。视频逐帧保留既有独立清理/取消合同，没有将其误报为图片逐题原子替换。
- 本地 177 个不同 API 用例通过：主批 176 个，新增元信息合并前已终态取消的专项补测通过、不重复计算原用例。覆盖真实独立连接的取消先赢、单题结果先赢、最终完成先赢；第二结果普通/SQL 异常、旧候选/采纳标注/证据回滚、费用/计数、partial 成功/记录失败、draft 生命周期、后续题 ORM 恢复、frame 分派及既有 pipeline/模型快照/工作台范围。
- 真实 Celery 临时 worker 消费三条作业，真实 HTTP 协议夹具拉取独立 MinIO 图片并逐次校验 SHA：正常覆盖提交两候选、第二结果坏数据整题回滚且保留旧候选、在途取消丢弃新结果。该夹具验证执行协议及事务，不代表真实模型质量或引用消费者已获得资格。
- 正式 API/用户/架构说明、README、CHANGELOG 和 OpenAPI 取消描述同步；Ruff、OpenAPI 核对、196 页 frontmatter、198 页导航和 diff 检查通过。只读复核发现并修复 frame overwrite 回归，最终无阻断结论。没有前端样式或控件改动，不申报新增 UI 浏览器验收。
- 人工环境无运行中业务任务时刷新当前 checkout API、普通/维护 worker 与 beat；3100/8100 healthy，两个 worker 注册 batch_predict/输入清理任务，订阅数量分别 8/2（含节点直达队列）。不重建镜像，不修改共享 .env、依赖、凭据、endpoint 或业务原件。任务所属测试资源在提交前由环境所有者销毁，临时脚本、HTTP 服务、worker、日志/XML 和测试缓存清理。
- 完整 C2-B 继续实施：入队固定 Task/内容/Key、同快照引用投递、专有 receipt、两个失败重试入口和采纳仍未开放；交互/predict-test/mask repair 保留独立资格门禁。本计划与整套 Epic 目标均未完成，不归档。

### E05-C2-B 后台引用图片预标 Outcome

- 本增量完成 bulk 与 workbench 后台图片预标主路径：固定实际任务（含空范围）、版本、原 Key、模型与内容身份；pending 重发保留首次证明。真实 worker 节点严格捕获一次图片，经独占临时输入供根阶段、geometry 和 ROI 使用，最终短事务复查并签发 session/事务/Task 绑定 receipt。公开元数据仅保留内容及执行溯源。
- 复用导出的低层内容绑定和生命周期锁，保留独立业务授权；frame 在旧清理前拒绝引用，阶段资格失败不能降为模型 partial，伪造或跨 session/事务/Task、嵌套事务 receipt 均拒绝。
- 本地 288 个不同 API 用例通过（227 项执行/管线/权限/输入/引用导出/事务回归，加 61 项打包/最终守卫/导出 worker/严格内容回归）。覆盖原 Key 轮换、吊销、过期和 scope、未知节点、来源变化、Task 版本、最终取消、原件在模型执行期间变化但同快照 ROI 保持一致、幂等空范围及旧候选保留。只读复核未发现新的阻断问题。
- 真实一次性 Celery worker 与 HTTP 协议夹具完成五种场景，实际 HTTP 拉取 MinIO 临时输入并校验原图 SHA 和 ROI：单阶段与三阶段成功；模型返回前撤销原 Key、取消、捕获前原件被替换均不写新候选且保留旧候选。五项结束后输入回收记录及临时对象均为零。夹具验证平台协议和事务，不代表真实模型质量、S3/SFTP 生产网络或规模性能资格。
- Ruff、OpenAPI 快照、196 页 frontmatter 与198 页导航检查通过；没有公共请求 schema、新依赖、环境变量或迁移变更。同步 README、用户指南、API 和架构文档及 CHANGELOG。
- 人工环境 3100/8100 沿用原 dev 数据，确认无活动作业后刷新普通/maintenance worker 与 beat；任务注册、8/2 队列订阅及真实 cleanup 派发通过。未重建镜像。独立 test 数据库、Redis 与九桶按 owner 确认销毁，任务所属脚本、日志与缓存在验证后清理。
- 完整 C2-B 继续执行：两个失败重试入口、预测采纳、交互/predict-test/mask repair 尚未接入；引用失败目前重新提交预标。E05-B、E10 血缘及整套 Epic 仍未完成，不归档，不声称远程 CI 或生产部署验收。

### E05-C2-B 失败重试与普通几何采纳 Outcome

- 两个失败重试入口共用准备、设备队列和发布。引用失败使用原模型快照与 Item/Asset/SHA/大小/尺寸，另行固定当前 Task 版本、新 actor/Key 及来源状态，并预建 durable prediction_retry。缺少原内容或模型证明的历史引用失败明确拒绝，要求重新预标；原作业过期不改变失败项仍持有的快照。
- 复用引用输入 owner 的真实 worker 节点、一次严格快照、geometry/ROI 和专有写入 receipt。原件和模型 I/O 期间不持 FailedPrediction 行锁，最终共同锁 job/失败项及当前授权与内容，一起提交候选、partial 替代、失败删除、作业完成与通知；失败先回滚，再只处理匹配 attempt 的计数与 claim。发布响应丢失不撤销 running attempt；Celery 中断、重复终态及迟到旧队列消息不清除后续 attempt。通用 signal 终态前刷新并锁定作业，避免迟到失败覆盖已完成结果。
- 采纳统一核对 URL Task 与候选归属（托管也覆盖）；引用普通几何要求当前读取和标注权限、严格 API 原图及候选内容身份。最终锁下刷新 TaskBatch，锁住候选与元信息、Key 和内容生命周期；内部凭证绑定 session/非嵌套事务、actor、Task、候选与正文/溯源摘要。历史推理 Task.version 不作永久门槛，draft 预标后正常分派/激活及版本变化可采纳；类别、属性、贡献者、审核和统计沿用既有流程。原生 mask 在预测物化和采纳前继续明确拒绝。
- 最终 281 个不同本地 API 用例通过，按最新 JUnit 逐用例去重；包含两个入口、新 Key、原内容、版本/dismiss/partial/第二结果失败整题回滚、重复消费、signal/迟到占用、历史 CPU 路由、分派后采纳、两个 Key scope、源/原件/正文/权限变化、跨 Task、异常 UUID/几何及 receipt 失效，另含现有 managed/native mask、角色与管线回归。修正通知测试对事务内相同时间戳顺序的假设，断言仍核对各类型与目标/内容。
- 真实 ASGI API→一次性 Celery worker→HTTP 协议夹具→MinIO 验证三阶段重试成功、模型返回前撤 Key、第二结果元信息坏数据及真实 Celery 中断；失败保留原候选并释放本 attempt。实际原图 SHA 与 ROI 拉取核对，结束后 lease 和临时对象清零。分派/版本变化后采纳成功，另一连接占用候选行返回409；独立连接仅改批次分派或状态时返回404、零标注且不覆盖并发状态。夹具验证平台协议、事务与授权，不代表真实模型质量或生产远端网络资格。
- Ruff、OpenAPI 快照、196 页 frontmatter、198 页导航及最终 diff 检查通过。同步 README、用户/API/架构文档、CHANGELOG 与执行队列；无新公共参数、依赖、环境变量、迁移或版本号。
- 人工环境3100/8100保留原dev数据，无活动作业时刷新普通/maintenance worker和beat；验证真实新retry签名、8/2队列和cleanup派发，不重建镜像。验证用独立test资源按owner销毁，脚本/日志/XML/缓存清理，未声称远程CI。
- 后续继续逐入口完成交互/predict-test/mask repair/native mask资格、E05-B缓存及E10血缘；完整E05与整套Epic仍未完成，不归档。

### E05-C2-B 同步交互、预测测试与原生掩码采纳 Outcome

- 已实现 `reference_inference.ReferenceImageOperation`：引用图片的同步交互和 predict-test 固定当前 Task.version、Item/Asset/SHA/大小/尺寸、来源/位置/连接修订、API 节点及原 Key proof，经严格原图快照和独占临时输入调用现有 HTTP 后端。请求使用独立 UUID 与明确账本 owner，不创建伪造 AsyncJob，不重建镜像。
- 响应前按账号/项目/成员→Key→Task 编辑 advisory→批次/Task→内容生命周期复验，刷新批次状态和 fallback 分派。advisory 使用 try-lock，忙时有界返回 409；授权检查只读未过期编辑锁，避免清理副作用跨 I/O。predict-test 保留管理者可读测试语义，交互保持可编辑、分派、编辑锁、审核证据和 AI 开关约束；setup 后、真正推理前重新验证。
- 原生候选 HMAC receipt 与 logits 会话绑定原图身份，旧无证据或另一份内容的会话拒绝。原生采纳重新严格核验当前原图、读取/写入及精修读取权限，经独立私有 receipt 接入现有 atomic owner；receipt 绑定当前 session、非嵌套事务、actor、Task、请求与掩码摘要，普通后台 receipt 继续拒绝原生 Mask。精修保留 If-Match 与源 Annotation 版本/digest 合同。
- Prediction、Annotation、内容血缘、贡献者、审计和 decision 同事务。decision 持久保留非授权原图证据，候选凭证 15 分钟到期后，相同请求仍可在原 decision 期限内核验重放，不重复写候选；当前权限或原图失效仍拒绝。公开响应不输出内部 decision 证据。
- 本地累计 **233 个不同用例**的最终结果通过，包含托管交互/原生掩码/模型权限、流水线验证、引用预标/重试/采纳、临时输入、任务锁及新增同步专项；最后干净环境 93 例与只读锁调整后 54 例通过，重复运行不重复累计。覆盖中途撤 Key/收 scope/停账户、Task/来源/位置变化、批次单独变更、其他人编辑锁、取消、PUT 不确定恢复、旧 logits、跨 session/事务/actor/请求/掩码的私有凭证拒绝、凭证和 decision 不同 TTL 重放。
- 真实 ASGI→现有 HTTP 协议夹具→一次性 MinIO 验证原生掩码创建、同对象精修及采纳、predict-test；三个推理请求实际读取与原件一致的字节和 SHA。输出 Mask 经过真实对象存储与既有像素验证，临时输入对象和账本清零；夹具输出对象按精确键清理。这证明协议与事务接入，不宣称实际 SAM 模型质量或生产网络资格。
- OpenAPI snapshot 无变化，Ruff 和格式检查通过，196 页 frontmatter 与 198 页导航检查通过。正式文档同步 README、CHANGELOG、用户预标/目录导入说明、ML/预测/严格内容/Mask 采纳 API、预测管线与存储架构。
- 人工环境保留 3100/8100，空闲后刷新普通/维护 worker；doctor healthy，两个 HTTP 入口均 200，两个节点队列分别 8/2，实际清理任务成功，beat 正常，未重建后端或 ML 镜像。一次性测试数据库、Redis、九用途桶及本轮脚本/日志/缓存清理；Orca 指定中继仍 ECONNREFUSED，不切换二进制或另建工作树。
- 后续推进批量 mask repair 的入队授权、worker 严格输入、取消与候选结果共事务，以及确定性修复/恢复资格；后台原生掩码、E05-B 缓存与 E10 血缘继续分别实施。完整 E05 与整套 Epic 仍在执行，不归档。

### E05-C2-B 批量修复执行原子性前置 Outcome

- Forward / rollback 在数据库发布前固定 AsyncJob UUID 与 Celery task ID，消息显式携带 Job；兼容旧消息时只按原消息记录解析，不读取最新 Batch 指针代替。fresh Job → Batch 边界核对当前尝试、actor、状态与计划摘要，运行中重复投递和恢复后旧消息均退出。execute / resume / rollback 的锁查询刷新 identity map，阻止并发请求按缓存状态覆盖新尝试。
- 取消先持久化再 best-effort revoke。SAM 推理前和返回后都检查当前任务、审核权限、源版本及 native mask 开关；模型 I/O 不持最终锁。全部候选、元信息与 parent item 完成记录同事务提交。对象写入前检查 native gate，完整 shard / 候选组先取 Task，再预留上传并按全局顺序取得全部内容锁，之后才持 Annotation 行锁。
- 确定性分片跨片范围版本只允许冻结版本或该批已完成账本记录的 after version；外部版本变化拒绝。数据库提交已成功但应答丢失时，异常恢复先检查 fresh 持久记录，保留 completed / child ID，不将其覆盖成失败或重复执行。回滚提交应答丢失同样保留检查点。
- Tracker child 与 parent 派发意图一次落库，事务外使用相同 child / task ID 发布；未知应答保留可恢复意图，重试不创建第二个子任务，已确认发布不会被迟到异常降级。取消和失败保留已提交的 shard、候选与 child 为 partial；已有子任务使用自身取消和审阅流程。
- 回滚使用当前 Job 发起者，按不可变计划逆序逐片恢复 geometry 与检查点，避免依赖 JSONB 对象键顺序。失败后可携当前 result digest 创建新尝试；UI 提供“继续回滚”，跳过已完成检查点并继续版本冲突保护。
- 本地最终累计 **107 个不同 API 用例 + 5 个 Web 用例**通过，其中新增修复执行专项 22 例。覆盖旧/重复消息、缓存旧状态、推理期间取消/账户停用/源版本和 native gate 变化、多候选失败整组回滚、跨片准确版本推进与外部冲突、发布应答丢失、提交成功应答丢失、Tracker 复用与单调合并、回滚检查点和当前 actor，以及信号兜底。专项使用真实独立 PostgreSQL 事务；模型和对象存储使用测试替身，不宣称实际 SAM 模型质量。
- 实际浏览器加载真实修复组件，以隔离 API 夹具验证浅色/深色 × 桌面/窄屏，“继续回滚”触发正确入口并进入 rolling_back，无横向溢出或浏览器错误。组件夹具验证不冒充完整在线模型流程。OpenAPI snapshot 不变，Ruff / 格式 / ESLint / CSS tokens、196 页 frontmatter 与 198 页导航检查通过。
- 保留人工环境 3100/8100、业务数据及 beat；空闲后刷新普通/维护 Worker，核对两个节点的新签名，并实际派发 forward / rollback 的无归属消息，均安全完成且不创建业务行。不重建后端或 ML 镜像。一次性测试模式资源与任务临时文件清理后提交。
- 本前置没有开放原地引用批量修复，也没有新增 running 硬失联尝试的自动接管租约。下一主路径仍为入队内容/Key/模型冻结、真实 worker 严格原图及 deterministic mutation / SAM 专有 receipt；后台原生掩码、E05-B、E10 血缘与其余 Epic 保持执行中，不归档。

### E05-C2-B 引用图片批量修复与候选审阅 Outcome

- 引用图片确定性修复、局部 SAM 重跑、resume 和 rollback 取得独立资格。计划固定 Task/Item/Asset/SHA/大小/尺寸与来源/位置/连接修订，SAM 固定模型声明、变体和参数；execute/resume/rollback 各自捕获当前 actor/Key，但不替换原图或原模型。私有证明只保存在 Job payload，并从 AsyncJob DTO 过滤。引用视频/Tracker 继续拒绝，旧无证明计划需重新预览。
- 每个分片/候选组由 `ReferenceRepairUnit` 管理真实 Worker 节点严格快照。无 Worker 锚点不回退 API 或预览；确定性修复在 AI 关闭时仍可执行，SAM 额外复核 AI 开关。原图 I/O 和模型 HTTP 不跨最终写锁事务，独占输入由持久账本回收。实际路由实例的 setup 在图像 URL 发送前核对冻结模型；最终短事务锁定 pool/项目启用/成员/registry，保存实际 instance/pool 身份。协议仅证明声明配置，不编造 checkpoint 哈希。
- 专属 Mutation/Prediction receipt 绑定发行者、session、非嵌套事务、当前 actor/Task、单元及精确 payload；关闭快照后或跨会话/事务/主体/内容不可复用。最终复查当前 Job/Batch 尝试、取消、审核/编辑/Key 权限、native gate 与内容绑定。回滚同样严格读原图，已修改对象继续版本保护。
- SAM 候选、元信息、生成时结果/元信息摘要和单元完成记录同事务保存。既有 accept API 专门核对计划/单元/候选归属、原标注版本与几何摘要、当前权限及严格原图；RLE 内容锁先于源 Annotation 行锁。父作业 partial/cancelled 不撤销已提交候选，重复/并发采纳最多新建一条标注，已驳回候选不能采纳。原生开关、内容对象、源标注或溯源变化均拒绝。
- 引用预测 reject API 在当前 actor/Key/Task 编辑锁域内更新驳回状态，不要求原图在线或 native gate 开启。采纳与驳回的编辑 scope 忙冲突返回明确 `409`，不会抛内部异常。普通后台 raster receipt 和泛用引用 mutation 门禁保持独立。
- 本地累计 **303 个不同 API 用例**通过，其中本轮新增 **45 个引用修复专项**；按不同 testcase 合并最终成功结果，不累加重复运行次数。覆盖独立提交事务、真实请求 Key 捕获与私有字段过滤、撤销/scope 缩减与新 Key 恢复、内容/节点/来源/native/AI/模型变化、旧尝试、回滚、receipt 隔离、实际 accept/reject、离线驳回、重复/并发采纳及真实 Celery 包装器 hostname 传递；旧修复、普通几何采纳、同步交互/原生 Mask、输入回收、管线、路由客户端和 mutation 回归通过。
- 独立真实 HTTP/MinIO 验证：模型端下载到与固定原图完全相同的字节，SAM RLE 通过实际存储合同落库，随后经实际 API 采纳为关联 Annotation；输入对象及账本回收，任务所属 Mask 对象清理。使用协议夹具，不宣称真实 SAM 模型质量、生产性能或远程 CI 验收。无 UI 改动；OpenAPI snapshot 不变，Ruff/格式与文档 frontmatter/导航检查通过。
- 保留人工环境 3100/8100、业务数据和 beat。确认两类 Worker 均无 active/reserved/scheduled 与 pending/running job 后刷新当前 worktree 服务，再核对注册及两队列 forward/rollback 无归属消息，测试资源按精确 owner 清理；没有重建后端或 ML 镜像。完整 E05、后台原生 Mask、原图缓存/配额及 E10 血缘仍在执行，不归档。

### E05-C2-B 图片原生候选读取与画布 Outcome

- 静态内容 API 按 Task/Prediction/原始 shape 下标检查当前可见性、API Key 读取 scope、项目归属、驳回/替代与全局读取开关；Task/Item 图片类型、尺寸均在 304 前核验。复用 Annotation 的 ETag、private/no-cache 与结构化 409/503。普通采纳保留原始候选像素，交互采纳审计记录不当作候选，来源离线不阻止既存 RLE 读取，采纳仍严格核验原图。
- 工作台复用任务级像素 Worker、缓存与资源预算，保留逐 shape 工具单位与候选身份。原生候选显示真实孔洞/断连区域，按像素命中、聚焦与失败重试，来源/置信度及审阅过滤同步生效；与已保存 Mask 共用一层并关闭平滑插值，不额外增加 Konva Layer。不可见暖缓存解除选中/编辑固定额度，切题与卸载释放旧资源，迟到结果不发布到新题。旧内部 shape 的退役工具单位按几何兼容派生，项目已绑定的原生类别不重复 alias 映射。
- 本地最终累计 188 个不同 API 用例、89 个相关 Web 用例和 4 个 SDK 合同用例通过；按各用例最新执行结果去重，未将修正前失败重算为新增。覆盖当前权限/Key scope、原始下标、弱/多值 ETag、禁读/错归属/尺寸变化先于 304、来源离线可读但采纳拒绝、普通采纳像素不变、存储错误重试、混合工具、过滤去固定及切题异步释放。Web 全量额外发现原 HEAD 已存在的 `filterCoverage` datasets 入口断言失败；该文件与页面未在此切片修改，不将全量宣称通过。
- 独立 e2e DB/Redis/九桶上的真实 MinIO 普通/gzip 像素，经 Chrome 150（Linux、DPR 1）真实 Canvas 渲染，1600×1000 与 768×900 的明暗截图已检查，页面无横向溢出。636/607 px、2 组件、1 孔洞一致；洞内点击不选、断连部分可选；来源过滤、切题清空、故障注入/重试、键盘采纳/驳回与刷新均核验。采纳后为 raster_mask，原候选 200/304、驳回候选 404；资源账本 invariantOk。控制台无运行异常；夹具未启动模型，setup/capabilities 的 502 与注入的网络失败为已知验收边界，不代表模型或硬件 GPU 资格。
- OpenAPI、生成客户端、581 路由索引、SDK exclusion 与用户/API/架构文档同步。人工 3100/8100、普通/维护 Worker 和 beat 保留，不重建镜像。文档站构建、196 页 frontmatter、198 页导航、Web 类型检查与 lint/CSS token 检查通过（既有告警保留）。隔离 test/e2e 资源已由 owner 销毁；人工业务表计数与输入 lease 数均未变化，两类 Worker 和原 beat 仍在线。后台引用原生生成/采纳的专用 payload/内容资格、两种失败重试仍为下一段必要实施，E05-B 缓存、E10 血缘与完整 Epic 继续执行，不归档。

### E05-C2-B 后台引用原生结果 Outcome

- 基线 `ee7730c0f`；本节与实现同次提交。`ReferencePredictionJob` 为受控整图分割根阶段的每条结果颁发独立内部资格，批量/当前题及两个失败重试入口复用；整组在首个对象 PUT 前核验固定原图尺寸、RLE 精确集合与预算、冻结模型/运行时、映射后项目类别及形状来源。普通、交互和修复凭证不能混用，保留字段不接受外部载荷注入。
- 内存资格绑定 owner、session、非嵌套事务、Task/Project 与完整写入正文；关闭、过期或原图快照关闭后拒绝继续写入。生成记录绑定最终候选身份和稳定元信息，包含历史冻结事实；清理原始 Job/执行快照或模型部署变为不可用后，仍可按当前严格原图及权限采纳合格旧结果。复制到另一候选、正文/模型/阶段变化、当前 gate 关闭和替代状态均拒绝。该记录保障服务入口的一致性，不声称防御任意数据库篡改。
- 保留非连续实例索引与已裁剪对象包，不重复映射原生类别。向量子形状验证实际执行、父阶段链及变换契约，兼容透传属性、已移除 root 与 intermediate。当前 ledger 未保留被丢弃祖先的完整几何，不声称重新计算所有 crop 映射；原生分割子阶段仍明确失败。审阅回归发现并修复共享采纳入口忽略驳回状态的问题：指定已驳回 shape 返回409，整组只接受剩余下标。
- 最新结果去重后累计334个不同后端用例通过，包含专用证明、普通引用门禁、受控模型、所有 Mask 写入 gate、原生/普通采纳与修复回归。新增可重复 HTTP/MinIO 回归。另在隔离 test DB/Redis/九桶内，独立真实 Celery消费者处理整图根7首次预标、HTTP503后的单项重试与作业重试；5次HTTP均读取同一原图摘要，3次原生候选读取/采纳保持RLE摘要，临时输入与lease全部回收。模型端点为确定性CPU协议夹具，不冒充真实权重或GPU质量资格；本轮无前端修改，不重复此前已完成的像素画布验收。
- 用户/API/架构、README与CHANGELOG同步，无新公开schema、依赖、迁移或镜像构建。Python lint/format、文档构建、196页frontmatter、198页导航及197页可读性检查通过。
- 人工环境刷新曾因单独HUP触发监督进程退出；原拥有服务自动恢复，已核对API8100/Web3100为200、两类新Worker注册与两队列空对象派发SUCCESS，原beat进程保留。业务计数仍为8数据集/12样本/7项目/12任务/7标注/0预测/67作业，0活动作业及0输入lease。运行指南记录通过环境owner刷新，避免再次依赖单worker HUP。任务所属测试资源及生成中间产物清理后提交；E05-B持久原图缓存/配额、其余消费者与E10血缘继续推进，完整E05与整套Epic不归档。

### E05-B1 原图缓存归属与配额 Outcome

- 新增迁移 `0213`、独立缓存账本/读租约、内部缓存服务和 `cleanup` 周期任务。上传前持久预约完整字节，位置预算包含上传中/可用/待删除代；同 Asset 去重，完整 SHA 实物核验后发布独立 `original_cache` locator。四个源快照同时填充无需第二份快照名额，源快照所有权仍归调用者。
- 发布在 Location → Source → Entry 锁序下复验策略、修订、目的与额度；可选保留遇业务锁时有界失败，不提交或等待调用者自己的业务事务。源/位置的公开策略仍只有 `none/preview`，内部默认零额度，数据库预留 `on_demand` 不代表产品入口已经开放。
- 完整缓存读前/读后核对当前 binding，重新捕获严格快照；LRU 在拿到行锁后复查读租约。取消后物理 SDK 读取未结束时保留有期限租约，防止提前淘汰；过期失联读租约可恢复。TTL 固定、删除失败保留额度、已确认/不确定 PUT 分开处理，旧代精确清理不影响新代、托管 locator 或相邻对象。业务删除不级联抹掉回收证据，目的服务器变化拒绝向新目标删除。
- 按各用例最新结果去重，累计 **130 个不同后端用例通过**。包含 31 个缓存专项、空表迁移往返与四类有数据降级保护、真实 MinIO + 独立 Celery Worker 清理，以及严格读取、引用、目录策略、桶盘点和临时预测输入回归。回归中将旧 ROI 测试的假对象改为实际 PredictionResult 协议类型，恢复真实 ROI/取消验证；没有为通过夹具放松生产合同。
- 真实 MinIO 完成实际上传、严格读回及独立 `cleanup` 消费者删除；测试前缀和回收记录清零。人工环境通过拥有的监督服务应用迁移、刷新两类 Worker 与 beat，两个节点都注册清理任务，分别向 cleanup/maintenance 派发空账本任务成功。API8100/Web3100 均 200，仍为 8 数据集/12 样本/7 项目/12 任务/7 标注/0 预测/67 作业，0 活动作业/输入 lease/缓存账本；未构建镜像。
- 架构、桶用途、运维、CHANGELOG 与执行队列已同步；公开 API/SDK 无变化，本轮无 UI 改动，不重复浏览器验收。Python lint/format、文档构建、196 页 frontmatter、198 页导航及197页可读性检查通过。独立只读复核发现的名额、租约和 binding 问题均已修复。
- 隔离 test 数据库、Redis 和九桶由资源 owner 销毁；任务自有报告、脚本、日志、文档构建和测试缓存清理后提交。下一段继续 B2 的逻辑授权拆分、reference_snapshot 缓存选择与按需保留、配额满时的 LRU/解释、位置管理/来源 effective policy、三策略预览兼容、状态和 Range 合同及完整消费者回归。B1 不宣称离线标注已可用，完整 E05 和整套 Epic 保持执行中。

### E05-B2-A 授权缓存读取 Outcome

- 基线 `66ab63d59`；本节与实现同次提交。逻辑身份与来源物理资格拆分，统一引用 reader 接入同一 Asset/SHA 的完整缓存。缓存 I/O 前后重新核对 Task/Item/接受修订、当前账号/成员/Key、来源/位置启用与策略；缓存实际字节仍完整校验并解码。没有来源挂载的 reader 可使用已验证副本，但不能因此获得来源管理或额外任务权限。
- 缓存缺失或损坏重新执行原来源物理资格，不回退预览。旧 Asset 可以读取自身旧副本，同名文件覆盖不自动接受新内容；质量检查仍读实际来源。首次严格读源后可选保留使用独立事务，调用方尚未提交的修改保持未提交；外层 Connection 事务跳过缓存接入。额度不足时最多淘汰八代可删除缓存，每次确认删除后重试预约；单图大于总额度不淘汰，删除/上传失败不妨碍返回已验证源快照。请求预算429/504不退休健康缓存，HTTP Range 保持416。
- 按各用例最新结果去重，累计 **433 个不同后端用例通过**。包括独立提交事务的授权/撤权/策略收回、缓存离线与旧内容、配额/LRU/删除失败、调用方事务隔离、来源质量、训练同内容冻结、推理输入捕获，以及引用预测/修复/原生结果、导出、S3/SFTP和OpenAPI合同回归。实际 ASGI 读图与手工标注保存覆盖来源离线，并以真实 MinIO 验证完整缓存；独立 Celery 清理回归通过。推理捕获测试不等于已完成独立 Worker/HTTP 的缓存预测投递资格，后者继续列入 B2-B。
- 正式内容 API、导出/预测和存储/管线架构说明、CHANGELOG及执行队列同步；公开策略仍为 `none/preview`，本段没有新公开 schema、SDK 生成物、依赖、迁移、前端改动或镜像构建，不声称已开放产品离线模式或完成浏览器验收。Python lint/format、文档构建、196页frontmatter、198页导航、197页可读性及 diff 检查通过。
- 通过现有监督服务刷新人工环境，API8100/Web3100均200、doctor健康、两个Worker空闲并注册预测与缓存任务；分别向cleanup/maintenance派发空账本清理均成功并清除结果键，beat运行。业务仍为8数据集/12样本/7项目/12任务/7标注/0预测/67作业，0活动作业/输入lease/缓存账本。隔离test数据库、Redis和九桶已由owner销毁，任务自有报告与构建缓存清理后提交，保留人工环境。
- 下一段 B2-B 开放位置额度/TTL配置、用量与受控清理，接入来源 effective policy、preview三策略兼容与明确可用性状态，完成API/SDK及真实浏览器/缓存预测投递验收。完整E05及整套Epic保持执行中，不归档。

### E05-B2-B 缓存配置、状态与实际预测 Outcome

- 接续 `8408dd61e`；独立 Worker 资格测试已提交 `c126f3bcd`，其余实现与本节同次提交。位置及来源开放 `none/preview/on_demand`；超管配置严格整数额度和固定TTL，来源返回请求、位置上限与实际生效策略。按需原图继续允许预览。位置保存、创建和清理均在等待锁后重新核对当前活跃超管、原始Key身份及scope，撤权或轮换后不提交。
- 用量只读独立完整缓存账本，分别计量上传中、已登记、待删除及异常。清理限定位置、当前修订、观测截止时间与最多100份副本；跳过活跃读取，HTTP只封存并移交现有周期任务，实际删除前继续占用额度，不删除来源文件、预览、托管原件或训练副本。样本及来源诊断分别报告原件历史观测与合格完整缓存记录，不以缓存命中覆盖来源“缺失/变化”。
- 存储详情沿用居中滚动对话框，提供额度/TTL/策略、用量和清理范围确认；来源页解释实际生效策略。保持13px正文、11px灰色下方提示及简短选项，权限失效时隐藏旧用量和写入入口，允许明确重试。浏览器在1440×1000与390×844、明暗主题检查存储和来源界面，无横向溢出、控制台错误或所查页面失败请求。实际验证源文件删除后的缓存读图、源仍显示缺失、清理进入待删除继续计费、8MiB/61秒准确保存，以及位置降级后来源请求仍为按需但实际生效仅预览。
- 最新结果按用例去重，**373个不同后端用例、54个不同Web用例及4个SDK覆盖合同用例通过**。覆盖策略矩阵、配置权限/修订/边界、独立事务撤权与Key轮换、预览兼容、租约清理、来源状态、引用消费者、训练/导出、S3/SFTP和OpenAPI合同。独立真实Celery Worker经HTTP/MinIO在来源文件与探测证据均移除、节点无源挂载配置时，读取相同SHA的完整缓存，完成受控原生预测、候选读取与采纳；临时输入和租约清零。模型为确定性CPU协议夹具，不代表真实GPU权重质量或生产跨站网络资格。
- `README.md`、`CHANGELOG.md`、用户导入指南、内容/存储位置/预测/导出API和存储/预测架构说明已同步；OpenAPI、前端生成类型、路由索引与SDK覆盖清单更新。无新依赖、迁移、版本或镜像构建。Python lint/format、Web类型/ESLint/CSS token、文档构建、196页frontmatter、198页导航与197页可读性检查通过；保留既有CSS层级及测试AsyncMock告警，不宣称远程CI结果。
- 人工3100/8100及普通/维护Worker、beat保留，更新的预测与缓存任务已注册且两队列空账本派发成功。临时来源、样本、原图/预览副本、通知、审计和作业已精确清理；业务仍为8数据集/12样本/7项目/12任务/7标注/0预测/67作业，0活动作业、输入租约和缓存账本。隔离test数据库、Redis和九桶由owner销毁，任务浏览器及中间产物清理后提交。
- E05-B本地图片路径完成；完整E05的其余消费者和故事/验收矩阵仍须逐项核对，不归档。实施队列下一项为E10-B2冻结血缘与差异解释；节点训练缓存仍须有重复传输的实测瓶颈才启动。

### E05-D Outcome — 签名端点与原始范围归档

- 对照原始 S01–S06、AC01–AC11 与七项工程交付，统一读取／预览、严格快照、内容身份、缓存治理、当前授权和核心图片消费者均有实现及本地证据。E05-D 补齐剩余签名端点缺口，本计划归档。前文各阶段的“未完成／不归档”为当时记录，不再派生新的前置条件。
- 签名由原客户端在 `before-sign.s3` 前选择 browser/internal/ml origin，预测保留确切 VersionId，上传 Content-Type 和原有效期不变。上下文按 client 隔离并在失败后恢复，不新增客户端、物理 I/O 或绕过归属许可。相对代理签内部 Host，绝对代理签公共 Host；路径式桶地址不足时明确拒绝，不丢桶名。
- 本次338项不同后端检查通过，覆盖端点隔离、并发／异常恢复、上传与版本参数、预测输入、导出媒体、训练／模型消费者、视频输入和管线校验。另1项既有 `test_pending_job_replay_keeps_original_release_after_new_publication` 返回 `no_readable_original`；已在修改前 `fa6b945bb` 的独立源码快照执行并复现同一失败，不宣称全套回归通过，也不放宽严格原图门禁。
- 真实MinIO验证中文／保留字符、不可变版本、差异Host GET／PUT、相对与绝对代理、错误Host及过期签名拒绝。独立Celery Worker经不同Host向HTTP模型夹具供图并采纳通过；真实浏览器通过现有3100同源代理完整读取27字节；已有镜像的临时只读容器经网桥网关读取一致原件，不加载模型或重建镜像。
- 浏览器资格只覆盖当前同源部署；绝对公共origin的HTTP测试不冒充浏览器跨域CORS验收，TLS、CORS、异地网络及未指定S3产品仍是目标部署条件。完整视频跨来源缓存／转码、站点预取与Tracker M2不增加为本计划归档条件。算法质量、会话和审核改造不在本片范围。
- 两类人工Worker空闲后通过原owner刷新，各注册73项任务并实际完成不存在扫描的无副作用派发；3100／8100和原Beat健康。13张业务表85行、24个对象完整字节、12个源文件指纹与刷新前一致；未修改共享环境配置。一次性test模式已由owner销毁，临时容器和浏览器关闭，任务中间产物清理后提交。
- 正式文档：`docs-site/dev/concepts/storage-connections.md`、`docs-site/dev/concepts/prediction-pipeline.md`、`docs-site/ops/deploy/docker-compose.md`、`docs-site/dev/troubleshooting/container-networking.md`、`docs-site/user-guide/datasets/storage-connections.md`、`DEV.md`及`CHANGELOG.md`。OpenAPI路由／schema未改变；Ruff、文档构建／frontmatter／readability及diff检查通过。提交随本Outcome落地，不分配版本或宣称远程CI。

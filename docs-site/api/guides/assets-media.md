---
audience: [dev, operator]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# 独立图片与视频媒体接口

`apps/assets-media` 持有图片尺寸、缩略图、金字塔，以及视频元信息、帧时间表、海报、播放副本、帧、分块、回填和派生缓存清理。公开 REST 仍由主 API 认证并检查原有 scope；内部控制服务再通过原生 owner 能力检查当前账号、素材及 Task 可见性。任务帧重试还在受理事务内检查当前标注写权限。

## 运行与协议

控制进程使用专用 Bearer token，只提供控制与查询接口；执行进程通过数据库轮询领取作业，没有公开领取 HTTP 接口，也不依赖 Celery 或 Redis。两者使用分开的 SQL LOGIN 和 S3 身份。API 的 `ASSETS_MEDIA_URL` 为空时装配同一套本地实现；显式选中远程后，故障不会回退到本地执行。

协议标识为 `aap-assets-media/v1`。请求和响应的精确模型随包发布为 `protocol.schema.json`，HTTP 路径及错误状态随包发布为 `openapi.snapshot.json`。成功信封为 `{schema, ok: true, data}`，失败信封为 `{schema, ok: false, error: {code}}`。不要把内部 token、签名 URL 或数据库凭据写入日志。

| 能力                                                                         | 调用入口                                           |
| ---------------------------------------------------------------------------- | -------------------------------------------------- |
| 来源、摘要、视频总数、准备、状态、时间表、样本、失败、重试、失效、取消与偏好 | `POST /v1/commands/{operation}`；13 个当前主体操作 |
| 已接受后台工作的来源、受理、状态、回填扫描及帧状态                           | 同一 commands 入口；5 个受信任系统操作             |
| 来源与派生对象的签名引用、批量来源引用                                       | 6 个专用 reference 路径，字段见 HTTP 工件          |
| 领取、续租、时间表暂存、发布、失败、过期、垃圾领取/完成、回填推进与用量      | 10 个原生执行能力；不通过控制 HTTP 暴露            |

共享控制 LOGIN 精确授予 40 个函数，其中视频 18 个、图片 14 个、受理输入 4 个、批量输入清单 4 个；视频执行 LOGIN 保留 10 个函数，图片执行使用独立 LOGIN 的 10 个函数（图片 6 个、输入 4 个）。它们均没有私有业务表、列、序列、其他 owner schema、成员继承或 DDL 权限。函数的外层事务仍由调用方持有；默认 HTTP 绑定在返回前验证并提交完整结果。

请求最多 8 MiB、响应最多 16 MiB。记录页最多 500 项、记录 JSON 最多 15 MiB；完整时间表和 packet 样本按页读取。调用方必须核对来源快照、依赖版本、顺序、总数、游标及完成标志，不能只取第一页。packet 数组保留解码顺序，展示帧排名可因 B 帧不单调。

## 作业、来源与对象

显式请求携带操作 UUID，重复提交相同操作可恢复已受理结果，换参数复用同一 UUID 会冲突。读取触发的隐式准备不为每次轮询建立不可变回执。作业受理后以来源代次、参数和作业代次继续执行；原发起者停用或失去管理权不撤销既有执行权，显式取消、来源变化或删除会阻止后续发布。

每次执行领取独立租约，输出写入 `media-assets/{job}/g{generation}/a{lease}/`。源对象条件读取、内容校验、最终 HEAD 和数据库 CAS 共同约束发布；租约过期或被替代的旧进程不能激活输出。元信息探测成功后即可认证当前代次的尺寸和帧数；海报、播放转换或时间表失败会保留各自错误，不把已验证的元信息变成不可用。H.264 原视频不需要额外播放副本。时间表先按页暂存，全部验证成功后才整体替换。精确帧图片按解码后的源帧序号选取，避免把稀疏插值或毫秒取整当作精确帧时间；当前从头解码并保留原超时，长视频逐帧成本仍需单独容量验证。

帧或分块刷新失败时，已有可用对象继续供普通读取，错误仍进入管理失败列表。“仅重试失败项”会清除选中项的错误并推进执行代次；同一项已排队后再次提交该筛选不会重复重置。时间表探测失败保留原有索引和修订，只有完整新时间表通过验证后才替换。

第一次 HEAD 只绑定已登记素材的对象身份；之后发现意外覆盖会标记来源漂移，停止发布并要求来源 owner 核对登记。媒体服务不能把外部覆盖静默当作新的已授权来源，也没有原始素材写入或删除权限。

准备就绪的帧、分块和海报在签名前检查对象存在。缺失对象以匹配当前来源和 key 的条件失效，再持久排队重建。海报独立于临时帧缓存，缓存过期不回收当前海报。垃圾清理保留可重试的责任记录，并在删除前核对没有活跃引用或租约；旧执行进程迟到写入的前缀会再次清扫。

Task 列表的原始文件引用不依赖 S3 HEAD，避免存储短时不可用使整个列表失败。需要精确媒体或后台导出的引用会核对当前源身份。来源已知漂移时，Task 响应不发出旧来源 URL，显式文件 URL 请求返回冲突。

## 部署与范围

迁移、精确 grants、secret 文件、就绪探针及显式 Compose 入口见仓库 `DEV.md` 的“视频媒体的独立控制与执行”。可选 worker 会继续使用显式配置的 `MEDIA_PROBE_URL`，远程探测故障也不会回退。Go 仅保留为单独实验。

## 图片合同与重试

图片使用独立的 `aap-assets-images/v1` 信封，精确数据与 HTTP 工件在包的 `aap_assets_images` 目录中。它与视频共用控制服务地址、token 和连接池；`POST /v1/images/commands/{operation}` 提供 9 个当前主体操作及 5 个受信任系统操作。来源、原图、批量来源、金字塔及切片签名使用该命名空间中的 reference 路径。6 个图片执行能力和 2 个图片垃圾能力只通过原生接口提供。

图片作业先冻结来源代次、快照、对象身份和处理策略，再由独立执行进程领取。上传、ZIP 和连接导入在登记事务中绑定已观察的原始对象并受理工作；兼容 Celery 消息只唤醒已受理作业。小图保留原缩略图和 blurhash 算法，超大图使用独立 libvips 子进程。对象读取带 ETag/version 条件，传输受作业截止时间约束；原始对象没有写入或删除权限。

Task 摘要明确区分 `missing`、`stale`、`inconsistent` 与生成状态，缺失时 `generation` 可以为空。已登记或探测的尺寸独立于缩略图是否就绪。`GET /api/v1/tasks/{task_id}/image-pyramid` 保留状态、manifest、overview 和 ETag；刷新失败时旧的可用输出继续提供，新的错误和 building 状态会改变 ETag。

`POST /api/v1/tasks/{task_id}/image-pyramid/retry` 可携带 UUID `Idempotency-Key`，返回 202 和持久 `job_id`。网络结果不确定时用同一个键重放；同键换参数返回冲突。`celery_task_id` 仅保留为兼容空字段，不再用于追踪图片作业。冷却返回 429 与 `Retry-After`，并发争用返回可重试的 409。

图片回填冻结选择范围和截止时间，按有界页分两阶段推进：先读取候选，锁外观察对象，再原子比较来源、受理子作业并提交游标回执。来源变化只跳过相应条目，存储暂时不可用会保留已受理工作。清理保留迟到写入的重扫责任；删除仍被相机、轨迹或视频执行记录引用的素材返回 409，先在项目内解除引用。

一般 Blob、章节和运维 reset 仍使用各自既有路径；其余推理消费方继续按各自输入迁移。完整模块状态与本地验收证据见[独立部署台账](/dev/concepts/module-deployment)、[ADR-0105](/dev/adr/0105-independent-video-media-owner)及 [ADR-0106](/dev/adr/0106-independent-image-media-owner)。

## 视频跟踪的原片输入

跟踪、范围传播和 Mask 修正使用 Labeling 的规范 `video_tracker_jobs`。创建作业时同事务捕获执行快照，通用作业摘要不参与输入授权。私有信封为 `aap-assets-video-tracker-control/v1`，与既有 Media 地址、Bearer token、数据库池和输入桶共用装配。

| 控制入口                                  | 请求与结果                                                            |
| ----------------------------------------- | --------------------------------------------------------------------- |
| `POST /v1/video-tracker-inputs/input`     | 当前 actor、tracker Job、Task UUID → 状态、固定执行事实及就绪计划摘要 |
| `POST /v1/video-tracker-inputs/prepare`   | 上述范围、Input UUID、request digest → 同一输入的准备状态             |
| `POST /v1/video-tracker-inputs/reference` | 准备范围与 plan digest → 状态、精确来源版本、短期 URL 与有效期        |

每次请求最多 64 KiB，响应最多 2 MiB，链接有效期最多一小时。公开控制值不含原始路径、对象 key、内部工作记录或租约；引用的 `source_version` 保存 VersionId、ETag、字节数与 SHA。调用方核对返回的作业、Task、Input、request／plan digest 及 URL 中的版本，不把这些身份值当成权限令牌。

`prepare` 在 native 事务外 HEAD 当前捕获来源，随后登记观察；Worker 使用既有原片执行器物化。首次未知对象身份在成功 HEAD 时固定字节身份，不宣称 HTTP 提交瞬间已经取得未知历史版本。准备未完成时不启动推理；每个窗口重新签发同一保留版本。当前来源变化或精确副本缺失会失败关闭，远端故障不切回当前 Task URL。

新引用仅供当前仍有效的 queued/running 作业。取消后停止新窗口；普通跟踪已完成的部分候选仍可审阅，修正取消会丢弃结果。暂存和采纳的来源围栏、原片级 Annotation pin 及保留查询是 Labeling 专属 native 能力，不作为 HTTP 操作。当前审核者可与原发起者不同，仍需满足现有权限、Task、分段与标注版本条件。

Accepted-input 垃圾回收还会查询 Jobs 当前 execution 的退役事实。运行中、已启动但没有账本、当前 execution 缺少 journal，或 journal 与当前 run 不匹配时，原片输入继续保留；只有当前 run 的 journal 同时证明 writer 已终结且 execution 已退役，或 Jobs 证明 Job 从未启动／已由删除围栏安全清除后，才解除 execution 保留。Annotation pin 会独立保留输入，仍存在的 Job 继续遵循现有状态与审核宽限规则。若 Labeling scope 缺失但 accepted input 仍为 ready，系统使用既有 Assets 事实恢复 Job 身份；scope 缺失且输入尚未 ready 时无法证明身份，Labeling 返回无效事实，Assets sweeper 会跳过该条目。它会继续保留并形成待修复积压；自动排空旧 worker 或人工修复入口尚未提供。

垃圾领取时也会重新核对尚未处理的 tracker tombstone：当前不可回收的 source 前缀会按清理租约时长延后，本页继续扫描，因此前面的保留项不会阻塞后续过期或已被新一代替代的前缀。清理 worker 默认租约为 600 秒，Native claim 接受 60–7200 秒。已领取的旧租约无法由 SQL 撤销；升级领取围栏前应停止 `apps/assets-cleanup` 并确认进程退出，再等待未完成租约到期。若不能确认进程退出，应先隔离其对象存储删除凭据。已经删除的对象版本不能通过此迁移恢复。

升级排空、Worker 选择与精确 grants 见 `DEV.md`；来源快照、保留和回退责任见 [ADR-0110](/dev/adr/0110-retained-tracker-video-inputs)。这条专用协议不扩大旧批量预标的 frame-only 准入。

## 已受理图片输入

`aap-assets-inputs/v1` 固定 Job、Task 受理版本与来源绑定、Assets 代次／快照和对象身份。`POST /v1/inputs/{accept,find,record,reference,verify-source}` 与媒体控制共用地址及认证；十二项 native 能力分配给控制、图片执行、Labeling 和清理身份。输入副本位于独立的 versioning 桶，引用绑定 VersionId、SHA 和大小；整图与 ROI 消费相同内容。最终 Labeling 在业务写事务中复核原输入及当前权威，并保留候选、失败项和标注的输入证据。

## 普通图片的批量输入清单

`aap-assets-input-manifest/v1` 面向普通图片批量预标，包含 Scene 模式图片项目中的单图 Task。此路径的推理输入是一张图片，清单不记录跨帧或相机上下文；显式场景执行单位、视频、逐帧、文字、跟踪及点云多相机输入继续按各自边界迁移。类型与两个工件位于媒体包的 `aap_assets_input_manifests` 命名空间；基础值和注入传输的客户端只依赖 Pydantic，HTTP 传输及服务运行依赖按可选包安装。

| 控制入口                           | 请求与结果                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------------ |
| `POST /v1/input-manifests/record`  | Job、actor、project 范围 → 清单 header                                         |
| `POST /v1/input-manifests/page`    | 范围、selection digest、可选 sealed digest、cursor、page size → 有序成员页     |
| `POST /v1/input-manifests/prepare` | 范围、selection digest、ordinal、Task ID → 同一成员与原 Input 收据             |
| `POST /v1/input-manifests/seal`    | 范围、selection digest → 完整 sealed header；未准备完返回 `manifest_not_ready` |

捕获使用 workflow 专属的 `aap_assets_v1.capture_accepted_image_input_manifest`，与 Job 在原事务共同提交；控制 HTTP 不提供捕获操作。选择值固定 `batch_id`、`task_ids`、`predict_mode` 三个键，包括显式 null。显式 Task 最多 200 项，项目／批次全选无新增总量上限。Job ID 同时作为清单 ID，Job 和队列消息只保存清单身份、摘要与数量，不携带全量成员。

header 保留 `candidate_count`、`selected_count`、`prepared_count`；`ready_count`、`failed_count` 和 `sealed_digest` 在准备期间为 null，完整封存后才固定。成员有稳定 ordinal，保留原 Task 版本与来源、Assets 快照、可选 Input 和固定失败原因。封存后的结果及摘要保持历史含义，后来输入过期不改写原封存结果。

清单请求最多 64 KiB，响应最多 2 MiB。每页至多 100 项，达到字节预算时可提前结束；消费者始终按 `next_cursor` 继续，不能假定每页恰好 100 项。`selection_digest` 固定逻辑选择，`sealed_digest` 固定最终 Input／失败结果，两者用途不同。

prepare 先读取原成员，已有 Input 或固定失败时直接重放。控制服务根据捕获快照观察来源，再提交 native 准备；HTTP 调用方不能提供伪造对象身份。已知对象身份必须匹配；首次未知身份以准备时首次成功 HEAD 为字节受理时点，提交瞬间不证明未知历史内容。来源或任务绑定变化使该成员失败，不会替换为当前源。

输入物化和准备等待发生在 Job 认领、覆盖清理和模型调用之前；临时繁忙继续等待同一作业。封存后逐页、逐题使用原输入，输入校验通过后才清理该题旧预测。后续页读取失败会记录作业失败及已处理进度；已提交候选保留，未处理部分需核对范围后重新提交。完整 Jobs 执行租约、进程接管及推理协议继续单独提取。升级排空与凭据配置见 `DEV.md`；设计取舍见 [ADR-0108](/dev/adr/0108-bulk-image-input-manifests)。

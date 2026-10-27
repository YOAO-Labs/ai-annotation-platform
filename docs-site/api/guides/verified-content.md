---
audience: [dev, project_admin, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# 内容核验与严格图片读取

这些接口提供完整图片的内容核验和请求内快照读取。服务器先读取全部字节、计算 SHA-256 并解码验证图片，再从同一临时快照发送响应。原路径或对象在此后被覆盖，不会改变正在发送的快照。全部路径以下列 `/api/v1` 前缀调用。

## 目录观测条目

| 方法 | 路径                                                               | 授权与行为                                                                  |
| ---- | ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| POST | `/dataset-scans/{run_id}/entries/{path_digest}/verify`             | 当前来源管理权限和 `datasets:write`；为完整扫描中的匹配文件核验当前图片内容 |
| GET  | `/dataset-scans/{run_id}/entries/{path_digest}/content?sha256=...` | 当前来源管理权限和 `datasets:read`；按已登记摘要严格读取                    |

来源管理权限为活跃超管，或创建该数据集且拥有位置使用权的活跃项目管理员；读取前后再次检查。撤回位置使用权阻止管理预览，已分配任务仍按下文任务权限读取。

从[观测条目列表](./dataset-sources)取得 `path_digest`。verify 返回 `sha256 / size_bytes / media_type / width / height`（尺寸按浏览器 EXIF 旋转后的逻辑方向）；条目列表增加 `verified_content` 与 `content_verified`。核验结果与原始 stat 观测分开保存，不包含绝对路径、节点指纹或文件内容。

要求 Run 已完整完成、数据集和来源仍存在且启用、来源与位置修订未变。必须先在**实际处理请求的 API 节点**完成当前 read 探测：调用位置 probe，`node_id=api` 并提供相对位置的 `sample_file`。worker 的 list 探测不能替代它。API 未挂载目录、探测过期或根身份变化时拒绝读取；不会自动把文件经 Celery/Redis 转发。多 API 实例应分别探测并暴露同一授权目录。首次核验要求扫描与 API 位于同一内核主机，并核对扫描固定的来源子目录和文件身份；跨主机、未知或重启前的主机证据返回 initial_verification_node_mismatch，需在可比较节点重新扫描。已建立的 SHA-256 可供其他已验证 API 节点读取同内容。

首次核验建立的是本次读取的内容身份，不能证明扫描时或旧标注曾读取的字节。设备号和 inode 属于各节点本地，不跨节点当内容证明。重复核验和后续读取必须符合已经保存的摘要，不能覆盖旧身份。文件与原始大小/时间观测不符时，需重新扫描。

上述挂载及同内核主机要求适用于服务器目录来源。Agent 位置由独立站点执行探测／扫描／读取，使用该 Agent 的根身份和远端观测；中心无需挂载来源目录。中心经受控中继接收完整字节，仍复核当前授权、摘要、大小及图片资格，不使用中心 inode 解释站点文件。

独立 verify 不创建 DatasetItem、Asset 或任务；需另外调用[登记接口](./dataset-sources#逐条登记原地图片)生成引用样本。已核验内容也不意味着原图以后一直在线；预览状态不由此次核验推导。

## 认证通用原件下载

下列接口读取托管原件的完整二进制，不进行图片解码。图片工作台、审核与数据管理预览、数据集原件下载，以及点云与相机使用认证客户端；不能将需要 Bearer 的路径直接放进原生 `<img>` 或 `<video>`。原生视频使用下文的准备与范围读取协议，数据集检查器的认证完整下载仍受512MiB限制。

| 资源        | 身份路径（GET）                                            | 下载路径（GET）                                   | 授权                                                 |
| ----------- | ---------------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------------- |
| Task 主原件 | `/files/tasks/{task_id}/original-identity`                 | `/files/tasks/{task_id}/original`                 | `tasks:read` 与当前 Task 可见性                      |
| 相机原件    | `/files/tasks/{task_id}/links/{link_id}/original-identity` | `/files/tasks/{task_id}/links/{link_id}/original` | `tasks:read`、当前 Task 可见性及其精确 camera\_ 关联 |
| DatasetItem | `/datasets/{dataset_id}/items/{item_id}/original-identity` | `/datasets/{dataset_id}/items/{item_id}/original` | `datasets:read` 与当前 Dataset 管理权限              |

身份响应包含 `owner_kind=dataset_asset|task_original`、`content_id`、`sha256`、`size_bytes`、`media_type`、`verification_state=verified`、`original_availability=unchecked` 和 `download_path`。这里的 `media_type` 是 image/video/point_cloud/other 等业务类别；身份查询不访问对象，既不证明当前在线，也不执行下载预算检查。响应不返回端点、桶或对象键。

非视频托管 Task/Item 的业务 DTO 返回 `file_url=null` 与平台相对 `original_identity_path`，不签发原件地址，也不在列取时逐项解析归属。未知历史原件仍可列取业务信息，身份解析另行明确报错。原地引用继续用既有 `storage_mode/content_sha256` 图片合同，视频 DTO 保留其播放分支。

点云 manifest 同样返回 `point_cloud_url=null`、`expires_in=0` 与主原件身份路径；相机保留真实 `link_id`、独立身份路径、尺寸及标定，`image_url=null`。主原件缺失不吞掉合法相机或 Scene/姿态元数据。浏览器只按精确 Task/link 查询身份，再使用内容 UUID/SHA 的下载路径；旧签名字段不参与回退。

下载必须同时提交 `content_id` UUID 与小写64位 `sha256`；可直接使用身份中的平台相对 `download_path`。旧身份或关联变化返回409，不按当前用途桶猜读。服务器从保存位置完整读取并核对 SHA、大小和当前目标条件，再复核原请求凭证、当前权限、精确关联及维护门禁；核验失败不发送原件字节。知道可见 Task 不能用于读取其他 Task 的相机链接；知道 Item ID 不能绕过路径中的 Dataset 归属。

运维已发布封存迁移映射时，完整逻辑地址及保存SHA／大小可解析到目标准确物理版本；请求字段、`content_id`和业务授权不变。两次HEAD、条件探测和GET均使用目标回执VersionId及ETag，不跟随同名键的latest。实际许可属于目标位置；目标维护、配置漂移或发布撤回使新读取拒绝，不因此开放停止来源，也不向客户端暴露物理路由。没有准确映射的未知原件继续沿原规则拒读。发布与撤回见[迁移映射运维](/ops/deploy/storage-migration-inventory#发布托管原件读取映射)。

响应固定 `application/octet-stream`、安全固定文件名 `attachment`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`，包含准确 `Content-Length`、SHA强ETag和 `X-Content-SHA256`。单文件最多512MiB，超限413且不读取对象；同时两份暂存，满额429。复制与发送各120秒为合作式期限，取消和超时会等待真实线程/句柄收尾，不能理解为硬墙钟保证。响应开始前也会监听客户端断开，取消复制并等待收尾；晚到的快照仍须关闭。暂存额度覆盖响应至真实文件关闭，与原图片核验四槽独立。

本接口拒绝 Range（416），不提供 HEAD（405）或条件304快捷返回。BMP/TIFF/GIF/SVG、点云、视频和空文件均保持已核验原始字节，但是否能由浏览器解码是独立能力；视频播放可使用下文独立准备协议，不能先整段转Blob作为默认播放。原地引用返回409 `reference_requires_verified_content`，继续使用下文既有图片接口，不扩大引用格式合同。未知、未核验、多原件位置或冲突关系明确拒绝，无签名URL降级。

## 视频原件准备与范围读取

这组接口为原生媒体提供可复用的托管视频原件表示。先用Bearer准备，收到的相对地址可作为同源 `<video src>`；浏览器自动发送限定租约路径的HttpOnly Cookie。地址和JSON中不含能力秘密或主凭证。视频工作台已使用此协议；原件字节保持不变，浏览器必须支持原容器和编码。转码代理、海报、预览帧和精确分块仍有各自派生合同。

托管视频Task/Item DTO只给出惰性 `original_identity_path`，`file_url`为空；未知历史仍可列出，身份或准备时明确拒绝。主video manifest提供metadata及准确Task身份路径，`video_url`为空。manifest-v2的 `video_url` 与chunks的 `fallback_video_url` 也为空，客户端不能按 `metadata.playback_path` 或旧用途桶拼接整段视频地址。现有视频工作台元数据仅来自直接关联的DatasetItem；独立TaskOriginal准备接口可用，不代表独立Task已具备该工作台的元数据。

| 方法       | 路径                                                                  | 授权与行为                                                                        |
| ---------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| POST       | `/files/tasks/{task_id}/original-playback`                            | `tasks:read`及当前Task可见性，准备其托管视频原件                                  |
| POST       | `/datasets/{dataset_id}/items/{item_id}/original-playback`            | `datasets:read`及当前Dataset管理权限，核对路径归属                                |
| GET / HEAD | `/media/original-playback/{lease_id}`                                 | 准备所得路径Cookie，以及每次重新核对的原Authority、关系、内容身份、位置和维护修订 |
| DELETE     | `/files/tasks/{task_id}/original-playback/{lease_id}`                 | Bearer、本人账号与准确资源/session，释放租约                                      |
| DELETE     | `/datasets/{dataset_id}/items/{item_id}/original-playback/{lease_id}` | Bearer、本人账号与准确资源/session，释放租约                                      |

POST提交 `content_id`、小写64位 `sha256` 和客户端登录会话的UUID `session_id`，多余字段拒绝。前两项来自认证身份接口；session只划分客户端资源，不是服务端权限证明。响应仅含 `lease_id / media_path / expires_at`，附 `aap_video_lease` Cookie：host-only、HttpOnly、SameSite=Strict，Path为准确media_path，HTTPS时Secure。自然期限不超过300秒或原凭证期限。非video为415，未知归属或旧内容为409，reference保持其既有读取合同。

首次准备按保存位置完整核对条件、SHA、精确大小、EOF及真实关闭，再复核当前授权与Gate。同Authority/session/资源/冻结来源的ready表示可复用，每次准备创建独立租约；正在准备时返回429 `video_playback_preparing`，不会再复制同一远端原件。GET/HEAD从已核验的本地匿名文件读取，不访问当前用途桶或隐式重建暂存。原User/Key、JWT自然期限/blacklist/代次、scope、Task/Item关系及Gate必须仍有效；授权等待后还会重核租约是否过期或退休，HEAD和范围错误也不绕过授权。

GET支持完整200及单段206：`bytes=start-end`、`bytes=start-`和`bytes=-suffix`，末端截断到真实大小。强ETag为 `"sha256:<SHA>"`，If-Range仅精确匹配该值才返回206；弱ETag、日期或不匹配值忽略Range并返回完整200。HEAD忽略Range、返回完整长度且无正文。未知range unit和合法多范围忽略并返回完整200；畸形bytes范围400，越界416附 `Content-Range: bytes */size`。空表示的正后缀范围返回完整空200。Range头最多512字符；If-Range失败时不再求值Range。

成功响应为 `application/octet-stream`，含准确Content-Length、Content-Range（仅206）、`Accept-Ranges: bytes`、强ETag、`X-Content-SHA256`、no-store/nosniff及CORP same-origin。能力仅用于GET/HEAD，不放入URL、日志或业务元数据；跨站/跨源的浏览器请求拒绝。DELETE携带 `{"session_id":"<原会话UUID>"}`；准确本人租约释放成功204并清对应Cookie，错误资源/session或他人账号404，已过期或不存在410。

每API进程最多两个stage、单个5GiB，声明暂存上限10GiB，另最多8个活动响应及每stage32个租约。容量429附 `Retry-After: 1`。准备900秒、远端transport30秒、单响应120秒均为合作式期限，实际线程收尾可能更长；这些额度与512MiB完整下载、图片快照、持久缓存分别计量。租约固定300秒，表示最迟3600秒退休，重复prepare可在原表示有效期内取得新租约。30秒回收器和API关闭收尾会先停止新读，再等待活动范围和真实文件关闭；未知关闭继续占额。

当前暂存为单API进程资源。重启、reload、到期或命中没有该租约的其他实例返回410 `video_playback_expired`；客户端需重新Bearer准备，不能反复重试同一media_path。此协议不提供跨实例共享暂存或集群总预算；既有转码/帧/chunk具有独立字节事实，不使用原件SHA为派生表示背书。

Web按账号/session、Task与内容身份管理逻辑舞台，换账号/任务时退休全部帧资源；同内容租约轮换仅替换原生传输，保留当前帧和草稿。同一已挂载舞台中视频信息短暂重取时，保留已确认元数据及逻辑舞台。刷新登录令牌仍遵循现有路由权限守卫：复核期间卸载工作台，退休旧媒体和局部草稿；通过权限复核后重新打开。临近到期提前prepare复用ready stage；替换或离开前先停止旧video源，再释放租约。原生错误以准确同源Cookie HEAD区分410与权限/解码失败，410有界重新准备，其他错误不降级签名地址；旧凭证的清理请求不触发新会话退出。QC定位和抓帧要求当前原件与原生画面真正就绪；JPEG编码前后均核对实际像素帧与逻辑帧，恢复定位期间不交付首帧冒充的截图。

## 图片任务

| 方法 | 路径                                        | 行为                                                                           |
| ---- | ------------------------------------------- | ------------------------------------------------------------------------------ |
| GET  | `/files/tasks/{task_id}/content-identity`   | 返回已核验原件的归属、摘要和大小；原图可用性仍为 `unchecked`，预览为 `unknown` |
| GET  | `/files/tasks/{task_id}/content?sha256=...` | 按固定托管位置或不可变目录引用和摘要读取完整图片快照                           |

两个入口均要求 `tasks:read`，并复用当前项目权限、批次状态和任务分派规则，包括获准读取的项目观察者。知道 Task/原件 ID 不代表有权限；任务之外的数据集或目录不会因此开放。读取后、响应前再次检查权限和绑定；原请求JWT过期、退出登录、会话代次变化，以及API Key的吊销、轮换、过期或scope收回，都会使在途读取失败。未核验历史Asset返回 `content_unverified`，不把旧hash hint或ETag当SHA-256。

身份响应含 `owner_kind=dataset_asset|task_original`。前者返回非空 `asset_id`、`task_original_id=null`；后者相反。身份查询只检查保存的关系、核验事实和位置配置，不读取物理对象，因此 `unchecked` 不表示原图当前在线。

Dataset原件通过Task的直接DatasetItem关联或唯一 `primary_lidar` 关联解析；两者都有时必须指向同一Item，冲突返回409 `original_relation_conflict`。独立Task则使用其TaskOriginal，不与Dataset关联混用。Dataset原件要求完整SHA、大小及唯一 `managed_original / present` 位置；已有完整核验事实的历史记录不强制补建Creation。多个候选位置返回 `original_location_ambiguous`，缓存或预览不作为托管原件候选。未知历史Task不按路径自动认领。

托管原件使用该记录保存的平台位置、桶和键，拒绝禁用或配置偏离的位置，不读取当前用途桶来猜测原件。每次先观察目标当前对象，确认条件读取有效，再完整核对大小、SHA及读取前后观测。历史写入ETag/VersionId只保留来源证据，不作为离线恢复目标的版本选择器。可覆盖S3 key也必须完成这些核验。

已弃用的 `/files/tasks/{task_id}/file-url` 在当前任务可见性检查后统一返回409 `original_requires_authenticated_download`，不再签发原件地址；视频原生 manifest 是独立入口。原地引用工作台使用严格图片内容接口。训练冻结、后台图片导出、后台图片预标及同步交互/预测测试及批量图片 Mask 修复通过各自严格快照合同消费引用，不使用旧签名地址。

## 响应、预算与清理

成功返回 `image/png`、`image/jpeg` 或 `image/webp`，含精确 Content-Length、`X-Content-SHA256`、基于完整摘要的强 ETag、`Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。仅支持单帧图片，请求必须带64位小写 SHA-256，不提供任意 locator 输入。

Agent 来源的扫描条目、样本和任务 content 支持单区间 `bytes=start-end`、`bytes=start-` 和 `bytes=-suffix`，返回206及准确 Content-Range；须先完整捕获并核验同一张原图，范围请求不减少这一步成本。`If-Range` 精确匹配 `"sha256:<SHA>"` 才使用区间，否则返回完整200。越界或多区间返回416；同内容的已验证完整缓存同样支持。非 Agent 图片端点在读取来源／缓存前拒绝 Range，预览端点仍不支持；不提供 HEAD 或304快捷返回。

图片上限为每张64MiB、3200万像素，响应30秒。托管原件的完整字节读取使用独立暂存层，物理读取预算10秒；图片解码在同一文件上执行，单独检查10秒预算并等待最多15秒。引用和目录快照沿用读取循环10秒、等待15秒的预算。阻塞I/O的实际收尾可能超过等待预算。

每个API进程的托管原件暂存与既有引用/目录图片快照各有4个名额，均持续到对应快照关闭；当前图片入口最多同时持有8份、合计512MiB临时字节。图片解码另有内存占用，多进程上限需相应累加。满额返回429。通用原件暂存层要求调用者显式提供大小和时间预算；此图片接口的限制不作为后续视频或点云读取的默认限制。请求快照预算与持久副本额度独立；持久缓存策略提供无持久缓存、仅预览和按需原图，由来源请求与位置上限共同约束。

超时或取消会设置停止标记；阻塞的内核 I/O 不能由线程强制终止，后台读取仍占名额，直到实际退出并清理。成功、失败、取消、发送异常和客户端断开都会关闭请求拥有的临时文件；清理不触碰源目录或托管原件。

| 状态      | 典型 code                                                                                                                                                                                                                                     |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 409       | `scan_incomplete`、`content_unverified`、`api_read_not_verified`、`source_changed`、`content_mismatch`、`configuration_changed`、`no_readable_original`、`original_relation_conflict`、`original_location_ambiguous`、`storage_original_busy` |
| 413       | `content_too_large`、`image_pixel_budget_exceeded`                                                                                                                                                                                            |
| 415       | `image_format_unsupported`、`invalid_image`                                                                                                                                                                                                   |
| 429       | `snapshot_capacity_exceeded`                                                                                                                                                                                                                  |
| 503 / 504 | `content_source_unavailable` / `content_read_timeout`                                                                                                                                                                                         |

认证使用 Bearer。浏览器应通过带认证头的请求取得 Blob，再管理 object URL 的取消与释放；不能直接把这些 API 路径赋给普通图片 src。原地引用数据集的管理预览与工作台已接入这条链路；object URL 按登录会话、任务和请求代次退休并回收。

## 点云质检的原件输入

创建仍使用 `POST /projects/{project_id}/point-cloud-quality/runs`，运行和问题查询路径保持不变。创建要求当前 `review.write` 能力及准确 Task 可见性；项目／Scene 范围仍限项目管理者，任务／标注范围允许审核员。此链路没有新增 API Key scope 要求：只有 `annotations:read` 的 Key 也可在满足上述项目职责和任务可见性的前提下发起检查，无需增加 `tasks:read` 或 `datasets:read`。

新运行在内部范围中声明 `original_contract=1`，为已有 PCD 输入固定原件 owner、内容 UUID、SHA-256、精确大小和逻辑位置 ID，并保存坐标约定。读取使用原件记录保存的位置，完整核对字节，不根据当前 datasets 桶、路径或历史 `content_hash` 猜测对象。问题证据中的 `pointcloud_original` 仅含安全身份收据；桶、端点、凭据和后台 Key 指纹不通过该证据返回。原 Key 指纹保存在后台任务的私有授权字段中，普通 AsyncJob 响应隐藏该字段；Key 轮换、scope 或期限改变会使原委派失效，后台不保存 Bearer。

单个 PCD 最多 256 MiB，独立两槽覆盖完整读取、PCD 解析与坐标归一化到真实文件关闭；复制 120 秒、传输 30 秒、解析与归一化 30 秒为合作式期限。取消／超时等待实际线程和句柄收尾，关闭未知时保留准入额度与维护许可，不能将期限理解为硬墙钟保证。每轮仍限 5,000 个 Task、20,000 条 3D 与相机标注；只支持 ASCII／非压缩 binary PCD，相机只参与标定和尺寸计算，不读取相机像素。

缺失、不支持或无法读取／解析的 PCD 在运行摘要记录明确跳过；权限／原 Key 失效、绑定或维护代次变化不能降级成普通跳过。全部物理收尾后再复核权限、Task 可见性、原件、坐标约定、完整来源与配置，并在同一事务提交本轮问题、Run／Job 结果及通知。取消或检查失败回滚本轮问题写入，保留既有人工复核。

旧 completed 结果保留历史读取；缺少原件合同的旧 pending／running 在派发或重放时标为 `stale`，不按当前 owner 补造身份或读取原件。旧原件相关问题缺少证据时也会标为过期，不参与当前规则治理样本；纯轨迹历史继续按其原证据判断。

## 已登记目录引用

Task DTO 新增 `storage_mode=managed|reference` 和可空 `content_sha256`。引用任务的 file_path 是平台内部引用标识，不是 S3 key，也不能拼接路径。已登记内容固定为 Item→Source/Run/Entry→SHA-256；来源改名或暂停后恢复不会改变内容身份，修改定位配置或停用位置/来源时拒绝读取；实际读取来源时还要求 API 锚点。

登记时要求当前 API read 探测；之后从来源捕获原图时，使用该节点已固定的锚点与实际完整读取作为当前可读证明。探测 TTL 过期本身不阻断已有引用，但不能自动绑定新根；缺失锚点或挂载变化仍失败。源位置未挂载到 API 时，不通过 worker 转发图片。跨节点最终以 SHA-256 核对内容，不跨节点比较 dev/inode。

普通手工标注、批量属性修改及任务生命周期写入在持有 Task 行锁后完整复核同一内容与当前权限，再沿用原事务；helper 不提交调用者状态。读取失败则整个标注写入失败、旧标注保留。此处为小目录切片接受了锁内有界 I/O 的代价，长锁最长受内容读取预算约束；目录扫描和独立 verify 仍在锁外读取。批次整批送审、未获专用资格的原生掩码写入和跨任务传播仍拒绝引用任务。具有可信内容溯源的普通几何预测可通过专用采纳资格检查，当前任务、原图、候选和权限须同时匹配。训练冻结、[后台图片导出](export.md)与[后台图片预标](predictions.md#原地引用图片)分别通过严格原图快照消费引用，不进入旧 S3 路径。

## 完整原图缓存的接入状态

统一引用 reader 支持 `none / preview / on_demand`；按 `none < preview < on_demand` 取来源请求和位置上限的较小值，按需原图包含预览能力。超级管理员在位置详情配置完整原图额度和 TTL；按需策略要求来源和位置同时允许，且位置显式配置正额度。首次实际读源通过完整校验后才尝试保留副本。填充失败、额度不足或没有可淘汰副本时仍可返回已经验证的请求快照；单文件大于总额度时跳过保留，不淘汰其他图片。缓存额度不能代替请求快照的并发与解码预算，满额仍返回429。

命中必须是同一 Asset/SHA 的完整副本，每次仍重新捕获、校验摘要并解码。任务可见性、活跃账号、API Key、来源/位置启用、接受修订与配置修订在 I/O 前后复核。此时实际读取目标是平台登记的缓存位置，无需来源目录挂载或来源节点锚点；缓存缺失、损坏或禁用后，回到来源读取时仍需原有物理资格。缩略图不会进入这个选择过程。

已缓存旧 Asset 可在同名来源文件被覆盖后继续读取自身旧字节，不自动接受新内容；新文件仍需扫描与明确接纳。来源停用、注销或策略收回后，不再通过旧缓存读取。质量检查继续读取来源实际字节，不将缓存命中报告为来源恢复。样本检查器与来源诊断分开显示来源观测、完整缓存登记和预览；缓存记录只说明已登记副本，实际读取仍复核字节、当前权限、修订与到期时间。来源未见或内容已变但有同 Asset 的合格旧副本时，可以尝试核验读取旧内容。缓存容量、配额/TTL和受控清理见[存储位置接口](storage-locations.md#完整原图缓存)。

## 外部 S3 引用

Source 可绑定外部 S3 连接器位置，Task/Item 的鉴权内容接口保持一致。回执保存精确 bucket/key、条件令牌及可用的固定版本，外部定位不暴露为可绕过授权的 URL。与本地目录不同，S3 首次核验不依赖同一内核主机或 inode；使用服务观测、条件完整读取和 SHA 建立身份。版本丢失、条件失败、响应身份或摘要不符均拒绝，不回退最新对象。固定版本仍存在时，最新版本改变不影响旧任务原件。

## SFTP 引用

部署合格的只读 SFTP jail 可使用相同 Item/Task 内容接口。每次捕获检查路径各级 lstat、打开句柄的 stat、读取前后观测与完整 SHA；不把 SFTP 的秒级 mtime 或大小当内容证明。旧扫描资格锚点必须与当前 API 和位置证据一致，不能通过换节点或更换主机公钥重绑旧引用。

SFTP 不提供本实现可依赖的原子 nofollow 或条件 GET；根边界由服务器 jail 保证。路径检查只拒绝观测到的链接，不能替代该部署条件。原图离线或变化时严格读与新增标注失败，已有索引、标注及获授权的独立预览保留；预览不使原图重新变为可标注。凭据仅在 I/O 线程解密，不写入内容回执。

## 媒体缓存短期读取

新注册位置的普通缩略图、图片金字塔和视频派生输出使用同源`GET /api/v1/media-cache/{kind}/{output_id}/{relative:path}?token=...`，同一路径也支持`HEAD`。`kind`仅为`thumbnail / image-pyramid / video-derivative`。链接由既有业务读取入口在权限和输出内容核验后签发；调用方使用返回的完整URL，不自行拼接对象键。旧schema1输出保留原链接合同；完整原图缓存不通过此接口公开。

短期token使用独立用途签名，绑定种类、输出UUID、准确相对路径、保存地址摘要、SHA与字节数及到期时间，最长一小时，沿用签发入口的现有期限。它不包含endpoint、连接密钥或登录凭据；持有链接即可在有效期内只读对应输出，请求不要求登录Bearer，也不能将其作为登录token。复制链接不会延长有效期。

读取重新核对保存的输出、内容清单和当前目标维护状态，使用保存地址及当前连接凭据。支持单段`Range`及`If-Range`：完整响应200、有效范围206，范围语法错误400、越界416；无效／过期token或不可读输出404，目标暂不可读503，传输超时504。响应为`Cache-Control: no-store`，实际远端读取、Body／client关闭及取消收尾有界管理；未知关闭保留访问责任。浏览器不直连外部目标，不依赖外部桶CORS，也不经过默认对象存储的`/minio`代理。

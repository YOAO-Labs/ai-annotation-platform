---
audience: [dev, project_admin, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-23
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

独立 verify 不创建 DatasetItem、Asset 或任务；需另外调用[登记接口](./dataset-sources#逐条登记原地图片)生成引用样本。已核验内容也不意味着原图以后一直在线；预览状态不由此次核验推导。

## 图片任务

| 方法 | 路径                                        | 行为                                                                                          |
| ---- | ------------------------------------------- | --------------------------------------------------------------------------------------------- |
| GET  | `/files/tasks/{task_id}/content-identity`   | 返回当前 verified Asset 的摘要、大小和 Asset ID；原图可用性仍为 `unchecked`，预览为 `unknown` |
| GET  | `/files/tasks/{task_id}/content?sha256=...` | 按固定托管位置或不可变目录引用和摘要读取完整图片快照                                          |

两个入口均要求 `tasks:read`，并复用当前项目权限、批次状态和任务分派规则。知道 Task/Asset ID 不代表有权限；任务之外的数据集或目录不会因此开放。读取后、响应前再次检查权限和绑定；API Key 的吊销、轮换、过期或 scope 收回同样使在途读取失败。未核验历史 Asset 返回 `content_unverified`，不把旧 hash hint 或 ETag 当 SHA-256。

托管任务支持固定平台 datasets 桶的 `managed_original / present` 副本，拒绝禁用或配置偏离的存储位置。不会用缩略图、其他桶或当前全局配置猜测原图。可覆盖 S3 key 也必须完整核对摘要，不能只因键名含 UUID 就直接相信其内容。

既有 `/files/tasks/{task_id}/file-url` 保留签名响应格式，并执行相同任务可见性检查；该旧地址仍有签名有效期窗口，不具备新快照接口的严格保证。托管任务继续使用已有工作台链路；原地引用任务的旧 file-url 返回409 `reference_requires_verified_content`，工作台必须使用严格内容接口。训练冻结、后台图片导出和后台图片预标通过各自严格快照合同消费引用，不使用旧签名地址。

## 响应、预算与清理

成功返回完整 `image/png`、`image/jpeg` 或 `image/webp`，含精确 Content-Length、`X-Content-SHA256`、基于摘要的 ETag、`Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`。仅支持单帧图片。请求必须带64位小写 SHA-256；不提供任意 locator 输入，Range 返回416。

固定上限：每张64MiB、3200万像素、读取循环10秒、请求等待15秒、响应30秒。每个 API 进程最多4个快照，名额持续到响应结束，临时文件字节上限合计256MiB；图片解码另有内存占用，多进程上限需相应累加。满额返回429。没有持久原图缓存、预览派生或缓存淘汰。

超时或取消会设置停止标记；阻塞的内核 I/O 不能由线程强制终止，后台读取仍占名额，直到实际退出并清理。成功、失败、取消、发送异常和客户端断开都会关闭请求拥有的临时文件；清理不触碰源目录或托管原件。

| 状态      | 典型 code                                                                                                                                               |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 409       | `scan_incomplete`、`content_unverified`、`api_read_not_verified`、`source_changed`、`content_mismatch`、`configuration_changed`、`no_readable_original` |
| 413       | `content_too_large`、`image_pixel_budget_exceeded`                                                                                                      |
| 415       | `image_format_unsupported`、`invalid_image`                                                                                                             |
| 429       | `snapshot_capacity_exceeded`                                                                                                                            |
| 503 / 504 | `content_source_unavailable` / `content_read_timeout`                                                                                                   |

认证使用 Bearer。浏览器应通过带认证头的请求取得 Blob，再管理 object URL 的取消与释放；不能直接把这些 API 路径赋给普通图片 src。原地引用数据集的管理预览与工作台已接入这条链路；object URL 按登录会话、任务和请求代次退休并回收。

## 已登记目录引用

Task DTO 新增 `storage_mode=managed|reference` 和可空 `content_sha256`。引用任务的 file_path 是平台内部引用标识，不是 S3 key，也不能拼接路径。已登记内容固定为 Item→Source/Run/Entry→SHA-256；来源改名或暂停后恢复不会改变内容身份，修改定位配置、停用位置/来源或丢失 API 锚点时拒绝读取。

登记时要求当前 API read 探测；之后每次业务读取使用该节点已固定的锚点与实际完整读取作为当前可读证明。探测 TTL 过期本身不阻断已有引用，但不能自动绑定新根；缺失锚点或挂载变化仍失败。源位置未挂载到 API 时，不通过 worker 转发图片。跨节点最终以 SHA-256 核对内容，不跨节点比较 dev/inode。

普通手工标注、批量属性修改及任务生命周期写入在持有 Task 行锁后完整复核同一内容与当前权限，再沿用原事务；helper 不提交调用者状态。读取失败则整个标注写入失败、旧标注保留。此处为小目录切片接受了锁内有界 I/O 的代价，长锁最长受内容读取预算约束；目录扫描和独立 verify 仍在锁外读取。批次整批送审、原生掩码写入、跨任务传播和预测采纳仍拒绝引用任务。训练冻结、[后台图片导出](export.md)与[后台图片预标](predictions.md#原地引用图片)分别通过严格原图快照消费引用，不进入旧 S3 路径。

## 外部 S3 引用

Source 可绑定外部 S3 连接器位置，Task/Item 的鉴权内容接口保持一致。回执保存精确 bucket/key、条件令牌及可用的固定版本，外部定位不暴露为可绕过授权的 URL。与本地目录不同，S3 首次核验不依赖同一内核主机或 inode；使用服务观测、条件完整读取和 SHA 建立身份。版本丢失、条件失败、响应身份或摘要不符均拒绝，不回退最新对象。固定版本仍存在时，最新版本改变不影响旧任务原件。

## SFTP 引用

部署合格的只读 SFTP jail 可使用相同 Item/Task 内容接口。每次捕获检查路径各级 lstat、打开句柄的 stat、读取前后观测与完整 SHA；不把 SFTP 的秒级 mtime 或大小当内容证明。旧扫描资格锚点必须与当前 API 和位置证据一致，不能通过换节点或更换主机公钥重绑旧引用。

SFTP 不提供本实现可依赖的原子 nofollow 或条件 GET；根边界由服务器 jail 保证。路径检查只拒绝观测到的链接，不能替代该部署条件。原图离线或变化时严格读与新增标注失败，已有索引、标注及获授权的独立预览保留；预览不使原图重新变为可标注。凭据仅在 I/O 线程解密，不写入内容回执。

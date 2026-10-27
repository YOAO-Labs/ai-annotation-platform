---
title: 数据集、目录来源与增量同步架构
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 数据集、目录来源与增量同步架构

本页解释平台的数据集内容身份模型（`Dataset` / `DatasetItem` / `DatasetAsset` / `DatasetAssetLocation` / `DatasetReference`）、受控本地目录与外部 S3 / SFTP 持续原地引用、可恢复目录扫描与批量预检登记、增量同步状态机、数据集工作台聚合查询，以及可恢复的图片质量检测与批量复核。

- 连接器凭据、受控存储位置注册与九用途映射见 [存储位置、连接器与用途绑定](./storage-connections)
- 统一原件读取、按需原图缓存、条件创建与保留回收见 [原件验证读取、缓存与保留回收架构](./original-access-and-retention)
- 对象存储迁移、跨网络只读站点 Agent 与容量观测见 [对象存储迁移、站点 Agent 与容量观测架构](./storage-migration-and-ops)
- 面向使用者的指南见 [数据集与存储总览](/user-guide/datasets/)、[数据来源与增量同步](/user-guide/datasets/sources)、[数据集工作台与视图](/user-guide/datasets/workbench) 与 [图片质量检测与复核](/user-guide/datasets/quality)

## 内容身份与兼容回填

外部连接器负责复制导入；平台对象按保存位置使用默认 `StorageService` 或受控 S3 连接的当前凭据执行 I/O。内部 `storage_locations` / `storage_bindings` 记录默认平台位置和九用途的桶，不复制连接器密码。目前头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件开放受控的新写入位置变更。配置与固定记录不符时登记会失败；用途新分配切换不搬迁历史对象；改变已有对象的物理地址须独立完成定位、迁移与对账。

每个 `DatasetItem` 可反向关联一份 `DatasetAsset` 内容修订及 `DatasetAssetLocation`。新上传、ZIP 与连接器复制使用独立上传 UUID 的稳定托管键，新 Item 在 `metadata.storage_relative_path` 保存逻辑相对路径，供 Scene、点云帧分组和导出复用；扫描保留真实键并单独分配重名后缀。托管根下 `_objects/<upload UUID>/` 为平台写入保留区，手工导入目录应放在该保留区以外，普通 UUID 场景目录完整保留。已核验内容以 SHA-256 和大小固定，数据库拒绝修改；派生图片继续由 `ImagePyramidAsset`/generation 管理。

回填只根据实际 `file_path` 增加管理映射。历史 `content_hash` 保留为不可信 hint，不能根据长度当作 MD5/SHA-256，更不能视作 ETag。未读取的旧原件和上传占位仍为 `unverified`；成功报告表示映射完整，不能证明对象存在或历史内容一致。Item/Task 路径、`Item.updated_at`、项目关联与训练冻结 JSON 不变。

在已经选择正确数据库和存储配置的 API 环境内运行：

```bash
cd apps/api
.venv/bin/python scripts/backfill_dataset_assets.py --dataset-id <UUID> --dry-run
.venv/bin/python scripts/backfill_dataset_assets.py --dataset-id <UUID> --apply --batch-size 100
.venv/bin/python scripts/backfill_dataset_assets.py --dataset-id <UUID> --verify
```

全量目标用 `--all-datasets`。`dry-run` / `verify` 使用数据库 `READ ONLY`，并核对默认位置配置和全部九用途；缺失 bootstrap 也计入配置错误。`apply` 按最多 500 条的批次提交，JSONL 只含 ID/计数/错误码。中断后可原命令重跑；`--after <item UUID>` 从已报告游标后继续，最终须不带游标全量 `verify`，覆盖恢复期间新增的样本。

退出码：`0` 表示该模式检查无待处理错误；`1` 表示配置/定位不一致或 `verify` 尚有待映射项；`2` 表示运行异常，已提交批次保留。`dry-run` 的 pending 数允许非零；映射工具不执行字节校验。应用回退保留 Alembic 0194；仅尚无内容身份和用途绑定的未使用 schema 可 downgrade，已使用则明确拒绝。

### 显式补算旧托管原件

`scripts/verify_legacy_dataset_assets.py` 在定位回填后建立当前原件的 SHA-256 基线。它不证明当前字节与历史标注时相同；历史摘要未知时，应先由数据负责人核对原件，再决定是否应用。工具不会搬移、覆盖或删除原件，也不更改样本、任务、标注、项目关联或历史冻结快照。

在选定数据库与平台存储配置的 API 环境内执行：

```bash
cd apps/api
.venv/bin/python scripts/verify_legacy_dataset_assets.py \
  --preview --dataset-id <UUID> --batch-size 100 --output /secure/legacy-plan.jsonl
# 核对清单中的 Item、Asset、SHA-256 和大小后，按同一清单应用
.venv/bin/python scripts/verify_legacy_dataset_assets.py \
  --apply-plan /secure/legacy-plan.jsonl
```

也可用 `--item-id <UUID>` 选单项，或显式 `--all-datasets`。批次最多 500 项，按 Item UUID 分页；预览的 `--after <UUID>` 可缩小续查范围。清单以 `0600` 权限独占创建，不覆盖旧文件，仅含业务 ID、SHA、大小和不透明指纹，不含物理定位或凭据；应按内部数据保管。预览不发布内容身份，但会写入并收尾访问台账，因此不是数据库 `READ ONLY` 模式。

预览及应用分别读取完整原件：单项最多 5 GiB，按分块计算 SHA，读取预算 60 秒，客户端单次传输等待 15 秒；超时／取消等待物理读取真实结束，不提交该项。HEAD、条件 GET、完整长度及末次当前 HEAD 必须一致，条件读取不受支持时拒绝。应用前先验证并冻结整份清单；缺失尾记录、校验和不符或非法条目均在首项发布前拒绝。随后逐项重读并比较 SHA／大小、Item／Asset 身份、精确定位、配置、生命周期与维护修订；有变化须重新预览，不按当前桶补猜路径。

应用只把原有 `unverified` Asset 及其唯一旧托管副本发布为 `verified`／`present`，同事务写入 `dataset_content.verify`，目的为 `legacy_current_baseline`。已核验内容不能通过此工具重绑定；重放同一清单返回 `already_verified`，这仅表示身份已发布，不表示再次检查了当前原件。批量预览跳过已核验项，外部引用不适用。未映射项先执行定位回填；原件缺失、旧大小不符、配置变化或定位歧义保持未知状态并报告。

退出码 `0` 表示所选清单无失败；`1` 表示逐项失败或原预览中仍有失败项，成功项已经单独提交；`2` 表示清单／运行异常。中断或锁竞争后可重跑完整清单，已成功项幂等跳过；未完成的预览文件不能直接应用，应生成新文件。清单 SHA 用于发现文件变化，不是历史内容证明或签名授权。

设计决策见 [ADR 0091](/dev/adr/archive/0091-dataset-content-and-location-identity)，接口见[数据集上传与扫描](/api/guides/datasets)。

## 受控文件系统原语与节点探测

中心本地受控目录与跨网络站点 Agent 共用无外部依赖的标准库包 `apps/_shared/filesystem_read`（`aap_filesystem_read`）：

- **显式 `RootSpec` 与 `openat2` 沙箱**：来源根通过不可变 `RootSpec(root_handle, absolute_root, relative_base="", binding_revision=1)` 传入。安全打开仅支持 Linux 5.6+（x86_64 / aarch64）的 `openat2`（`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV`），不支持时返回 `safe_open_unsupported`，绝不回退普通 `open`。
- **目录与文件锚点**：`capture_anchor`、`scan_directory`、`browse_directory`、`probe_directory` 与 `content_file` 在读取前后复核文件及来源目录的 `dev/inode` 与 boot identity；单目录扫描上限 5,000 项 / 2 秒。`stage_content` 在私有 `0600` unlinked 临时文件上完整读取并核对 SHA-256（单次上限 64 MiB / 10 秒），物理句柄关闭不确定时标记 `storage_operation_unsettled`。
- **指定节点 Celery 队列**：API 与各 Worker 分别加载 `STORAGE_FILESYSTEM_ROOTS`，通过 Celery `worker_direct` 队列结合实际 hostname 校验将探测、扫描和原地引用读取路由至挂载了对应目录的精确节点。

## 持久目录扫描基座

`DatasetSource` 以 `inventory` 模式绑定已有数据集、`filesystem` / `s3` / `sftp` / `agent` 位置、相对目录、固定 worker hostname 和规则；这是观测合同，不是可标注样本。API 当前限超管，通用 Job 列表/详情也拒绝降权账号查看扫描任务。每批及 claim 重验操作者、来源/位置修订与启停，启动、恢复和 claim 要求当前节点 `list` 验证有效。

`DatasetScanRun` 独立持有快照、代次、60 秒租约与随机 token；`DatasetScanEntry` 同时保存目录队列和文件观测。以 `(run_id, path_digest)` 唯一，路径本身用非索引 `TEXT`。每次完整目录捕获以一个事务分块写入，目录标记与观测一起提交。恢复和收尾复核目录摘要，`stat` 不提升内容 `verified`，也不判 `missing`。

统一短锁顺序为 `Location → Source → Job → Run`，与 Job retention 的 `Job → Run` 外键清理一致；文件系统 I/O 在事务提交后执行。来源行锁加活动运行部分唯一索引防并发启动，任务只 claim 一次；失租后显式 resume 创建新 Job/`task_id`。每批核对 task ID、来源代次、generation、token、有效期与授权；扫描 signal 在通用 fallback 前分派到同一 owner，旧信号不影响新尝试。

Job 外键 `SET NULL`，数据集删除只清空来源关联，长期观测账本保留；来源引用参与位置改向/删除保护。无来源数据时 0196 可降级，有数据则保留表并回退应用入口。接口与支持预算见[目录来源与观测扫描](/api/guides/dataset-sources)，决策见[ADR 0093](/dev/adr/archive/0093-resumable-directory-inventory)。

## 图片内容核验与请求快照

目录观测的 `stat` 不证明内容一致。`content_snapshot.py` 为目录及固定托管 S3 共用有界 `TemporaryFile`，完整 SHA-256/图片验证后才交付同一快照。`dataset_content.py` 处理真实当前权限、来源/位置修订、绑定与响应前复检，I/O 在数据库锁外。`ScanEntry.verified_content` 与原始观测独立，旧摘要不改写；首次核验只证明此次捕获字节。API 使用自身 `read` 探测和 anchor，不把 worker 的 `dev/inode` 当跨节点内容身份。扫描 claim 固定实际内核主机证据；首次核验必须与 API 同主机，再比对来源目录及文件 identity，未知/跨主机先拒绝；已核验摘要可跨节点验证相同字节。回执在数据库触发器中禁止改写。

现有任务内容身份与严格读取接口要求当前任务可见性，不开放整个来源或数据集。非 Agent 图片 content 拒绝 Range；Agent 图片可从完整核验快照交付单区间，完整缓存命中的范围能力随逻辑来源保留。详见[接口合同](/api/guides/verified-content)与 [ADR 0094](/dev/adr/archive/0094-verified-request-content-snapshots)。

## 可标注的目录引用与可恢复批量登记

### 可标注的目录引用

完整观测中的图片可通过 `register` 逐条登记。`DatasetReference` 固定 `Item → Source/Run/Entry`，`Asset` 固定已核验 SHA-256；同 `Source/path` 幂等，不创建 S3 原件副本。Item 与 Task 保存专用不透明引用标识，通用 S3 resolver 及未适配的后台消费者拒绝该标识。legacy 去重与 asset 回填排除引用项，不根据标识猜桶。

登记和项目关联共用 Dataset 锁；登记按扫描锁序取得来源后对 Dataset 使用 `NOWAIT`，繁忙返回 409。Link 在锁后查重，各建档批次在锁内再次查已有 Task。普通标注写入与单任务生命周期在 Task 锁内完成有界原图核验并保持原事务，避免等待锁期间来源变化仍被保存；快照 helper 不 commit。独立扫描与 verify 仍锁外读取。最初 API Key 身份、当前授权和绑定在 I/O 后复查。

业务从来源读取时使用当前 API 节点已固定锚点与实际完整读取，不因探测 TTL 到期自动重绑目录；缺失锚点、来源定位改变、停用或摘要不符会失败。来源改名、停用后恢复保留内容身份。首次登记仍需有效 `read` 探测。工作台按登录/任务/请求代次持有鉴权 Blob URL，原图未就绪禁用编辑；退休请求不能污染新任务或新账号，URL 按 owner 回收。应用回退保留 0198 及引用账本；空表才可降级。更多取舍见 [ADR 0095](/dev/adr/0095-immutable-directory-reference-tasks)，管理员流程见[服务器目录原地引用](/user-guide/datasets/import-images#服务器目录原地引用)。

### 可恢复的批量登记

扫描完成后创建独立 `DatasetRegistrationRun/Entry`，先预检全部匹配图片再显式确认登记；扫描状态、来源代次及核验回执均不充当登记进度。预检账本通过 `INSERT SELECT` 建立，逐项读取并提交，避免将全目录内容放入单次事务或内存。统计把索引、待核验、已核验、可用、异常和历史路径分开。

登记 Run 固定账号、初始 API Key 证明、配置修订和节点；每次执行有独立 task ID、generation、token 和 60 秒租约。单片 25 项/90 秒，未提交项可重放；旧 worker、重复消息及迟到失败信号不能改变新分片。派发先提交持久 `pending`，再发布消息；Beat 每 30 秒恢复待派发或到期租约。完成的 Job 可按既有策略清理，`Run/Entry` 保留，不把短期任务历史当恢复账本。

预检的 SHA-256 回执、正式登记业务对象和逐项检查点分别在各自事务提交。最终业务登记与该项计数共用事务；取消保留已提交部分，过期执行者不能在核验后继续落库。暂态数据集锁/快照容量不足退避重派；单文件错误隔离并允许显式重试，已核验摘要不可改写。确认固定预检尝试身份，I/O 期间出现新版预检时拒绝旧确认。

固定节点以锚点和逐项真实安全读取证明可读，探测 TTL 不终止已领取的长任务。API 确认同样重新读取固定摘要，不以扫描前几项损坏文件的探测结果否定后续正常候选。原图复制量与派生占用独立计量，目录支持 `reference/managed_copy` 与 `none/preview/on_demand` 的独立策略，默认 `reference/preview`；旧来源迁移为 `reference/none`。定位 `config` 与策略分列，Source 路径映射固定 `Item/Asset/SHA`；仅策略变化也要求新扫描和预检，旧引用内容身份不变。预览由独立记录固定父 `Asset/SHA`、规格、自身 SHA/字节和对象位置，走鉴权端点，不写 legacy `thumbnail_path` 或发签名 URL。来源停用/注销取消活动 lease，保留业务身份；托管复制原件已独立，继续可用。迁移 0199 增加可恢复账本；有历史记录时保留表，应用回退停止新尝试。取舍见 [ADR 0096](/dev/adr/0096-durable-directory-registration)。

### 目录管理的最小授权

`Dataset.created_by` 是目录数据集的最小管理边界：活跃超管管理全部，活跃项目管理员只管理自己的数据集，来源操作另叠加位置可见性。中央 `dataset_source_access` 服务为来源列表、total、`Scan/Registration/Entry` 直接 ID 访问及保留的 AsyncJob 尝试提供相同 SQL 关系。旧数据集写入口只对已含来源的数据集叠加创建者守卫，保留其他数据集行为。混合上传、ZIP、桶扫描、连接器复制导入和同步/异步分块建任务保留初始授权，耗时 I/O 后及分块提交前复查；撤权中断未提交部分，复制路径清理本项新对象，已提交部分保留。旧排队作业若后来接入目录而缺少该授权证明，则拒绝继续。作业公开 payload 隐去内部授权证明。

浏览、探测、内容核验、批量登记及扫描在 I/O 前后重读账号和具体来源权限；尝试固定初始 Key 证明，排队和执行期间的轮换、撤销、过期或 scope 收回阻止后续写入。位置启停与可见性分离，停用位置的历史原因仍可查询。位置授权用于管理，任务内容读取继续沿用分派/成员权限；撤回管理员使用权不撤销员工既有任务，停用来源/位置才会阻止其业务读图。更多取舍见 [ADR 0097](/dev/adr/0097-directory-ownership-previews-and-lifecycle)。

## 外部 S3 与 SFTP 持续引用

### 外部 S3 持续引用

严格引用使用 `sources/s3_reference.py`，旧 `sources/s3.py` 继续服务复制导入。`reference_storage.py` 解析现有连接器位置和加密凭据；`S3Access` 只在线程内解密、建立客户端，不进入 Celery 消息或持久快照。`Source/ScanEntry/Registration/Reference` 与 opaque `Item/Task` 身份复用已有模型，不新增平行资产表。S3 扫描在根 `ScanEntry.observed.checkpoint` 中逐页提交游标与结果，使用现有代次和租约；不套用 filesystem 的 inode 或整树二次目录摘要。

LIST 显式 URL 编码后严格解码一次，`ContinuationToken` 原样保存；完整 key 不裁剪、不折叠。HEAD/GET 使用原始 ETag 和固定版本，检查完整响应及 SHA；每次读都会验证错误条件被拒绝。读写权限及连接修订在 I/O 前后复查。既有 `Connection → Location` 更新锁序保留，读方在 Location 锁内只以 MVCC 读取 Connection，不等待反向 Connection 锁。SDK 内建超时、条件和版本参数负责协议访问；`before-send` 限定配置 endpoint，关闭代理避免中间层路径规范化。详见 [ADR 0098](/dev/adr/0098-strict-external-s3-references)。

### SFTP 持续引用

`sources/sftp_reference.py` 复用来源、扫描、登记和严格内容服务；旧 `sources/sftp.py` 保留复制导入调用者。部署者用 `STORAGE_SFTP_ROOTS` 声明已核验的服务器只读 jail，精确绑定 `host/port/username/完整主机公钥`；连接 `base_path` 必须为 `/`。整个账号可读命名空间就是位置授权范围，来源子目录仅作过滤。普通协议无法证明 jail，这项声明必须来自实际部署核验。

严格客户端使用独立 `HostKeys` 与 `RejectPolicy`，在认证前检查固定公钥，不加载系统 `known_hosts`；连接到白名单解析后固定的 socket 地址，防止客户端再做一次 DNS 解析。凭据在线程内解密，连接/认证/通道均设短超时。目录逐项有界枚举，复用 BFS 租约与末次清单核对；没有虚构持久 SFTP 游标、inode 或版本身份。

`lstat` 链、句柄 `stat`、读取前后观测与完整 SHA 负责内容一致性；服务端 jail 才是根边界，这些客户端检查不能实现原子 `nofollow`。资格摘要形成扫描锚点，历史引用必须匹配原锚点。资格变更先禁用位置并提交修订、阻止旧 worker 发布，再协调 API 与 worker；只改一个进程的环境不是集群撤权。不同资格应新建位置，旧引用不自动重绑。取舍见 [ADR 0099](/dev/adr/0099-qualified-sftp-references)，部署见[只读 SFTP 引用](/ops/deploy/docker-compose#只读-sftp-引用)。

## 增量同步、内容修订与周期协调

### 持续来源的手动增量比较

持续引用与连接器一次性复制导入共享连接配置，但保留独立的来源、扫描、预检及登记账本。预检在来源锁内从已接纳路径映射固定基线，完整读取内容后生成六类差异；上轮仅观测的条目不成为接纳基线。比较结果只有完整预检完成后才发布，取消或中断不推断缺失。

确认仅新增会固定选择范围，相同项跳过，变化和未见保留历史绑定；恢复不能扩大已确认范围。正式登记仍重新严格读取，并在同一事务建立样本/项目任务与登记结果。分页差异与短期 AsyncJob 历史独立保留；旧引用始终使用原摘要及资格；普通新扫描不重绑观测，只有显式同内容重新核验可以追加读取凭据。详见 [API 合同](/api/guides/dataset-sources#手动增量差异) 和 [ADR 0100](/dev/adr/0100-preflight-owned-source-differences)。

### 周期来源协调

`DatasetSourceSyncSchedule` 每来源一行，保存间隔、CAS 修订、原始授权证明、来源/位置修订及根锚点，以及当前 `Scan/Registration` 指针。既有 Beat 每 60 秒在 `default` 队列触发 tick，按 `next_check_at/source_id` 取最多 100 个到期来源；协调消息以 90 秒数据库令牌和修订固定，路由到精确 worker hostname。重复消息不能重复建运行，提交后派发丢失可由持久指针补发。

协调器按 `Location → Source → Schedule` 锁序检查权限、冲突与阶段，在 I/O 前释放事务；指定 worker 真实探测后重新取得锁、核对令牌和原授权。嵌套领域入口捕获的 `actor/Key/proof` 必须与计划一致，不把轮换后的凭据当成原授权。扫描和预检沿用自己的检查点、租约和分片；人工登记只观察终态，恢复由原登记 owner 处理，不由计划更换执行人。

默认策略下，完整差异除全部相同外进入待核对门禁。人工登记完成或显式跳过后按当前间隔安排下一轮；暂态失败按间隔退避，权限/配置/资格变化停用并阻断。暂停计划不撤销已创建业务任务，取消任务不被自动接续。历史作业可清理，领域账本和周期指针保留。迁移 0202 在已有计划时拒绝破坏性降级；应用回退需暂停计划，保留账本。

### 来源内容修订与路径头

`DatasetSourceItem` 是来源/路径的当前头；`DatasetSourceRevision` 保存不可变接纳历史，包含永久 `Item/Asset ID`、活动 FK、前驱、观测、SHA、确认预检及操作者。`Change.before_revision_id` 固定比较前驱，防止 SHA 往返造成 ABA；数据库保护修订事实与头绑定一致性。迁移回填旧头为修订 1，不补造历史接纳身份或时间。

单项新修订仅用于完整预检中的实际内容变化。沿用严格读取、Source 序列化与 Dataset `NOWAIT`，在一个事务创建新样本及空白任务、追加修订并推进头；Key 原证明与当前授权在 I/O 后和提交前复查。托管复制跳过业务 SHA 去重，引用保持各自 `ScanEntry`。历史媒体所有权与来源影响统计查询全部修订；管理预览仅服务当前 Asset。删除历史 Item 不回退头，不丢失原业务身份。

差异分类、原预检异常和仅新增确认范围不变；新修订接纳结果独立查询。周期核对门禁不因逐项接纳自动开放。应用回退保留 0203 与业务历史，禁止破坏性降级。详见 [ADR 0102](/dev/adr/0102-explicit-directory-content-revisions)。

### 同摘要观测重新核验

完整预检的 `observation_changed` 可明确接受为同 `Item/Asset` 的新 `SourceRevision`，继承 `owns_item`，不改业务身份、原 Reference、标注及 metadata。读取先验证原始绑定，再按同 `Source/Item/Asset` 选最新接纳观测；不按当前路径头或 SHA 搜索样本，不在失败时回退旧凭据。返回上下文包含选中修订 ID，I/O 后切换证据会被发现。资格锚点变化保持阻断。无需 schema 迁移；回退入口后仍需保留已接纳账本与其严格读取。见 [ADR 0103](/dev/adr/0103-explicit-same-content-revalidation)。

托管复制的同内容重新核验与定位迁移不分配或上传新原件，也不依赖当前原件目标的写入能力；仍执行来源快照、当前授权和前驱核对。只有接纳不同内容的新修订才强制分配独立原件。

### 显式定位迁移

同来源完整预检的 `not_seen → added` 配对必须由人选择、核验同 SHA 后接纳。`SourceRevision` 的前驱可以跨路径，路径内编号独立；`acceptance_order` 由数据库序列给出接纳顺序。跨路径前驱必须保留永久 `Item/Asset/SHA` 与所有权。原 Reference 不变，resolver 按同业务资产的最新接纳顺序取实际路径，不按时间或路径内编号猜测。

退役旧 `SourceItem` 与建立新头、目标 `RegistrationEntry` 消费、审计同事务提交；原差异保持冻结。旧预览退出当前读取，按保存归属和实际清理证据回收，新路径按当前策略生成派生物；退休记录不等同于物理字节已删除。旧路径回归是新增候选。迁移 0204 保留原账本事实，已有移动历史时拒绝降级到旧定位读取。见 [ADR 0104](/dev/adr/0104-explicit-source-relocations)。

### 纯新增自动同步与受审选择规则变更

周期计划显式持久化 `new_file_policy=manual|pure_additions` 与有效 IANA 显示时区（默认 `manual/UTC`）。纯新增策略固定在扫描快照并传给预检；整轮只含新增/相同且无错误才以真实 worker 节点走既有 `additions_only` 确认。任何其他差异整轮留在门禁，策略中途打开不会接管人工轮次。确认嵌套 I/O 后复核计划 `revision/token`、原账号与 Key proof，自动正式登记仅在原授权完全相符时由协调器恢复；人工正式登记仅观察。0205 降级前须暂停自动计划。固定分钟间隔不表达日历定时，时区仅控制显示。见 [ADR 0105](/dev/adr/0105-opt-in-pure-addition-sync)。

选择规则通过独立 `preview/apply` 入口更新，仅限 `recursive/include_globs/exclude_globs`。预览绑定提议规则、`Source/Location` 修订和排序接纳头修订身份，防止移动或重新核验后复用同计数旧预览；仅评估已接纳路径，不读取外部来源。保存拒绝活动尝试，原子递增来源修订与代次并阻断旧周期计划。观测账本保持；新差异按复用的后端选择语义识别规则外路径。业务 reader 只排除这三个字段进行定位比较，其他未知配置、原根/资格/回执与内容屏障继续严格校验。见 [ADR 0106](/dev/adr/0106-reviewed-source-selection-changes)。

### 同步导航与证据留存

来源历史通过数据库分页读取，最近完整扫描和活动扫描分别查询，不依赖最近 20 条。来源级活动登记独立于最新扫描。Web 深链保存同会话的 `dataset/source/scan` 上下文，再分别校验父子关系；直接来源查询支持第一页之外的来源。历史和停用来源禁用写操作，权限变更使旧会话视图退休。

位置摘要仅执行有界数据库查询：可见来源计数/分页、该位置全部可见来源的任务计数/分页。复用 `dataset_source_access` 的真实 `Run/FK` 或历史 payload 关联，不以提交者或 `payload.source_id` 授权；字符串比较避免把坏旧 ID 强转 UUID。最小 DTO 不返回 payload。位置可见性不能扩大数据集所有权。

领域账本持续保留，无自动压缩、过期删除或缺失等待期。每来源最多一个周期计划、活动扫描和活动登记；不设部署级计划总量上限，调度每次有界唤醒 100 个到期来源，扫描受原预算约束。运维应监控账本增长，不能把清理 AsyncJob 当作压缩领域事实。

托管训练冻结复用独立媒体与快照。构建提交应答异常时，清理前重新读取 `package_key`；已被版本引用或查询无法确认的 attempt ZIP 保留，避免删除实际已发布包。明确未发布的本尝试前缀才可清理。重扫、新修订、未见及取消不修改旧训练版本。外部引用训练冻结同样通过严格读取保留独立媒体和来源证据，已就绪版本不依赖外部原件继续在线。

## 位置来源诊断与已采纳源索引规模

### 位置来源诊断的只读口径

`storage_source_summary` 先按当前账号裁剪位置和来源，再对当前来源页批量汇总。`storage_source_diagnostics` 复用 Workbench 的 `observations`，显式限定 `DatasetReference.source_id`，避免将托管样本的多来源 provenance 任意归到某个来源。无 Dataset 时必须提供引用来源范围，禁止意外扩大为全局观测。

计数单位是保留的业务样本，含旧内容身份，非当前路径头；原件仍使用已有持久观测六态。预览通过当前 `SourceItem` 的业务 `Item/Asset/SHA` 联结，允许显式移动后的同一身份，拒绝旧样本继承新内容预览；当前策略禁用或来源停用优先阻断。它是生成记录摘要，不执行对象存在性检查。当前内容的合法缓存复用沿用现有读取合同，不增加仅诊断端生效的策略修订条件。

扫描和预检分别按来源批量查询，活动轮次优先，并独立保留最近完整成功记录。配置匹配由当前修订判断，不外露 snapshot。AsyncJob 保留期不删除领域证据。整个 GET 无源 I/O、写入或后台派发，不需要迁移或刷新 Worker。

### 当前已采纳源索引规模

`storage_source_usage` 在现有 `source-summary` 授权后，以 `source_scope_clause` 裁剪整个位置的当前 `DatasetSourceItem`。Head 与 current revision、该修订 `Run/Entry`、`Item/verified Asset` 的关系及 SHA／完整大小在数据库校验。`LEFT JOIN` 保留缺失关系；不要求当前头等于最初 `DatasetReference` 的 run，也不按当前配置修订重新解释冻结地址。`reference` 和 `managed_copy` 均属于来源规模，复制形成的平台对象仍独立计量。暂停／注销保留的头仍计量，已删除的头不从修订历史补造。

候选、去重和所有计数在同一 SQL statement 快照内聚合，应用只接收固定大小结果，不加载全部路径，也无文件或 provider I/O。来源页诊断仍仅处理当前页，usage 不受分页影响。观测范围取保存扫描的完成时间，计算时间使用 `statement_timestamp()`；这不是实时文件盘点。

- `filesystem`：单位置内以冻结 `host/root/base` 锚点和完整相对路径归组。host 是扫描时的 boot-id 摘要，不能用 `node_id` 代替；来源根 identity 不参与键，父子来源因而可合并。位置内存在多个无法比较的主机／挂载域时，整个范围进入未知；不同相对路径也不足以证明可以相加，不推断共享磁盘。
- `S3`：冻结 `endpoint/bucket`、准确完整 key 和明确记录的 `VersionId`；JSON `null` 与字符串 `null` 同为未版本化，缺字段或错误类型保持未知。保留重复分隔符、点段和编码字符，不调用文件系统规范化或二次解码。不同已采纳版本分别计量。
- `SFTP`：冻结 `root_handle/qualification` 和完整受控根内路径；校验收据 `protocol=sftp` 与准确远端路径。

同址任一关系或收据不可信即不报告完整；SHA／大小冲突排除整组，不选择最新或最大值。已确认小计与未知总量独立提供，只有完整空范围才返回总量零。去重限当前逻辑位置的已保存地址，不证明硬链接、跨位置别名或文件系统实际分配量。响应不外露内部路径、锚点、端点、桶、键或逐文件列表；Web 在已有摘要权限门禁内展示并复用刷新失败保留／撤权隐藏规则。

### 外部原件责任声明

`DatasetSource.external_protection` 保存责任方、恢复方式、内部参考编号和服务器登记主体／时间，`external_protection_revision` 从 0 开始独立递增。旧来源保持空声明；同一位置下不同来源可以有不同责任方。声明不放入 `source.config` 或位置配置，因此不会改变内容修订、规则证明、历史扫描、有效缓存或原件身份。

`dataset_source_protection.update` 复用当前来源权限、Key 复验和 `Location → Source` 锁顺序，仅比较声明自身的 `expected_revision`。它没有物理 I/O、在线探测、活动作业或数据集可写门槛；停用／注销和归档后的责任维护仍使用既有来源权限，孤立来源继续仅对超级管理员可见。登记主体为保留 UUID 事实，不以用户外键删除联动抹去责任。审计动作是 `dataset_source.protection_update`。

迁移 0244 仅增加两个字段及一致性约束，旧来源不自动补责任。非空声明拒绝破坏性降级；应用回退应保留新增列及记录。完整／保护数据库指纹均包含声明所有字段和修订，不属于物理位置重定位白名单。声明只表达责任与方案，不作为可恢复证明、备份保留要求或物理删除许可。

## 数据集工作台查询、个人视图与归档治理

### 工作台观测子查询与组合筛选

`services/dataset_workbench.py` 以统一的 `observations()` SQL 关系同時支撑样本筛选、分页列表与状态汇总，全程不发起物理源 I/O：

- **六态可用性推导**：在数据库内联合 `DatasetItem`、`DatasetAsset`、`DatasetReference`、`DatasetSource`、`StorageLocation`、完整预检差异（`DatasetSourceChange`）、按需原图缓存（`OriginalCacheEntry`）、显式同内容副本（`DatasetReferenceReplica`）与站点 Agent 状态，计算 `verified`、`missing`、`changed`、`preview_only`、`unavailable`、`unverified` 六种状态及允许操作列表（`allowed_actions`）。
- **多维组合筛选**：`item_query()` 支持按来源（`source_id`，需通过 `source_scope_clause` 校验可见性）、可用性状态（`state`）、文件名模糊搜索（`search`）、所属场景（`scene_id`）、媒体类型（`media_type`）、逻辑文件名后缀（`extension`，忽略不透明引用键，`jpg` 同时匹配 `jpg/jpeg`）以及宽高已知状态与数值区间（`dimension_state`、`min_width/max_width`、`min_height/max_height`）组合过滤。当前页通过 `MATERIALIZED CTE` 限制 `available_cache_ids` 与媒体序列化范围，避免全表水合。
- **关联项目上下文统计**：`workbench()` 按调用者对各关联项目的实际访问级别（特权管理者或 `annotator` / `reviewer` / `viewer` 成员角色）分组，复用 Data Manager 的 `apply_task_visibility` 在单条 `UNION ALL` 聚合 SQL 中统计当前用户可见的 `total_tasks`、`labeled_tasks`、`completed_tasks` 与 `review_tasks`。

### 个人保存视图（`DatasetSavedView`）

迁移 0206 新增 `dataset_saved_views` 表（路由 `/api/v1/datasets/{dataset_id}/saved-views`）：

- 视图按 `(dataset_id, owner_id, name)` 唯一，仅保存动态查询条件 JSON（`query` 与 `query_schema_version`），不持久化成员快照或授权凭据；单用户单数据集最多保存 100 个视图（在 Dataset 行锁下串行校验上限）。
- 创建与更新使用 `datasets:read` 读取权限，独立于数据集写权限或归档状态；若查询内包含 `source_id` 或 `scene_id`，保存时立即校验该来源对当前用户可见且场景属于当前数据集。

### 数据集归档与恢复

迁移 0214 为 `Dataset` 增加 `archived_at`、`archived_by` 与 `lifecycle_revision`，历史数据为 `active/1`。`dataset_lifecycle` 维护独立的预览/确认和共享写屏障，沿用当前创建者/超级管理员及 Key 权限；不通过停用 Source/Location 实现归档，避免破坏 Task 严格读取。单 SQL 汇总当前成员、主/次 Task 关联、活动 run/Job/同步及有持久数据集归属的冻结版本，并以有序关系指纹检测等量替换；历史冻结归属缺失保持未知。

归档只持有 `User → Key → Dataset NO KEY UPDATE NOWAIT`，写入状态/操作者/递增修订和审计，不反向等待 Location/Source/Task。来源执行遵循 `Location → Source → Dataset NOWAIT`；托管管理写入沿用 Dataset 先于 Item/Task 的串行化，写前重新核验当前状态与请求票据。Scene/标定同样保护 Dataset 结构，派生缓存的既有读取/清理仍由原 owner 执行。

托管接入和建任务授权、Scan/Registration 快照、Sync 委托及 Quality/Review 授权都捕获 `lifecycle_revision`，旧记录缺失按 1。最终提交前检查当前状态和捕获修订；归档再恢复不会复活旧排队工作、已完成观测的旧采纳许可或同步委托。新的显式请求可在当前权限下建立新轮；恢复不回放人员、位置、项目、Key 或来源启用状态。失败/取消收尾保留，避免生命周期变化阻止清理。

缩略图、视频元数据等既有派生媒体回填仍由原维护 owner 执行，不携带生命周期委托，也不因归档停用。训练新选择在合格样本集合上检查归档并保留生命周期票据，媒体核验后、发布版本前再次锁定复核；已排队或就绪的冻结版本使用自身不可变内容，不在历史媒体读取/worker 打包路径追加归档限制。Task 读图与标注继续遵守当前项目授权，个人阅读视图不是 Dataset 内容写入。

## 数据集质量证据与固定范围批量复核

质量运行使用 `dataset_quality_runs` 和 `dataset_quality_results`（迁移 0207）固定提交时的成员及 `Item/Asset/SHA/size`。已有 `verified` Asset 身份受保护，质量检测不升级旧哈希提示；未核验或非图片明确跳过。结果不依赖 Task 或训练冻结版本，不修改已有可用性观测。Item/Asset 删除后保留历史 UUID，当前查询计算 `stale`；AsyncJob 外键 `SET NULL`，通用任务清理不删除证据。

原图读取沿用受控 managed locator 或引用 receipt/anchor/来源授权，完整字节捕获与图片检查共用有界临时文件、并发槽、超时和清理。质量入口保留无法解码字节的完整 SHA 证明，严格图片响应入口仍拒绝这些字节。精确重复按本轮 `bytes_verified` 的 `SHA+size` 聚合；预算超限不报坏图，读取前后重新核查身份和权限。

Worker 以 `run generation`、`task/job ID`、租约共同 fencing，逐成员事务提交；每块最多 25 项或 60 秒。引用成员派到来源指定节点的 `direct` queue，托管成员进入 `media`；跨节点成员在块边界让出后续派发。120 秒租约过期由 30 秒 Beat 调度恢复，旧投递和旧失败信号不再有写权限。取消立即使 generation 失效，保留已提交证据；显式恢复创建新 AsyncJob，只消费 `pending` 成员。

人工决定与计算事实分离：成员保存当前决定、修订和操作者，每次变化追加到现有审计日志，乐观修订防止覆盖并发复核。检测与决定均不自动改变训练资格。回退迁移前应停止质量运行并导出证据；迁移只增加质量表，不迁移样本/来源或冻结数据。

批量复核另使用 `dataset_quality_review_batches` 和 `dataset_quality_review_members`（迁移 0208）保存不可扩张的预览范围与逐项 checkpoint，执行继续复用 AsyncJob 和单项复核 owner。筛选 SQL 与质量结果列表共用；预览固定规则、内容证据和 `review_revision`，摘要绑定决定和原因，确认不再查询动态范围。Result 锁使用 `populate_existing`，防止等待行锁后沿用会话缓存的旧修订。

每个成员的决定、审计及处理结果同事务提交。冲突/阻止明确保留，部分完成作为领域终态；取消后恢复只遍历 `pending`。批次锁、当前 `Job/task/generation` 和租约共同阻止迟到 Worker 或 failure signal 写入；Beat 每 30 秒恢复过期租约，派发失败保留持久意图。每项前后检查当前管理、来源与 Key 授权，无源文件 I/O；历史证据不继承到新内容。通用任务保留策略不删除复核账本。

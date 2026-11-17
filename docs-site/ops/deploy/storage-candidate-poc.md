---
title: 对象存储候选本地验证
audience: [ops, dev]
type: how-to
status: stable
last_reviewed: 2026-10-25
---

# 对象存储候选本地验证

`verify_storage_candidates.py` 在独立短期容器中顺序比较锁定的 SeaweedFS 与 RustFS 构建。它使用相同夹具、boto3、当前应用的 `StorageService` 和签名实现，实际验证浏览器、代理及单节点运行行为。命令不会读取部署的 `.env`，不会连接业务数据库、Redis 或现有对象存储，也不会修改应用、Compose、用途绑定或迁移位置。

本地比较可以带着兼容性失败完成。`execution_completed` 表示执行器完成了本片计划；`local_baseline_passed` 要求全部本地必测案例通过。完整选型与生产迁移仍未资格化，`selection_ready` 和 `production_ready` 始终为 `false`。

## 运行与清理

使用当前 checkout 已安装依赖的 API 虚拟环境，以及 Docker、`ffmpeg` 和已安装的 `agent-browser`。运行前按仓库浏览器技能读取 `agent-browser skills get core`；执行器会创建自己的浏览器 session。无需安装软件或启动应用开发栈。镜像只按固定 digest 和 `linux/amd64` 使用；先缓存锁文件列出的三个镜像，再运行验证。

```sh
# 对锁文件中的每个 candidate.image 和 proxy.image 执行：
# docker pull --platform linux/amd64 '<完整锁定digest>'

mkdir -m 700 /private/storage-poc
PYTHONDONTWRITEBYTECODE=1 apps/api/.venv/bin/python \
  scripts/verify_storage_candidates.py run \
  --manifest /private/storage-poc/owner.json \
  --report /private/storage-poc/comparison.json \
  --concurrent-load-note '共享宿主机，记录本次已知并行负载'
```

路径必须尚不存在；报告与 manifest 使用 `0600`。manifest 在创建资源前保存 nonce、checkout 与创建意图，容器启动前保存精确 ID。凭据为每候选随机生成，只用于所属资源；临时凭据文件与原始数据在清理时删除。报告包含固定大小案例证据、脱敏错误码、源码指纹、镜像及二进制观测，不含预签名 URL、凭据或提供方错误正文。

命令退出时按精确 manifest 清理容器、网络、卷、浏览器 session 和私有暂存，保留固定镜像缓存及报告。正常信号中断也会执行清理。进程被硬杀后，从同一 checkout 显式恢复清理：

```sh
PYTHONDONTWRITEBYTECODE=1 apps/api/.venv/bin/python \
  scripts/verify_storage_candidates.py cleanup \
  --manifest /private/storage-poc/owner.json
```

清理逐项核对 checkout、精确 ID、创建记录、镜像及 owner label；其他使用者占用卷或网络时拒绝删除。不得用名称前缀批量删除资源。`cleanup_verified=true` 表示所有登记资源已确认不存在；中断清理可以重试同一 manifest。

退出码 `0` 表示双候选执行完成且资源清理通过，仍须读取案例状态与 `local_baseline_passed`；`1` 表示执行未完整完成，`2` 表示参数、所有权或执行器错误，`130` 表示信号中断。失败结果不会自动换候选或修改预期。

## 固定场景与预算

每候选容器及辅助容器上限为 4 CPU、8 GiB，不使用 GPU；顺序运行，每候选最多 256 个写入键、各方向对象正文最多 1 GiB、15 分钟协作时限。boto3 连接 3 秒、读取 15 秒、总尝试次数 1；启动与重启分别最多等待 120／60 秒。正文计量区分预留上传字节与实际读取，失败上传的预留仍计入预算。协议头不计入正文；直接 HTTP 拒绝响应的正文也计入下载预算，SDK 的元数据响应不计入对象正文。同提供方卷备份另限 2 GiB 归档。

固定矩阵覆盖以下内容，每项保留 `passed`、`failed`、`not_supported`、`not_run` 或 `inconclusive`，以及原始 E07 类别映射和具体阶段：

- PUT／GET／HEAD、空对象、中文与百分号原始键、内容元数据、分页与删除；字面点路径的寻址合同另有纯回归。
- 条件 GET／HEAD、Range 与响应长度；四路条件创建只允许一次成功。
- 旧版本固定读取、版本分页、删除标记及精确版本删除、暂停后的可变 null 槽。
- 两个至少 5 MiB 的 multipart part、分页、错误完成拒绝、同次上传正确重试及 abort 后不存在。
- 实际生成并解码的 PNG、短 MP4 与 ZIP 完整 SHA，内部／ML 签名及两条真实 Nginx 路由；ETag 单列，不作为完整内容证据。
- 原先有效签名真正过期、签名篡改和错误凭据拒绝；独立浏览器从同源相对路由与不同回环 Origin 完成 PUT、GET、Range、CORS 预检、暴露头和新签名重试。
- 当前应用生命周期方法的真实配置与回读，以及单独的 SSE-S3 请求探测；当前平台未请求 SSE，不引入外部 KMS。
- 已确认非空对象与不可变版本经实际重启、写入中强杀后保持；未确认 PUT 只能缺失或完整。停稳后同提供方数据卷备份并恢复到新卷，逐保存版本读回。
- 相同小图片与混合大小对象在单路／四路下的 PUT／GET 字节、延迟、HTTP 失败，以及候选主进程 CPU 和 RSS 样本。

绝对公共路由在签名前采用公共 Host，Nginx 剥离 `/absolute` 并保留 Host；相对 `/relative` 路由签内部 Host，Nginx 剥离前缀并恢复内部 Host。ML URL 使用实际 `ML_BACKEND_STORAGE_HOST` 选择 Host，不替换平台签名实现。

生命周期配置仅证明当前规则可配置：media-cache 的 `videos/` 为 30 天，export 为 7 天；持久 `playback/`、`thumbnails/`、头像和归档不进入这些清理范围。回读规则不能证明原生到期删除或持久对象长期保留。

## 构建事实与结果边界

固定输入为 `scripts/storage_s3_candidates.lock.json` 与两个原始 Apache-2.0 许可文件；工具按仓库根解析许可路径并验证完整 SHA，不对上游文件归一化。SeaweedFS 普通发行标签对应的常规镜像不存在，所选为包含 FoundationDB 适配器的变体；本地显式使用数据卷内 LevelDB，不配置独立 FoundationDB。OCI 源码声明、发行 tag 提交和实际二进制版本分别记录。RustFS 镜像缺少 OCI 源码 revision label；发行提交不等同二进制构建证明。许可输入对应锁定的源码声明，也不作为镜像二进制来源的独立证明。

原始矩阵的原生到期、真实部署 TLS／Origin、磁盘满、多节点故障、滚动升级与已有冻结业务消费者均明确 `not_run`。本片的生成媒体与旧对象版本测试不代表现有训练 manifest、原件、标签及打包消费者已经迁移。单节点同版本卷恢复不代表 MinIO 跨供应商磁盘格式迁移。

主机共享负载、进程 CPU／RSS 的采样范围和采样峰值限制随报告保留。延迟来自小型固定本地夹具，不是生产 SLO 或独立硬件跑分。后续按 E07 原始矩阵补齐证据并形成供应商选择 ADR；本地比较不增加初次复制或 E12 的前置条件。

## 本地实测记录

2026-10-25，同一锁文件及测试器修订顺序执行完成，50 个候选案例记录中 37 项通过、1 项失败、12 项未执行。SeaweedFS 本地基线通过；RustFS 的显式 SSE-S3 探测返回 `InvalidRequest`，本地基线未通过。当前平台未请求 SSE；此结果不扩大为任何供应商全部加密能力的判断。两者的完整选型与生产资格仍为 `false`。

| 案例                       | SeaweedFS | RustFS  |
| -------------------------- | --------- | ------- |
| `basic`                    | passed    | passed  |
| `prefix_pagination`        | passed    | passed  |
| `conditional_reads`        | passed    | passed  |
| `conditional_create`       | passed    | passed  |
| `versions`                 | passed    | passed  |
| `mutable_null`             | passed    | passed  |
| `multipart`                | passed    | passed  |
| `multipart_abort`          | passed    | passed  |
| `media`                    | passed    | passed  |
| `signatures`               | passed    | passed  |
| `signature_rejections`     | passed    | passed  |
| `browser_same_origin`      | passed    | passed  |
| `browser_cross_origin`     | passed    | passed  |
| `lifecycle_configuration`  | passed    | passed  |
| `encryption_probe`         | passed    | failed  |
| `restart`                  | passed    | passed  |
| `kill_during_write`        | passed    | passed  |
| `volume_restore`           | passed    | passed  |
| `performance`              | passed    | passed  |
| `native_expiry`            | not_run   | not_run |
| `real_tls`                 | not_run   | not_run |
| `disk_full`                | not_run   | not_run |
| `multinode_failure`        | not_run   | not_run |
| `rolling_upgrade`          | not_run   | not_run |
| `existing_frozen_business` | not_run   | not_run |

两者均确认 6 项正文与元数据（含 2 个不可变旧／新版本）经重启、写入中强杀及新卷恢复保持，未确认 PUT 均不存在。备份归档分别为 15,038,464／16,241,152 字节。四个独立浏览器场景均完成预签名上传、完整摘要、206 Range、响应头暴露、403 拒绝和新签名重试；新增页面异常均为零。

每个性能单元使用相同的 8 个对象、2,752,792 字节，HTTP 失败均为零。下表为每请求的上中位延迟／p95，单位毫秒；样本只有 8 项，偶数样本的上中位数使用排序后的较高中央值。

| 操作／并发 | SeaweedFS     | RustFS         |
| ---------- | ------------- | -------------- |
| PUT／1     | 8.266／27.026 | 56.19／83.178  |
| GET／1     | 3.789／11.085 | 4.135／8.027   |
| PUT／4     | 14.688／33.87 | 65.883／72.625 |
| GET／4     | 8.56／14.931  | 8.389／17.865  |

SeaweedFS 的 31 个主进程样本覆盖 0.183 秒，RSS 采样峰值 294,170,624 字节，CPU 增量 0.16 秒；RustFS 的 127 个样本覆盖 0.694 秒，峰值 232,919,040 字节，CPU 增量 0.24 秒。宿主机同时有冻结 C6 测量及其他工作，文档构建也与部分执行重叠；这些短窗口不支持生产容量、稳定峰值或性能优劣结论。

## Native 输出目标的单独资格

既有候选 PoC 不证明 Native 回收所需的有界 `ListMultipartUploads`、重启后完整枚举、条件删除或受限 IAM。新增的前置入口复用锁定 SeaweedFS 镜像与准确 owner 资源协议，只核对 Native 保存输出目标，不替换日常 MinIO、原始数据或既有清理目标：

```bash
apps/api/.venv/bin/python scripts/verify_native_storage_contract.py \
  --manifest /tmp/native-storage-owner.json \
  --report /tmp/native-storage-contract.json
```

入口核对真实条件生产、HEAD/GET/DELETE、无请求头短期签名读取、原始 ETag/版本回执、准确版本删除与外来对象保护；用 122 个上传跨越 100 项页边界，其中同 key 的多个上传跨页。重启准确自有容器后，必须完整枚举相同上传、从保存的双游标续页，并保留已上传分片、桶标签、版本状态和受限身份。身份使用实际 Native 策略，核对策略回读、无额外组/策略/密钥、越界访问与 IAM 管理拒绝、禁用生效及准确移除。

入口只发布到准确自有 Docker bridge 的私有网关地址，不绑定所有网卡；真实有限签名请求也走该地址。策略指纹、固定镜像和发布方式随报告保留。版本探测用 tmpfs 覆盖镜像声明的数据卷，启动后核对所有本批容器的实际卷挂载均属于准确登记的数据卷，拒绝未登记的匿名卷。

隔离 API 的绑定探测身份仅允许目标桶 `.probe-media-cache/` 下的对象读写删除，以及带该前缀条件的 LIST，不具有 Native 输出权限。前置入口直接执行 `media_cache_storage_target.probe_target`，而非模拟其请求：核对真实条件写入、完整读取、前缀 LIST、准确删除，以及桶根/Native 前缀枚举、Native 对象读删和 multipart 操作拒绝，再准确移除该身份。恢复 A 绑定仍走真实 preview/apply 和同一存储探针，不以直接改库代替。

该入口只证明报告中列出的 Native 目标合同，不证明通用 S3 兼容、生产容量、完整 Native 生命周期或浏览器 8K。SeaweedFS 启动参数显式开放仅由管理身份使用的 IAM 写入，同时要求普通身份的自助密钥管理也必须另获授权；不会向运行进程发放 root。成功后退休该批自有资源；失败则保留准确 owner 与私有证据，另获该批退休批准后再清理，不复用失败环境。

固定 SeaweedFS 镜像已通过 Native 目标的条件 I/O、有界分页及有限权限合同：内建桶的 122 个上传以 `100 + 22` 分页，重启后完整枚举与保存双游标续页一致；连接桶另有一个独立上传。条件读写删除、准确旧版本删除后保留当前版本、短期签名读取、Native 身份的显式 multipart 拒绝、身份持久化和准确移除均通过。当前入口还通过实际媒体缓存绑定探针及其越权拒绝、全部实际卷挂载登记和正常退役；历史未登记卷的归属限制仍单列于运行账本。该结论要求使用这里核验的有限策略，尤其是显式拒绝 `AbortMultipartUpload` 与 `ListMultipartUploadParts`；缺少拒绝时，该构建会把 `PutObject` 扩展成额外 multipart 权限。普通 Native 整链已通过下述独立 profile；浏览器、通用 MinIO 替换和生产容量资格不能由此推断。

### 普通 Native 整链验收

模块数据默认资格把 `tests/test_native_pyramid_lifecycle_live.py` 放入独占的 `native-lifecycle` profile；定向选择该文件时不能混入其他测试。验证器先运行上面的存储合同，成功退休前置资源后，才创建整链使用的新 Native 提供方和 PostgreSQL。前置报告不会代替整链结果。

内置目标 A 不是“只改 Source 地址”：独立 pytest 进程在首次导入 API 配置前，读取绑定 SHA-256 的私有进程配置，使 API 配置、数据库保存位置和 Source 地址一致。登记目标 B 使用同一提供方上的另一自有桶及独立受限身份。真实 prepare 路径会 HEAD 原件，Images 控制端与 Worker 因而各使用提供方上的独立原件只读身份；它们的端点、桶和凭据在 A→B 切换期间保持不变。Cleanup 保留原临时 MinIO 配置。验证器以真实请求证明这些身份、Source 原件读取者和 API 绑定探测身份均不能读删 Native 对象；Native I/O 只能使用 Source 的有限授权，管理身份不进入消费者进程。

普通 MinIO 资源清单不登记这些同名、不同物理端点的桶。Native 提供方有单独的 owner manifest；测试管理读取先核对配置摘要、准确镜像、容器、网络成员、发布地址、数据卷及卷使用者，再核对桶 owner。私有配置不会交给 Images/Cleanup，也不会修改日常 `.env`、主机配置或现有 MinIO。

成功后分别退休普通环境和 Native 提供方；失败保留整批准确资源与私有诊断，必须另获该批退休批准。模块数据报告的 `cleanup` 同时记录 Native 前置报告、owner manifest 和退休状态，因此默认分区报告不会丢失独立提供方的清理证据。该 profile 证明普通 Native 生产存储端口、登记、独立读取、绑定切换与回收，不代替图片编码器、浏览器 8K、通用 S3 选型或生产容量验收。

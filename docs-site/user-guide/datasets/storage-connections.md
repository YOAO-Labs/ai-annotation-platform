---
audience: [project_admin, super_admin]
type: how-to
since: v0.11.16
status: stable
last_reviewed: 2026-09-10
---

# 存储连接器

> 适用角色：项目管理员 / 超级管理员

存储连接器让你把外部的对象存储（S3 / 阿里云 OSS / MinIO 等 S3 兼容服务）或 SFTP 服务器接入平台，之后在数据集里直接从这些来源批量导入文件，免去手动上传。

平台后端只内置两种连接器类型（`StorageKind`）：

- **`s3`**：AWS S3 及所有 S3 兼容服务，包括**阿里云 OSS、MinIO、腾讯云 COS** 等——它们都走同一个 `s3` 类型，只是填不同的 `endpoint`。
- **`sftp`**：SFTP / SSH 文件服务器。

> 界面上类型选项写作 **S3 / OSS**，对应后端的 `s3`；OSS / MinIO **没有**独立类型，统一用 `s3` 类型 + 各自的自定义 `endpoint`。

入口：左侧导航 **数据集** → 顶部 **数据连接器** 标签页。

<DocsVideo
  src="/media/datasets/storage-connector-create-test.mp4"
  poster="/media/datasets/storage-connector-create-test-poster.webp"
  alt="创建脱敏 S3 数据源，核对密钥已加密并通过真实连接测试返回样本数"
  caption="新建 S3 / OSS 连接器后，列表只显示“已加密”，不回显凭据；测试连接会真实列取目标前缀并显示抽样数。"
/>

## 新建连接器

1. 进入 **数据集 → 数据连接器**，点击右上角 **新建数据源**。
2. 填写信息：
   - **名称**：便于识别的名字，如 `aliyun-oss-prod`（必填）。
   - **类型**：`S3 / OSS` 或 `SFTP`（创建后**不可更改**）。
   - **范围**：
     - **个人（owner）**：仅你自己和超级管理员可见、可用。
     - **全局（global）**：所有用户可用，**仅超级管理员可创建**。
   - **连接参数与密钥**：按类型不同，见下面两个子节。
3. 点击 **新建连接器** 保存。

> **密钥安全**：Access/Secret key、密码、私钥、私钥口令均**加密落库**，列表、详情、任何接口都**不会回显明文**（`secret_set` 只表达「是否已配密钥」）。编辑时密钥字段**留空即保持原密钥不变**；填入新值则整体轮换。
>
> 创建（及编辑改了地址）时会先用超管配置的**主机白名单**校验目标地址，连不出去的连接器根本建不成功——详见 [主机白名单](#主机白名单超级管理员)。

### S3 / OSS / MinIO（`s3` 类型）

必填 `endpoint`、`bucket`、`access_key`、`secret_key`；可选 `region`、`base_prefix`（导入起始前缀）、是否 HTTPS（`use_ssl`）。

界面字段：**Endpoint**、**Bucket**、Region、Base prefix、**HTTPS** 复选框、**Access key**、**Secret key**。

完整配置示例（三份等价结构，差别只在 endpoint / region / 是否 HTTPS）：

```jsonc
// AWS S3
{
  "config": {
    "endpoint": "s3.ap-east-1.amazonaws.com",
    "bucket": "my-annotation-bucket",
    "region": "ap-east-1",
    "base_prefix": "raw/2026", // 可选：只看这个前缀下的对象
    "use_ssl": true, // HTTPS
  },
  "secret": { "access_key": "AKIA...", "secret_key": "********" },
}
```

```jsonc
// 阿里云 OSS（S3 兼容，填 OSS 的 region endpoint）
{
  "config": {
    "endpoint": "oss-cn-hangzhou.aliyuncs.com",
    "bucket": "annotation-oss-prod",
    "region": "oss-cn-hangzhou",
    "base_prefix": "",
    "use_ssl": true,
  },
  "secret": { "access_key": "LTAI...", "secret_key": "********" },
}
```

```jsonc
// MinIO（自建，常用明文 HTTP + 自定义端口）
{
  "config": {
    "endpoint": "minio.intranet.local:9000",
    "bucket": "datasets",
    "region": "us-east-1", // MinIO 无所谓，填默认即可
    "base_prefix": "incoming",
    "use_ssl": false, // 不勾 HTTPS
  },
  "secret": { "access_key": "minioadmin", "secret_key": "********" },
}
```

要点：

- `endpoint` 可带或不带 `http(s)://`；不带 scheme 时由 `use_ssl` 推断（勾 HTTPS → `https`，否则 `http`）。
- `base_prefix` 是连接器的「根」：之后导入填的 `source_path` 会拼在它**下面**，且不能逃出它。
- `region` 对 OSS / MinIO 通常不影响连通，留默认即可；AWS S3 建议填准以免签名/路由问题。

### SFTP（`sftp` 类型）

必填 `host`、`username`，以及一种凭据；可选 `port`（默认 **22**）、`base_path`、`auth_type`（`password` / `key`）。

界面字段：**Host**、Port（默认 22）、**Username**、Base path、**Auth**（Password / Private key）及对应凭据。

```jsonc
// 密码认证
{
  "config": {
    "host": "sftp.example.local",
    "port": 22,
    "username": "annotator",
    "base_path": "/incoming", // 可选：连接器的根目录
    "auth_type": "password",
  },
  "secret": { "password": "********" },
}
```

```jsonc
// 私钥认证（passphrase 可选）
{
  "config": {
    "host": "sftp.example.local",
    "port": 2222,
    "username": "annotator",
    "base_path": "/data/datasets",
    "auth_type": "key",
  },
  "secret": {
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
    "passphrase": "********", // 私钥无口令时省略
  },
}
```

`private_key` 把整段 PEM/OpenSSH 私钥文本贴进去（界面是多行文本框）。测试连接与实际导入使用同一解析路径，按顺序尝试 **RSA / Ed25519 / ECDSA** 三种当前支持的私钥类型。

#### SFTP 前置条件（务必先读）

连接器的“测试连接”和复制导入使用 `RejectPolicy` + 系统 **known_hosts**：

- 平台在 worker / API 进程内调 `load_system_host_keys()` 加载**服务端机器**的系统 known_hosts，并对未知主机一律 `RejectPolicy()`（直接拒绝，**不**自动接受、**不** TOFU）。
- **因此：目标 SFTP 主机的 host key 必须先出现在平台服务端的 known_hosts 里**，否则连接在握手阶段就被拒（报「Server ... not found in known_hosts」之类）。需要运维在部署平台的机器（API + Celery worker 容器/主机）上预先 `ssh-keyscan <host> >> ~/.ssh/known_hosts`，或挂载已含目标指纹的 known_hosts 文件。
- 连接测试与实际导入共享 RSA / Ed25519 / ECDSA 私钥解析和主机指纹校验。测试成功可确认连接器配置与目录读取能力；正式使用前仍建议跑一次小范围导入，验证文件范围与权限。

[SFTP 原地引用](./import-images#sftp-原地引用)使用独立的部署资格与固定主机公钥，不读取系统 known_hosts。运维须先核验整个账号命名空间的只读 jail，并配置 `STORAGE_SFTP_ROOTS`；连接 Base path 为 `/`。原地扫描的指定节点探测与连接器“测试连接”是不同检查，复制导入测试成功不代表取得持续引用资格。

## 测试连通性

连接器列表中每条都有 **测试** 按钮。点击后平台会用保存的凭据**真实建立一次连接**（出网前再次复检白名单 / SSRF）：

- **连接成功**：提示「连接成功」，并显示探测到的样本文件数（采样上限 20 条，仅用于确认能列目录，不是总文件数）。
- **连接失败**：提示原因，常见为凭据错误、地址不可达、SFTP 主机指纹不在 known_hosts、**目标不在白名单内**，或**白名单未配置**（见下）。

建议新建后先测试，再用于导入。

## 主机白名单（超级管理员）

出于安全（防 SSRF / 内网越权）考虑，连接器只能访问**白名单内**的主机。超级管理员在 **设置 → 系统设置 → 连接器主机白名单** 中管理条目；来源标记会区分部署默认和数据库覆盖，恢复部署默认只删除数据库覆盖，不修改服务器环境变量。

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/datasets/connector-allowlist.png — 超管连接器主机白名单配置面板；标注红框：白名单条目列表 + 添加输入框 [manual] -->

白名单在**连接器创建、测试、导入**三处全部强制校验（白名单可能在建好后被超管收紧，存量连接在测试 / 导入时会复检；DNS 会先解析为真实 IP 再判定，缓解 DNS rebinding）。两类典型报错要分清：

- **白名单未配置**（一条都没有）：create / test / import 三处**全部被拦截**，提示「**未配置连接器主机白名单，请联系超级管理员先配置允许的主机**」。这是「默认拒绝」语义——超管没显式放行任何主机前，谁都连不出去。
- **目标不在白名单内**：提示「**目标不在白名单内: \<host\> → \<ip\>（需超管将其加入连接器主机白名单）**」。

白名单条目可以是：

| 形态               | 示例                           | 含义               |
| ------------------ | ------------------------------ | ------------------ |
| CIDR 网段          | `10.0.3.0/24`                  | 内网服务器整段放行 |
| 单个 IP            | `192.168.1.50`                 | 精确放行一台       |
| 精确域名           | `oss-cn-hangzhou.aliyuncs.com` | 仅该域名           |
| 后缀域名（前导点） | `.aliyuncs.com`                | 匹配其所有子域     |

**永久硬拒绝**（即使写进白名单也连不出去）的地址类别共 **5** 类，命中即拒：**loopback（回环，如 127.0.0.1）/ link-local（含云元数据 169.254.169.254）/ multicast（组播）/ unspecified（未指定 0.0.0.0）/ reserved（保留段）**。worker 在容器内，访问宿主机请走 docker 网关 IP，不要也无法靠放行 loopback 绕过。

保存时会规范化并去重条目，URL、带端口地址、路径和 `*` 通配符不能保存。保存空名单前会二次确认，因为它会阻断所有连接器目标。

若运维显式配置了部署主机 SFTP 地址，超级管理员会在数据连接器页看到 **添加部署主机**。点击后只预填 SFTP、host 和端口 22；用户名、私钥或密码、路径仍需手工填写，目标也必须单独加入白名单。应使用只能读取导入 staging 目录的专用账号，不要输入 root 或日常部署账号密码。

## 从连接器导入数据集

> 完整向导。除「新建数据集」时可走连接器导入外，已存在的数据集也能随时再次从连接器补充导入。

1. 进入 **数据集 → 数据集管理**。在某个数据集的**展开行点击「上传」**打开导入向导（新建数据集时则在向导第 1 步「选择来源」处）。
2. 把来源切到 **连接器导入** 子面板。
3. **选择连接器**：下拉列出你可见的连接器。
   - 普通**项目管理员**：可用**全局连接器 + 本人创建的个人连接器**。
   - **超级管理员**：可用**全部**连接器。
   - 没有可见连接器时下拉显示「暂无连接器」，需先去 **数据连接器** 标签建一个。
4. 填写导入参数：
   - **Source path**（`source_path`）：相对连接器 base（S3 的 `base_prefix` / SFTP 的 `base_path`）的起始目录，**留空表示根**。不能含 `..`，也不能逃出 base。
   - **递归扫描**（`recursive`，默认开）：勾选则连同子目录一起扫描。
   - **Include globs**（`include_globs`，可选）：**逗号分隔**的通配模式，留空表示全要。
5. 点击 **开始导入**。请求会落到后端 `POST /datasets/{id}/import-from-connection`，返回一个后台任务（`job_id`）。
6. 导入在后台异步执行，向导第 3 步与右上角任务铃会显示**进度**以及**成功数 / 跳过数 / 失败明细**（失败逐条带文件名与原因；跳过通常是内容已存在的幂等去重）。

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/datasets/connector-import-step.png — 导入向导「连接器导入」子面板；标注红框：source_path 输入框 / 递归开关 / Include globs 输入框 [manual] -->

真实请求体（`DatasetImportFromConnectionRequest`，前端会把逗号分隔的 globs 拆成字符串数组）：

```jsonc
// POST /api/v1/datasets/{dataset_id}/import-from-connection
{
  "connection_id": "0b6f...uuid",
  "source_path": "scene_001/frames",
  "recursive": true,
  "include_globs": ["*.jpg", "*.png"],
}
// 202 → { "job_id": "..." }
```

### Include globs 怎么写

每个模式按 `fnmatch` 匹配，对**完整相对路径**或其 **basename** 任一命中即收录（所以 `*.jpg` 这种只写扩展名的会按 basename 匹配，无需带路径前缀）：

| 模式                   | 命中示例                           | 说明                       |
| ---------------------- | ---------------------------------- | -------------------------- |
| `*.jpg,*.png`          | `a/b/img.jpg`、`cover.png`         | 按 basename 匹配扩展名     |
| `scene_*/frames/*.jpg` | `scene_001/frames/0001.jpg`        | 按完整相对路径匹配多级目录 |
| `*.pcd,*.bin`          | `lidar/0001.pcd`、`lidar/0001.bin` | 点云 / 二进制帧            |

### 导入上限

单次连接器导入按文件数与总字节限制枚举范围，**枚举超限时整体失败，不产生本次部分导入**。代码默认值为：

- 最多 **50000** 个文件（部署变量 `DATASET_IMPORT_MAX_FILES`）。
- 总体积最多 **200 GiB**，即 `214748364800` bytes（部署变量 `DATASET_IMPORT_MAX_TOTAL_BYTES`）。

实际有效值由本部署默认与超级管理员在「设置 → 系统设置 → 数据导入」保存的覆盖决定。每次导入受理时固定两项预算；修改设置只影响之后新建的导入，已经排队、运行或重试的作业继续使用原预算。恰好等于预算仍可导入。这些预算只约束连接器枚举，不代表浏览器上传限额或实际传输字节上限。

超限时请用 `source_path` / `include_globs` 缩小范围，避免误扫整个存储桶。

> **保留目录结构**：导入按 `source_path` **内部**的子目录层级落库（源 `dataset-A/a/img.jpg` → 数据集内 `a/img.jpg`），不会多嵌套一层。后续源里新增子文件夹时，对同一数据集再次从同来源导入即可，已存在文件按内容**自动跳过**、只补新增。

## 编辑与删除

- **编辑**：可改名称或连接参数；**不填密钥则沿用原密钥**。改了地址会重新过白名单校验。
- **删除**：移除连接器。**已经导入到数据集的文件不受影响**（文件已落入平台存储），但此后无法再用该连接器导入。

只有连接器的**创建者或超级管理员**可以编辑 / 删除；**全局连接器仅超级管理员**可管理。

## 相关文档

- [图像数据集导入](/user-guide/datasets/import-images) — 多文件 / ZIP 上传方式
- [点云 / 多模态数据集导入格式](/user-guide/datasets/import-formats) — scene 目录布局
- 开发视角：[概念 · 存储连接器](/dev/concepts/storage-connections)

## 已挂载目录的存储位置

服务器目录、CephFS 和 NFS 可由运维统一挂载为少量受控根，再由超级管理员通过[存储位置 API](/api/guides/storage-locations)登记名称、根句柄、相对目录和使用人员。例如根句柄 `ceph` 对应 `/mnt/cephfs`，位置登记 `traffic`，目录浏览选择其下 `vehicle_detection`。新建根内目录和登记已有目录无需重启容器；新增未挂载的宿主机目录仍需运维先补挂载。

超级管理员可在存储管理页登记位置、配置使用范围和人员，并探测指定节点；获位置使用权的项目管理员也可探测，并为自己创建的数据集从数据集页[接入服务器目录](./import-images#服务器目录原地引用)，扫描并逐条核验登记。存储页的桶用量统计仍仅反映平台对象存储，不代表外部目录容量。现有连接器导入仍会复制文件。管理员不能仅凭 API 节点可读就认定扫描节点可用，必须探测指定 worker；外部目录不接受平台写入或删除。

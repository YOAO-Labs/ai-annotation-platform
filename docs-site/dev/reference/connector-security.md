---
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 连接器安全与部署配置

存储连接器的密钥加密、SSRF 白名单与导入护栏所依赖的部署配置。架构说明见 [概念 · 存储连接器](/dev/concepts/storage-connections)。环境变量完整清单见 [环境变量](/dev/reference/env-vars)。

## 配置项

| 环境变量                         | 默认值                    | 作用                                                                   |
| -------------------------------- | ------------------------- | ---------------------------------------------------------------------- |
| `CONNECTOR_ENCRYPTION_KEY`       | （空）                    | 连接器密钥加密用的 Fernet key。**留空则加解密拒绝，相关 API 返回 503** |
| `CONNECTOR_HOST_ALLOWLIST`       | （空）                    | 主机白名单的**部署默认值**；超管通过 API 写入数据库后由 DB 值覆盖      |
| `CONNECTOR_DEPLOYMENT_SFTP_HOST` | （空）                    | 部署主机 SFTP 快捷项预填地址；留空时不展示，不包含凭据或放行规则       |
| `DATASET_IMPORT_MAX_FILES`       | `50000`                   | 单次连接器导入最多扫描 / 导入的文件数，超限任务失败                    |
| `DATASET_IMPORT_MAX_TOTAL_BYTES` | `214748364800`（200 GiB） | 单次导入允许的总字节数                                                 |

## 密钥加密（CONNECTOR_ENCRYPTION_KEY）

连接器密钥用 [Fernet](https://cryptography.io/en/latest/fernet/) 对称加密后存入 `storage_connections.secret_enc`，与 JWT `secret_key` **隔离**（泄露面不互相放大）。

生成 key：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

注意事项：

- **必须在创建任何连接器前配置**。未配置时 `core/crypto.py` 抛 `ConnectorCryptoNotConfigured`，create / test / 导入端点返回 `503`。
- **不要轮换已有 key 而不迁移数据**：换 key 后旧 `secret_enc` 无法解密，`test` / 导入会报 `ConnectorCryptoError`（密文损坏或密钥不匹配）。轮换需重新录入各连接器的密钥。
- 生产环境从密钥管理 / `.env.production` 注入，不要提交进仓库。

## SSRF 白名单（CONNECTOR_HOST_ALLOWLIST）

白名单是 SSRF 防护的核心：每次创建 / 更新 / 测试 / 导入前，目标主机都必须命中白名单（`services/connector_guard.py`）。

### 取值来源与覆盖

白名单实际存储在系统设置 `system_settings.connector_host_allowlist`：

- 部署初值来自 env `CONNECTOR_HOST_ALLOWLIST`；
- 超管可在「设置 → 系统设置」查看当前来源并通过 `PUT /storage-connections/allowlist` 写入数据库，**DB 值覆盖 env**（运行期可改，无需重启）；
- 超管通过 `DELETE /storage-connections/allowlist` 删除数据库覆盖后，立即恢复 env 默认值。显式空数组是数据库覆盖，语义为全拒。

env 接受逗号分隔字符串或 JSON 数组：

```bash
CONNECTOR_HOST_ALLOWLIST=10.0.3.0/24,192.168.1.50,.aliyuncs.com
# 或
CONNECTOR_HOST_ALLOWLIST=["10.0.3.0/24", ".aliyuncs.com"]
```

### 条目形态

| 形态     | 示例                           | 含义                     |
| -------- | ------------------------------ | ------------------------ |
| CIDR     | `10.0.3.0/24`                  | IP 落在网段内放行        |
| 单 IP    | `192.168.1.50`                 | 精确 IP                  |
| 精确域名 | `oss-cn-hangzhou.aliyuncs.com` | 域名解析出的公网 IP 放行 |
| 后缀域名 | `.aliyuncs.com`                | 匹配任意子域             |

数据库覆盖最多 256 条。保存时会 trim、按首次出现顺序去重，并规范化 IP、CIDR 与 IDNA 域名；URL、端口、路径和 `*` 通配符会返回 `422`，不会落库。

### 始终拒绝（HARD_BLOCK）

无论白名单如何配置，目标解析出的 IP 若命中以下类别一律拒绝，防止打到内网 / 元数据：

- loopback（`127.0.0.1` / `::1`）
- link-local（`169.254.0.0/16`，含云元数据 `169.254.169.254`）
- multicast、unspecified（`0.0.0.0` / `::`）、reserved

> 空白名单 = 全拒。未配置任何条目时所有连接器目标都被拒绝。

### 本地开发提示

Docker 内访问宿主机 SFTP/MinIO 时，目标常是 Docker 网桥地址（如 `172.17.0.1`）。把对应 `/32` 加进白名单即可：

```bash
CONNECTOR_HOST_ALLOWLIST=172.17.0.1/32
```

## 部署主机 SFTP 快捷项

需要把本仓库部署服务器作为导入来源时，配置一个 API 与 worker 都可达的地址：

```bash
CONNECTOR_DEPLOYMENT_SFTP_HOST=deploy-sftp.internal
```

超级管理员随后可在数据连接器页点击「添加部署主机」，复用 SFTP 表单并预填地址与端口 22。该快捷项不会自动探测宿主地址、加入白名单或写入凭据；仍需显式配置白名单，并录入专用 SFTP 账号。推荐使用只能读取 staging 目录、最好禁用 shell 的账号和私钥认证，不要使用 root 或日常部署账号。

SFTP 测试与实际导入统一支持 RSA、Ed25519、ECDSA 私钥，并都使用 Paramiko `RejectPolicy`。目标主机指纹必须预先写入 `config/ssh/known_hosts`；生产 Compose 会把同一文件只读挂载到 API 与 worker，未知主机不会被自动信任。

## 导入护栏

`DATASET_IMPORT_MAX_FILES` 与 `DATASET_IMPORT_MAX_TOTAL_BYTES` 限制单次导入规模，避免误把整个存储桶拉进来。超限任务直接失败，应缩小 `source_path` 或用 `include_globs` 过滤。

## 相关文档

- [概念 · 存储连接器](/dev/concepts/storage-connections)
- [API · 存储连接器](/api/guides/storage-connections)
- [环境变量](/dev/reference/env-vars)
- [容器网络与 loopback](/dev/troubleshooting/container-networking)

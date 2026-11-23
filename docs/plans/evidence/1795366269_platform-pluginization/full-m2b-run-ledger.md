# M2b 全量部署收敛：运行证据账本

[进程清单与当前状态](../../../../docs-site/dev/concepts/module-deployment.md#deployment-convergence)是唯一拓扑入口。这里只记录运行范围和待补证据，不覆盖[Workspace 首切片](./first-slice-run-ledger.md#m2b-workspace)的冻结结果，也不授权 M2c 或发布。

## 本地基线回执

原始 JSON、收集清单与已有 JUnit 保留于本地 `.artifacts/test-results/full-m2b/`，不是随文档提交的制品。首轮探针在验收 checkout `ada675c5593694f0c2496e2f7b9d3c959fd20177`，使用隔离 PostgreSQL 从 `0267` 升级到 `bo0484`，不是后续 owner 授权与 Source 一起装配的最终 head。

| 回执／范围                                                                  | 已观察结果                                                                                                                                                                                                                                             | 不证明                                                                                                                                 |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/api/tests/test_hosted_settings.py`                                    | 20 项配置测试通过；检查旧绑定拒启及脱敏、签发与媒体密钥隔离、远程 publisher 与内嵌 publish 凭据互斥。                                                                                                                                                  | 未启动整套 Compose／工作树，也未执行 SQL 或业务。                                                                                      |
| `acl-three-before.json`、`acl-three-before.junit.xml`                       | `failed`，退出码 1，三个真实 LOGIN 全失败：`identity_embedded_api_consumer`（LAN 内嵌）172、`identity_api_consumer`（LAN 远程 issuer）171、`legacy_runtime`（工作树）172 个对象有有效权限。单个对象按关系计，不是 grant 数；JUnit 保存明细。           | 此探针当时扫描普通表、分区、物化视图、列与序列，未纳入外表，不是最终覆盖口径。两个 LAN profile 检查的是普通消费者，而非 issuer LOGIN。 |
| `inference-cold.json`、`inference-cold.junit.xml`                           | `passed`，3 项、零跳过：真实安装镜像的 Workspace 探测、健康观察与 Registry 同步；覆盖独立用途绑定、重启／回执或拒绝回退。镜像 ID `sha256:131d8dd0155620a8af9b048fe3cc6feb41b8374ec89aac88d76a3d13e7ef83f8`；回执记录临时 PostgreSQL 与本地资源已退役。 | 不是 GPU、端到端插件预标注、所有 split 或新 ACL 的资格。                                                                               |
| `labeling-class.json`、`labeling-class.junit.xml`                           | 基线 14 项中 7 项失败；HTTP 测试绑定因 `AsyncClient` 构造调用错误中止，不能填为已通过冷恢复。                                                                                                                                                          | 不将此失败解释为 owner 数据损坏，也不复用先前 Workspace 资格。                                                                         |
| `audit-compose.json`、`audit-compose-image-id.json`、`mask-gc-compose.json` | 保留失败回执；Audit 两次停于工件标识不符／缺失，Mask GC 独立领取进度断言失败。                                                                                                                                                                         | 发布的 Compose profile 或可构建镜像不是实际恢复资格。                                                                                  |

后续定向回执不覆盖上表的首次失败：

- **Mask GC 保留档**：`mask-gc-operator-cold.json` 与同名 JUnit 的两项用例均通过，零跳过。实际 Compose 中非 root GC 恢复原 claim，三轮删除 3／3／1 项，保留 1001 项；另验证一次性 operator 重启与权限拒绝。此内核不提供最终 cgroup `memory.peak`，故无实测峰值，不能据此通过独立容量基准。
- **Audit operator**：`audit-cold-source-receipt.json` 与同名 JUnit：1 项通过，零跳过。实际 Compose CLI 在上传后失去进程，继任者接管，整库还原后保留归档及分区退役事实；镜像 `sha256:e582ed36f86c0af42f9bec7cbb21ad911aaecd2b06063e8794561c51f37de0c3` 绑定 22 个源码输入。该轮早于新 owner 授权，不以其代替新账号恢复。
- **媒体探针**：`media-probe-cold.json` 记录发布用 Compose 的隔离项目及镜像，实际 `/health` 返回 `{"status":"ok"}`。这是探针进程冷启动，不是完整的视频／图片执行测试。
- **媒体控制、视频与图片执行器**：隔离 `bo0509` 数据库实装媒体窄授权，从 `docker-compose.assets-media.yml` 启动独立控制端和执行器。`media-a6cc142d3f-canonical-compose-gateway` 的视频用例用打包的 FFmpeg／ffprobe 生成并读回实际媒体；`media-2c4b684e59-canonical-image-restart` 的 libvips 用例生成缩略图和 1024×1024 金字塔。`media-c0f7a005ab-canonical-compose-policies` 中三个独立 worker 用例验证过期租约、来源替换及解除关联；各 JSON 和 JUnit 仍保留同批未通过的用例。这五个用例不证明远程 `media-probe`、响应丢失路径或整批通过。
- **Audit Summary 实验档**：`audit-summary-experiment-cold.json` 记录 `3d75a3d45` 上用发布的 `docker-compose.experiments.yml` 冷启 `audit-summary-experiment`；真实健康响应报告协议 1，`mixed.request.json` 的汇总响应与预期完全匹配。它不替代 Audit 核心写入与归档恢复测试。
- **新建数据库**：`schema-all-private-owner-502.json` 保留首次完整模块 owner 迁移；`native-schema-508-ddl.json` 在 `55e7cb4108` 上用全新 PostgreSQL 16 从空库迁至 `bo0508`，包括已登记 Source 的固定能力。`native-schema-508-ddl-reset-cli.json` 另验证普通连接不可达时，重置 CLI 只用显式迁移身份删除目标数据集和其中一项，并保留无关用户。两项均不是运行账号或连接器字节路径的证明。
- **带导入租约的最终迁移链**：`native-schema-509-lease.json` 记录 `4daf9631f1` 上全新 PostgreSQL 16 迁至 `bo0509`。`acl-509-owner-leases.json` 在独立的 `0267` 基线升级前后核对了 651 个冻结函数体，并通过临时存储预置；随后装配 owner 时发现已经删除的七参数 Labeling 函数，事务失败，未使用该回执声称授权或业务通过。
- **三个真实普通 LOGIN 和发布安装器**：`native-grants-509-learning.json` 在 `f3a12a5a83`、全新 PostgreSQL 16／`bo0509` 上实跑 `module-data-grants.sql`、两个 LAN profile 和工作树启动器。每种登录先获旧 `users.id` 列权限，再重放发布授权。逐个真实连接枚举私表、分区、外表、物化视图、列和序列，均为 **0**；显式读取 `public.users.id` 均返回 `42501`。工作树用启动器实际生成的 `…_test_app`，未用 `legacy_runtime` 冒充。Source 专用 LOGIN 通过 41 个固定能力的启动校验，租约 Importer installer 也成功；临时容器已删除。此证据不等于全部旧授权类型、业务或存储 I/O 已通过。
- **非空、剥离权限的整库恢复**：`native-grants-509-stripped-restore.json` 保留首次失败：旧枚举漏掉应用 schema，恢复后 PostgreSQL 默认的 PUBLIC `EXECUTE` 触发 cleanup 角色拒启。`native-grants-509-quarantined-restore.json` 在 `a25688abd6` 上用 `pg_dump -Fc`，另建无 PUBLIC 连接的新库，实跑 `pg_restore --no-owner --no-privileges` 后重装同版 owner 及窄授权。三个实际 LOGIN 均从公开视图读回同一用户 UUID 和邮箱；私有关系有效权限为 0，直接读取均为 `42501`。Source 41 项启动资格与导入器 installer 通过，临时 PostgreSQL 已删除。此回执不代替单个业务的恢复或连接器字节校验。
- **旧授权重放与普通账号业务**：`native-grants-509-stale-jobs-oid.json` 在 `c4c5184669` 为三个实际 LOGIN 逐一注入直接与 PUBLIC 旧授权；先实测旧关系／函数可读，再两次重装该档授权。私表、列、分区子表（含跨 schema）、物化视图、外表、序列及新建对象均不再暴露；直接改表和切换到仍有授权的无关角色均得到 `42501`。同一普通登录仍能创建持久 Jobs 并经公开接口读回各自的同一 UUID，无关账号原有授权保留。此回执验证的是普通角色切换，而非全套 HTTP 业务。
- **Audit 未来分区**：`flow-audit-trigger-clone-pg16.json` 在隔离 PostgreSQL 16 实测，已有月份可维护，但缺少复制不可变触发器所需的函数权限时，新建月份返回 `42501`。`18a8d6351b` 只向 `audit_code` 授权该触发器函数，真实创建 `2098-04` 分区并复制两个行级保护触发器；普通及维护 LOGIN 仍无该函数执行权，未给 Audit 增授 Users 引用权限。
- **保留的站点 Agent**：`site-agent-tls-e99a.junit.xml` 的三项用例均通过：真实 TLS 配对写入私有身份文件、独立 CLI 进程丢失确认后重启并重放同一结果、拒绝无效证书及重定向。HTTPS 对端是协议夹具；这不是本轮双站点或自定义 SFTP 的端到端证据。
- **默认业务**：`hosted-business.json` 首次停在资源准备；`native-grant-hosted-business.json` 已进入独立插件及服务启动，因 Inference 就绪探针未通过而中止。两者都是失败回执，不是业务通过。

## 完整 M2b 仍需独立回执

- **普通角色三档**：在最终迁移的隔离库，重放各 profile 的实际安装，再分别用 LAN 内嵌、LAN 远程 issuer 的 API consumer、工作树实际生成的运行 LOGIN 登录。枚举私有普通表、分区父表与实际子分区、物化视图、外表、列和序列；同时检查所有权、PUBLIC 与可切换角色。不以有 schema 权限、只数显式 grant、owner 测试或测试专用替代角色取代有效权限验证。恢复副本按同样顺序重放授权并复核。
- **默认业务冷启动与恢复**：用真实 API／worker／Beat、Broker 与对象存储验证登录、建项目、导入、标注、审核、通知及插件预标注。按恢复账本核对提交、进程丢失后的再领取、原操作 ID 和不确定确认；另取数据库恢复证据，不以全部服务健康代替业务结果。
- **保留的私有运行档**：按进程清单使用准确的 overlay／profile、固定镜像及窄登录。分别取实际冷启、凭据拒绝，以及该 owner 的恢复／重放回执；既有历史工件和只读配置解析不计为这些新档通过。
- **自定义 S3／SFTP**：验证既有 Source 的新受限 I/O 能力、真实字节与原保存身份，保留内建 MinIO、受控目录、站点 Agent 路径。负例至少覆盖停 Source 不回退、当前绑定变化不改历史目标、失联恢复复用原记录、unrelated 函数与操作 token 被拒；检查 API／普通 worker 不持有连接器密文、解密密钥或 Source 私有 SQL 身份。SFTP 使用实际核验的主机公钥，不将 S3 或内建存储的结果推及 SFTP。

最终回执需绑定实际提交、Alembic head、Compose 文件链和 profile、镜像、数据库及业务／对象身份；失败和窄项重试单独留存。以上未完成前，**不标记完整 M2b 通过；M2c 未获授权**。

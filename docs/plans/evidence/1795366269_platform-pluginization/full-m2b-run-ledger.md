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
- **新建数据库**：`schema-all-private-owner-502.json` 记录提交 `910804c179108cd99c75109debfafcb317d3d3c7`、PostgreSQL 16 镜像 ID 和实测 `bo0502`。空卷从头升级成功；这不证明后续 Source 修订、权限重放或业务通过。
- **默认业务**：`hosted-business.json` 首次停在资源准备；`native-grant-hosted-business.json` 已进入独立插件及服务启动，因 Inference 就绪探针未通过而中止。两者都是失败回执，不是业务通过。

## 完整 M2b 仍需独立回执

- **普通角色三档**：在最终迁移的隔离库，重放各 profile 的实际安装，再分别用 LAN 内嵌、LAN 远程 issuer 的 API consumer、工作树实际生成的运行 LOGIN 登录。枚举私有普通表、分区父表与实际子分区、物化视图、外表、列和序列；同时检查所有权、PUBLIC 与可切换角色。不以有 schema 权限、只数显式 grant、owner 测试或测试专用替代角色取代有效权限验证。恢复副本按同样顺序重放授权并复核。
- **默认业务冷启动与恢复**：用真实 API／worker／Beat、Broker 与对象存储验证登录、建项目、导入、标注、审核、通知及插件预标注。按恢复账本核对提交、进程丢失后的再领取、原操作 ID 和不确定确认；另取数据库恢复证据，不以全部服务健康代替业务结果。
- **保留的私有运行档**：按进程清单使用准确的 overlay／profile、固定镜像及窄登录。分别取实际冷启、凭据拒绝，以及该 owner 的恢复／重放回执；既有历史工件和只读配置解析不计为这些新档通过。
- **自定义 S3／SFTP**：验证既有 Source 的新受限 I/O 能力、真实字节与原保存身份，保留内建 MinIO、受控目录、站点 Agent 路径。负例至少覆盖停 Source 不回退、当前绑定变化不改历史目标、失联恢复复用原记录、unrelated 函数与操作 token 被拒；检查 API／普通 worker 不持有连接器密文、解密密钥或 Source 私有 SQL 身份。SFTP 使用实际核验的主机公钥，不将 S3 或内建存储的结果推及 SFTP。

最终回执需绑定实际提交、Alembic head、Compose 文件链和 profile、镜像、数据库及业务／对象身份；失败和窄项重试单独留存。以上未完成前，**不标记完整 M2b 通过；M2c 未获授权**。

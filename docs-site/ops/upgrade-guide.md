---
title: 版本升级指南
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-10-28
---

# 版本升级指南

本文描述将平台从一个版本升级到下一个版本的标准流程。

## 升级前准备

1. **阅读 CHANGELOG**：查看目标版本的 Breaking Changes 和 Migration Notes
2. **备份数据库**：
   ```bash
   docker exec ai-annotation-platform-postgres-1 pg_dump -U user annotation > backup-$(date +%Y%m%d).sql
   ```
3. **备份 MinIO**（如有重要标注数据）
4. **通知用户**：计划维护窗口

## 标准升级步骤

1. **确认部署归属并进入维护窗口**：核对当前 checkout、Compose 项目、容器挂载、数据库及队列消费者；停止旧版本写入口，排空 API、Worker 和 Beat 的在途任务。主 checkout 开发环境使用用户 systemd 与开发 Compose，生产环境使用其独立部署配置，不要按容器名或端口猜测目标。
2. **更新代码并核对构建输入**：记录当前部署 commit，比较目标发布标签的依赖、共享包、Dockerfile 与 Compose 变化：

   ```bash
   git fetch --tags origin
   git diff <deployed-commit>..<target-tag> -- apps/api/pyproject.toml apps/api/uv.lock \
     apps/web/package.json pnpm-lock.yaml apps/_shared infra/docker \
     apps/*-backend/Dockerfile docker-compose*.yml
   ```

   按选定部署流程更新代码与锁定依赖；共享包或镜像构建输入变化时，重建实际消费这些输入的 API、Worker 和已启用后端，保留模型与数据卷。生产启动命令须显式选择生产环境文件和 Compose 文件，见[局域网生产部署](./deploy/lan-production)或[容器部署](./deploy/docker-compose)。

3. **先显式应用迁移**：使用目标部署的迁移连接运行 `alembic upgrade head`，确认单 head 及数据库版本可达；不要对日常业务库运行测试初始化。开发 Worker 关闭自动迁移，不能用重启代替此步骤。
4. **同步刷新所有消费者**：启动匹配版本的 API、全部相关 Worker、Beat 与 Web。Celery 不会因挂载源码变化而热重载；环境变量变化需要重建容器，而不是只执行 `restart`。

   从使用 Redis 锁的旧导出 worker 切到使用 PostgreSQL advisory lock 的版本前，须先排空并停止旧导出 worker。两种锁不能互相排斥，不得并存领取导出任务。

5. **验证后恢复入口**：分别检查数据库、Redis、对象存储、Worker 注册与实际业务读取。主 checkout 默认开发地址如下；其他部署使用其实际协议和端口：

   ```bash
   curl -f http://localhost:8000/health
   curl -f http://localhost:3000/
   ```

## 迁移相关说明

- 数据库迁移通过 Alembic 自动管理，每次升级必须执行 `alembic upgrade head`
- 只有目标 migration 明确提供可逆 downgrade 时才能执行 `alembic downgrade`；数据归位、合并或候选暂存类迁移可能是 no-op downgrade 或会丢数据，回滚前必须先读 migration docstring
- 前端静态资源由 Vite 构建，版本号在文件名中，无缓存问题

合并后的迁移链保留主线 `0190`–`0267`，随后接入改名前缀的 `bo0190`–`bo0370`。旧 backend-opt 数字谱系（包括旧 `0366`、`0370`）不等于主线。Alembic 在发现这组旧数字修订与旧 Workspace owner schema 时会在 DDL 前拒绝。先备份数据库与角色，在副本上迁入并核对主线结构、来源身份与位置账本，再由部署负责人确认采用。不要用 `stamp`、删版本表或放宽授权绕过。

## 重点注意事项

<span id="native-owner-namespace-cutover" aria-hidden="true"></span>

### Native 数据库协调切换

1. 按 `apps/api/alembic/sql/0474_owner_namespaces.json` 核对目标库实际存在的 schema、专用账号与事务设置。备份数据库、角色、授权和凭据，记录原连接上限与登录状态。不要用清单补建未绑定角色或改名共享账号。
2. 停止 API、worker、中继、插件及独立模块的新准入，结清在途写入、租约和物理工作。在独立事务中将目标库设为 `CONNECTION LIMIT 0`，将已绑定的旧专用账号设为 `NOLOGIN`。检查无其他客户端和预备事务。连接上限不约束超级用户，维护窗口内须另行关闭其他管理入口。
3. 由有权限修改清单内对象的迁移账号在已验证的 PostgreSQL 16 库执行 `alembic upgrade head`。遇到目标名冲突、跨库角色、额外依赖、持久设置或目录摘要不符时保持围栏，定位准确对象，不绕过校验。
4. 在围栏内更新 DSN、授权与模块工件。角色改名会清除依赖旧用户名的 MD5 密码；按凭据更新流程设置新密码，不轮换用户签名密钥。仅恢复原获准角色的登录和原连接上限，放行前核对受限读取和回执重放。

### 媒体输入升级 {#media-input-cutover}

升级到 `bo0222`／`bo0227` 前，停止旧图片批量预标受理并排空没有清单的作业。无法核实旧 payload 或来源时，由旧运行时明确失败，不补造输入清单。升级到 `bo0228` 前，同样排空旧跟踪和修正作业，采纳或丢弃仍可审阅的旧候选。升级后按[媒体执行角色](./deploy/docker-compose#media-executor-provisioning)装配授权，刷新实际 API、Celery 和独立 Media 消费者。

应用 `bo0233` 前，停用旧预测重试的派发与消费者，查明相关队列和在途执行均已排空。如果仍有未绑定的历史重试标记，确认排空后才可以使用 `alembic -x prediction_retry_legacy_drained=true upgrade head`。参数不绕过活跃旧作业检查。

### 回填历史任务数据集关联

先在目标库应用迁移，刷新 API、自动学习策略 worker 和 scheduler。使用 `apps/api/scripts/backfill_task_datasets.py`，明确选择 `--project-id UUID` 或 `--all-projects`。先以 `--dry-run` 预演并核对 JSON Lines 报告，再以 `--apply` 分批写入，最后用 `--verify` 检查遗漏和序号一致性。未知记录保留原样；中断时可重跑 `--apply`，不会重新分配既有序号。

在独立工作树中，先核对 `pnpm dev:worktree -- doctor --mode dev` 的目标，停止同模式进程，再用该模式执行。例如：

```bash
pnpm dev:worktree -- exec --mode dev -- sh -c \
  'cd apps/api && .venv/bin/python scripts/backfill_task_datasets.py --all-projects --dry-run'
```

仅 `--apply` 写库。兼容应用回退时停止新关联与回填，保留成员关系和序号，不降级删表。

### Workspace 旧拆分安装 {#workspace-split-upgrade}

当前默认 API 在原数据库事务中调用六组 Workspace owner，不再有按操作选定 Workspace URL、专用 DSN 或控制 bearer 的部署档。Workspace 首切片本身未引入新 schema 或授权；升级整套收敛拓扑仍须满足下节普通角色门禁。

若当前安装仍有这六组独立服务，或 API 环境仍设置其旧选择器，**不要带旧环境与 Compose 文件链滚动升级**。暂停升级，单独取得切换授权与维护窗口，核对现用 API／worker 角色、旧服务、配置注入及 secret 挂载，先备份并在隔离副本验证。再按批准的方案排空旧消费者、改用当前默认装配并复验权限与真实业务。代码合并不切换维护者日常环境，也不自动撤销既有凭据。

不要把旧专用 DSN、数据库 owner 或迁移账号复制到 API `DATABASE_URL`，也不要扩大普通角色的私表权限。保留实际需要的 Inference 认证探测和 Identity 资料依赖，见 [Workspace 部署说明](/api/guides/workspace-runtime#workspace-deployment)。较老数据库若要跨过已有迁移，仍须满足相应迁移的原门禁。

### 旧模块拓扑升级门禁 {#split-topology-upgrade}

**以下是获准切换时的门禁，不是现网已经通过或可以滚动升级的承诺。** [进程清单](../dev/concepts/module-deployment#deployment-convergence)区分源码默认档、保留服务和已观察证据。部署收敛不改写已接受的账本、对象地址或密钥。先备份数据库、角色、准确对象版本和私有环境文件，保留旧镜像与启动定义；先在隔离副本验证。切换时暂停入口、定时器及领取新任务，排空旧 worker、独立服务、连接、租约和实际 I/O；到期、撤销或未收到确认不证明外部动作停止。

| 旧绑定                                        | 切换到当前装配并核验                                                                                                                                                                                                                                                                            |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Workspace、Labeling Class Query／Video Writer | 停止旧进程，删除旧 Workspace 与 `LABELING_CLASS_QUERY_*`／`LABELING_VIDEO_WRITER_*` 产品绑定及对应宿主 secret 选择器。复核普通角色的三个类目函数；历史 SHA Mask GC 仍可用 `mask-gc` profile 手动预览。保留 `WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS`，它仍约束本地事务。                           |
| Jobs Queue／Dispatch Relay                    | 退役旧 HTTP 队列／relay、`JOBS_QUEUE_*`、`JOBS_DISPATCH_RELAY_*`、`AAP_JOBS_DEVELOPMENT_NETWORK` 及旧开发镜像选择。先安装新的[受限绑定](./deploy/docker-compose#jobs-recovery-provisioning)，再启恢复 worker 与 Beat。保留原消息、租约、执行 ID 及不确定发布记录。                              |
| Identity Admission／Mail／通知 publisher      | 默认内嵌签发；需要隔离才选 `identity-issuer` 和 `IDENTITY_URL`。保留现用 JWT key，停 V1 与旧 kick；`identity-mail-v2` 接管同库投递，包括测试邮件。移除旧 Mail DSN、旧 Identity publisher、`mail-v2` profile 及 API／worker 的 `SMTP_PASSWORD(_FILE)`；不要移除 sender 自己的密码。              |
| 独立 Notifications socket／publisher          | 移除 `NOTIFICATION_SOCKET_*`，反代仍指向 API；默认 Beat 每 10 秒恢复，与远程 Identity 无关。只在 publish-only 隔离时启用新发布 overlay，确认 API 仅有订阅身份与私有 bearer。                                                                                                                    |
| Assets Catalog／Source／Media                 | 退役 Catalog HTTP 和 `ASSETS_CATALOG_*`。分别选择 `ASSETS_VIDEO_EXECUTOR`／`ASSETS_IMAGE_EXECUTOR` 和[窄执行 LOGIN](./deploy/docker-compose#media-executor-provisioning)，不靠 `ASSETS_MEDIA_URL` 切换执行。已登记自定义 S3／SFTP 沿用现有 Source，见下文；保留历史保存目标并结清旧领取和 I/O。 |
| 独立 Inference Registry／Health／Probe        | 关闭按操作的容器，把已启用 URL 指向同一个 `inference:8768` 私网 origin。各用途 bearer、窄 SQL LOGIN、GPU Redis 与签名文件继续分开；不交给 API 或模型容器。确认端口空闲、逐项就绪后才放行调用方；不把宿主机转发可达性或 GPU 资格当作已验收。                                                     |
| Audit                                         | 保持 Beat 与维护 worker；先确认管理员能查看已发布的归档输出。受限 CLI 只作一次性运维，不能代替产出 `audit_archive_outputs` 的定时任务或整个维护队列。                                                                                                                                           |

**迁移与恢复授权顺序**：先对准确目标使用迁移 LOGIN 应用 Alembic head。采用独立 NOLOGIN owner 的库，在装配普通授权前，以该迁移操作者运行 `psql -X -v ON_ERROR_STOP=1 … -f infra/docker/module-data-grants.sql`，使用脚本头所列目标库和角色变量；保持同版本脚本及相邻 SQL 的目录结构。恢复到隔离新库时，先撤销 PUBLIC 的数据库连接和临时对象权限，再用 `pg_restore --no-owner --no-privileges`。该模式不会保留原有函数的 `REVOKE`：开放流量前必须重放模块 owner 安装器，再安装实际运行档和各窄角色授权，不能仅重授普通账号。标准工作树按启动器的迁移角色和普通授权装配，不用伪造 NOLOGIN owner 去执行拆分配方。分别检查 LAN 内嵌、LAN 远程 issuer 的 API consumer 与生成工作树的实际运行 LOGIN 对私表、列、物化视图、外表、父子分区及序列均无有效权限；重点核对实际所有权、PUBLIC 与成员关系。维护／迁移另用授权身份，不给普通 API／worker DDL 连接。

**推理凭据**：普通 LOGIN 能登记观察或接收结果，不应能开封观察、探测或注册表中的后端令牌。既有安装和恢复副本都要重放当前普通授权及 Inference 用途 LOGIN；在流量仍隔离时验证普通身份被拒绝、私有服务仍能完成一次已登记后端的认证操作。不要恢复旧 `EXECUTE` 或给 API 配置私有服务的数据库身份。

**登记的自定义 S3／SFTP**：切换前逐项盘点保存的版本、未结算的 I/O 和仍需历史访问的位置。为既有 `assets-source` 安装[窄 SQL 授权、作用域 token 和持久暂存](./deploy/docker-compose#source-registered-io)，不要改向内建 MinIO、受控目录或站点 Agent。不能把连接器秘密、开封函数或 Source 的数据库连接下放给普通 API／worker。仅在隔离副本上用原保存身份验证真实连接器字节、拒绝回退与原记录接续后，才能放行该部署，见[普通角色与 Source 边界](../dev/concepts/module-deployment#m2b-runtime-boundary)。

使用九用途迁移预览时，旧库也须重放上面的模块 owner 安装器。目标探针需要由 Assets 的 NOLOGIN 函数所有者核对封存目标、发布窗口及修订、切换记录 ID；这些列不授权给 Source 的 LOGIN 或普通账号。运维探针通过仅说明目标能完成该用途的受限 I/O，不能据此宣布绑定已切换。

**需要保留的 Recovery 邮件密文**：原来设置了独立 Recovery Fernet key 的，继续使用原 key。若旧内嵌 producer 曾自动由其有效 JWT 密钥派生，则在可信离线环境用已安装的 Identity 包一次性派生到新文件；`OLD_EFFECTIVE_JWT_FILE` 是只含旧有效 JWT key 的只读单行文件，`RECOVERY_FERNET_FILE` 是受限目录内尚不存在的目标：

```bash
uv run --project apps/identity --locked python - "$OLD_EFFECTIVE_JWT_FILE" "$RECOVERY_FERNET_FILE" <<'PY'
import os
from pathlib import Path
import sys

from yoao_identity.recovery_crypto import mail_key_from_secret

old_key = Path(sys.argv[1]).read_text(encoding="utf-8").removesuffix("\n")
mail_key = mail_key_from_secret(old_key)
with os.fdopen(os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="ascii") as stream:
    stream.write(mail_key + "\n")
PY
```

不打印或挂载旧 JWT key 给 sender。只把派生结果交给 sender 和实际恢复 producer（API 内嵌或远程 issuer）；不要把 SMTP／sender DSN 提供给 API。先在隔离恢复中确认历史待投递邮件可解密，再启新生产者。若原 key 不可确定，保持围栏与记录，不随机生成密钥覆盖历史意图。

迁移与授权后重建实际消费者，核对登录、项目、导入、标注、审核、插件预标注、WebSocket 许可，验证实际 claim／publication、至少一次重新派发及待投递邮件恢复。启用的 split 也须按真实 overlay 验证冷启、拒绝与恢复。显式 owner 不可用时不得降回更宽的本地凭据；提交状态不确定时查原账本，不新建请求。新配置未通过则保持入口关闭并保留旧工件与数据，不用无保护回退或强删 outbox。仅在验证成功、回退资源已有副本且获明确授权后退役不再使用的凭据。

### 维护者日常环境另行迁移（M2c） {#daily-topology-cutover}

维护者日常主 checkout 已完成获准的 M2c 原地迁移，[运行账本](https://github.com/YOAO-Labs/ai-annotation-platform/blob/3b0b311966494cd095e6f21f2bd66f8251bcf34f/docs/plans/evidence/1795366269_platform-pluginization/full-m2b-run-ledger.md#m2c)记录了范围与未通过项。这个结果不授权其他环境切换。应另批准目标环境、备份与恢复证据、维护窗口和实际新拓扑验证方案，不直接照搬启停命令。

### 训练、数据集与存储演进迁移索引（0175–0267）

升级时按实际数据库版本选择下表涉及的迁移段，核对受影响表族、进程刷新要求及降级保护边界。训练执行器还须使用与 API 目录匹配的共享契约和镜像；数据集与对象存储的逐项恢复、切换顺序见[一致备份与隔离恢复演练](/ops/deploy/storage-recovery)与[对象存储迁移与切换](/ops/deploy/storage-migration-inventory)：

| 迁移范围                                     | 核心能力与表族                                                                                                                                                        | 升级后需刷新的组件                                                                                       | 回退与降级约束                                                                                                                       |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `0175`–`0189`                                | 冻结训练数据版本、实验与执行尝试、受控模型回流、配方、预览、评估、模型谱系、参数搜索、主动选样、任务版本流及训练清理责任                                              | API、`default` / `export` / `cleanup` Worker、Beat；共享契约或原生依赖变化时重建训练执行器及相关推理镜像 | 降级会删除训练、模型决策或清理责任记录；已有实验、保留产物或未收尾资源时先停止领取和执行，保留数据与独立产物，不以降级代替资源结算。 |
| `0190`–`0193`                                | 推理凭据修订、不可变预测执行快照、按任务独立的模型发布与自动学习策略                                                                                                  | API、预测与失败重试 Worker、执行策略的 `default` Worker、Beat                                            | 历史模型或策略任务身份不明时迁移拒绝，须先核对；保留分割发布或策略时拒绝有损降级，不猜测改成检测任务。                               |
| `0194`–`0195`                                | 数据集原件身份与保存位置、默认平台位置及九用途绑定、受控存储位置                                                                                                      | API、全部原件与存储消费 Worker                                                                           | 迁移只建表，不自动登记旧原件或初始化九用途；先统一部署地址，再显式定位回填与内容核验，保留已有样本、任务和标注。                     |
| `0196`–`0205`                                | 目录来源扫描账本、内容核验、原地引用（`dataset_references`）、登记作业、预览策略、预检差异、周期同步计划与重定位顺序                                                  | API、`default` / 定向 `worker_direct` Worker、Beat                                                       | 存在扫描轮次、引用或同步计划记录时禁止破坏性降级；回退前先暂停周期同步并排空活动任务。                                               |
| `0206`–`0208`                                | 数据集个人动态视图、图片质量检测运行與固定范围批量复核                                                                                                                | API、`default` Worker                                                                                    | 降级会移除个人视图或复核批次表；有质量运行或复核历史时保留 schema。                                                                  |
| `0209`–`0215`                                | 平台桶占用快照、连接器修订、导出格式、预测输入租约、完整原图缓存归属、数据集生命周期归档与位置访问维护门禁                                                            | API、`default` / `cleanup` / `ml` Worker、Beat                                                           | `original_cache_entries` 与 `storage_operations` 随库保留；未收尾访问禁止按 TTL 强清。                                               |
| `0216`–`0219`                                | 托管原件退役断言、保留策略与 Hold、完整核验及条件创建账本                                                                                                             | API、`default` / `cleanup` Worker、Beat                                                                  | 存在创建回执、退役断言或保留 Hold 时拒绝破坏性降级。                                                                                 |
| `0220`–`0237`                                | 27 类持久对象归属（训练版本/实验/预览/评估/选样、头像、导出 ZIP/媒体包、报告/评论/指南附件、审计归档、持久 RLE、独立 Task 原件、金字塔、视频派生、NuScenes 辅助原件） | API、`default` / `export` / `media` / `cleanup` / `maintenance` Worker、Beat                             | 不按当前环境变量静默回填历史归属；移除 bug/annotations/import 桶冲突自动过期规则；存在归属行时拒绝降级。                             |
| `0238`–`0241`                                | 预标临时输入所有权与版本化租约（图片、多文件、整视频 v3）                                                                                                             | API、`ml` / `cleanup` Worker、Beat                                                                       | 升级前须在 `import` 桶启用版本控制；存在租约或 `deleted` 墓碑时拒绝降级。                                                            |
| `0242`–`0246`, `0249`                        | 对象占用分类明细、外部来源保护声明、增量账目事件与本地来源去重故障状态                                                                                                | API、`default` / `maintenance` Worker、Beat                                                              | 保留原始观测与增量事件；存在记录时拒绝破坏性降级。                                                                                   |
| `0243`, `0245`, `0247`–`0248`, `0250`–`0254` | 九用途（头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件、训练文件）新分配切换与 `schema2` 引用                                               | API 及对应读写/清理 Worker                                                                               | 仅后续新分配采用新目标；应用回退须保留新 schema，通过受控预览确认切回后续分配。                                                      |
| `0255`–`0264`                                | 迁移源清单、初次复制、增量批次、迁移维护窗口、15 分钟任务进度提示、准确版本回收、每日观测率分母、窗口绑定最终封存、读取映射发布与九用途原子切换/回滚                  | API、全部存储消费 Worker、Beat                                                                           | 复制计划、迁移窗口、回收墓碑、读取发布与切换组独立于 AsyncJob 保留；存在事实时禁止降级。                                             |
| `0265`–`0267`                                | 只读站点 Agent（`sites`、`agent_registrations`、`agent_credentials`、`site_agent_commands`）与引用样本同内容显式备用来源（`dataset_reference_replicas`）              | API（需加载 `yoao-filesystem-read` 与 `yoao-site-agent-contract`）、`default` Worker                     | 保留身份修订、关闭证据与副本授权记录；非空时拒绝破坏性降级。                                                                         |

### 旧数据集内容身份

升级新增的内容与位置映射保留既有 Item、任务、标注及冻结版本。先运行[定位回填](/dev/concepts/storage-connections#内容身份与兼容回填)并核对报告；其中 `--verify` 只检查数据库定位，不读取原件，不能作为内容可用证明。

定位回填的 `--apply` 会初始化默认平台位置及九用途绑定，并固定当前进程的 `MINIO_ENDPOINT`、`MINIO_USE_SSL` 与用途桶。执行前必须让 API、所有 Worker、Beat 和操作命令使用同一组配置，且该地址须在宿主机与容器内均可达；宿主机的 `localhost:9000` 与容器的 `minio:9000` 不能混用为同一已登记位置。主 checkout 优先使用仅开发服务生效的 systemd／Compose 覆盖，避免修改共享 `.env` 影响其他环境；使用同源对象代理时同步核对 Web 的 `MINIO_PROXY_TARGET`。不要先回填再改地址，也不要手工删除位置或绑定来绕过配置不匹配。

未知摘要的旧托管原件需按[显式补算流程](/dev/concepts/storage-connections#显式补算旧托管原件)先预览、核对当前字节，再按清单应用。不要把历史 hash 或 ETag 当作可信 SHA，不要为恢复读取而修改旧 Item／Task 路径。原件缺失或发生变化时先保留旧身份并处理数据问题。

回退前停止写入并排空后台消费者，保留已使用的内容身份、来源、用途绑定和归属账本。旧应用不理解外部引用时，不得接管这些记录的写入、删除或清理；按具体回退方案维持受控只读。不能统一执行 `alembic downgrade -1`，也不能以 `stamp` 绕过迁移屏障；具体应用回退须在一次性环境按目标版本演练。

### 数据集存储的受控只读应用回退

数据集存储采用保留schema的受控应用回退，不提供独立读取开关、双路径回退或回退计数。当前应用继续严格校验内容身份；未知摘要的旧托管原件须先按上文显式补算，再核对目标旧应用的可用读取范围。

旧应用不理解 `__aap_reference__/` 内容引用，也不能依赖新版的删除前检查。仅保留新表和迁移屏障仍可能被旧版级联删除，因此应先停全部API写入、Worker和Beat，排空活动任务，并保留可恢复的数据库及对象存储备份。不要让新旧应用同时写同一数据库，也不要运行旧镜像中自动迁移的入口。

紧急只读回退使用独立的非owner数据库账号；不要复用schema owner、迁移账号或原运行账号。只授予选定旧读取页面需要的表SELECT，不授予写权限，并设置 `default_transaction_read_only=on`。以下为数据集列表和任务元数据涉及的表；任务详情的锁查询限制见下文，连接凭据表及新来源配置表不在其中：

```sql
CREATE ROLE rollback_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
-- 在交互式 psql 中使用 \password rollback_reader 设置独立密码，不写入脚本或历史。
GRANT CONNECT ON DATABASE annotation TO rollback_reader;
GRANT USAGE ON SCHEMA public TO rollback_reader;
GRANT SELECT ON users, projects, project_members, task_batches,
  datasets, dataset_items, project_datasets, tasks, annotations,
  predictions, image_pyramid_assets, task_dataset_item_links TO rollback_reader;
ALTER ROLE rollback_reader SET default_transaction_read_only = on;
```

数据库名须按部署替换。只读默认值不能替代权限裁剪：应在演练中临时设置事务READ WRITE，确认INSERT、UPDATE、DELETE和TRUNCATE仍因权限被拒。不要赋予连接凭据表SELECT，也不要给旧进程挂载外部目录或提供来源解密／连接凭据。

对象存储也必须使用独立只读账号，不能沿用旧root凭据。仅允许对九个已存在用途桶执行HEAD所需的ListBucket／GetBucketLocation，并只对已核对的旧托管对象授予GetObject；不授予PUT、DELETE、生命周期变更或建桶。旧应用启动会HEAD九桶，HEAD失败可能转为建桶并导致启动失败；生命周期写入拒绝由旧代码记录告警，不能为消除告警授予写权限。即使平台桶中碰巧存在与不透明引用同名的对象，也不能允许旧应用读取该键。

旧应用仅在内部维护入口启动，使用匹配的部署产物及显式环境，不执行数据库降级；不启动后台消费者，禁用环境模型发现和推理连接。入口层拒绝写方法与WebSocket，并限定维护人员访问。GET本身也可能有副作用，因此数据库和对象存储只读权限必须同时保留。使用仍有效的JWT；旧API Key认证会更新 `last_used_at`，密码登录也可能写入，不属于此只读回退范围。维护入口应明确显示只读，不能承诺继续编辑或提交标注。

此模式主动停止Worker，旧版聚合 `/health` 因Celery无响应返回503是预期结果。分别核对数据库、Redis和MinIO健康，以及实际旧数据读取；不要为让聚合健康变绿而启动旧消费者。服务端返回的签名原图URL应使用不带平台Authorization头的独立请求下载。

回退核对旧数据集列表返回的原图URL，并下载完整字节比较摘要。原始基线的任务详情GET会执行 `SELECT FOR SHARE`，因此在真正只读事务中返回500；这一路径不可用，不能为恢复它而放开写权限。关联DatasetItem的任务原件按datasets桶保存，也不能用旧 `/files/tasks/{id}/file-url` 默认annotations桶的结果代替。再经旧HTTP尝试改名、删样本、删数据集，并直接测试数据库与对象写权限。入口正常应在调用旧处理器前拒绝写请求；底层演练绕过入口层时，旧处理器可能返回500，但每次请求必须有对应的数据库只读或权限拒绝证据，不能将任意500视为保护成功。

切回当前应用前停止旧进程并撤销专用凭据，保持schema不变；复核数据库身份、关系、标注、冻结快照及托管／外部字节，分别验证两类当前原件读取，再恢复匹配的Worker／Beat和写入口。应用回退不使外部引用变成旧版可读原件，也不恢复已丢失的外部文件。

本地可复现演练只操作已初始化的工作树test命名空间，使用本地虚拟环境、现有 `mc` CLI、原始基线完整代码归档，以及随机临时DB角色和一小时内到期的MinIO service account：

```bash
pnpm dev:worktree -- init --mode test
pnpm dev:worktree -- exec --mode test -- apps/api/.venv/bin/python \
  scripts/verify_legacy_storage_rollback.py --report /private/rollback-report.json
```

报告记录实际旧提交、检查结果和完整数据库行摘要，不包含密码或签名URL。脚本退出前撤销临时账号并停止旧API；合成业务夹具留在test命名空间，按工作树doctor给出的精确确认值停止并销毁。失败时保留私有诊断日志，清理本轮诊断后再交付。该演练不操作人工环境，不重建镜像，不提供生产回退资格，也不实现读取模式开关、双路径回退或计数。

### 跨过员工项目职责迁移

迁移链包含 `0173`、`0174` 时，按[员工项目角色迁移与回滚 runbook](./runbooks/project-role-migration.md)安排一次协调切换：先完成角色与遗留审核证据审计，在停写窗口排空旧写入者，保留经验证可恢复的迁移前快照，再同步部署匹配的 API、Celery worker 和 Web。

`0174` 将全局标注员、质检员转换为员工，授权改由项目成员职责决定；迁移不猜测补建成员关系，也不自动补造历史贡献者证据。自定义集成须适配 `employee` 与项目职责；遗留审核任务的未知证据可能阻断审核，应在切换前确定恢复方案。

::: danger 不可自动降级
`0174` 的 downgrade 在任何窗口都拒绝。重开写入前只能恢复精确的迁移前快照并回退旧二进制；重开写入后优先保留新身份模型向前修复，恢复旧快照则属于需要业务确认、会丢失新增写入的数据回滚。不要用下面的通用 downgrade 示例跨过 `0174`。
:::

### 跨过任务编辑锁域切换

迁移链包含 `bo0466` 时，按[任务编辑锁域切换 runbook](./runbooks/task-edit-lock-domain.md)安排维护窗口：停止 API／worker 与视频、Identity、Workspace 编辑锁写入者，排空连接，再应用迁移并同步部署匹配产物。迁移拒绝仍有旧域写者或活动旧域锁的数据库；不要用双锁、旧域回退或只升级数据库来跨过它。`bo0466` 不提供自动 downgrade。

### 跨过工具单位退役与视频候选迁移

从较旧环境直接升级、迁移链包含 `0115`–`0117` 时，必须安排维护窗口并先做数据库备份：

- `0115` 新增项目级 `ai_interactive_enabled`，并把 annotation 中退役的 `ai_interactive` 按几何分批归位到 `region` / `bbox`。分批降低单事务持锁，但大表仍会产生读写压力。
- `0116` 分批归位 prediction，并把项目 / 模板 `tool_bindings.ai_interactive` 中的类别与属性合并到真实几何单位。冲突时保留目标单位配置；downgrade 是 no-op，无法恢复退役单位。
- `0117` 新增 `video_tracker_jobs.staged_result`。downgrade 会删除该列，所有尚未接受的视频追踪候选随之丢失；回滚前先处理 `pending_review` / 带候选的 `cancelled` job。
- `0134` 新增 `raster_mask_uploads`，用于 task 级匿名 Mask 上传归属和并发配额。downgrade 会丢失尚未认领对象的额度账本，但内容寻址对象仍由 24 小时引用扫描 GC 回收。
- `0135` 新增项目级 `raster_mask_native_editing_enabled`，既有项目默认为关闭。downgrade 会删除该列并丢失项目的原生 Mask 灰度选择，不会删除已有 annotation 或内容对象。
- `0136` 新增原生 Mask 候选接受幂等账本。downgrade 会删除接受快照；回滚前不要依赖旧 idempotency key 重放结果。
- `0137` 为接受账本增加过期时间，使过期快照不再永久保留内容引用。downgrade 会移除 expiry，需确认清理任务与旧代码的引用口径一致。
- `0138` 增加 Tracker 局部审核 revision、稳定候选与部分状态。downgrade 会把部分审核状态退回待审，但已从 staged result 移除的候选不会恢复。
- `0139` 增加 correction job 种类、纠错帧 / track 快照和同轨活跃租约。downgrade 会取消活跃 correction 并清除其 staged result，已经保存到 annotation 的人工纠错关键帧不会回滚。
- `0148` 将图片原生 Mask 编辑的列默认值和所有既有项目改为开启。项目管理员升级后仍可单独关闭；downgrade 只把新项目默认值恢复为关闭，不批量改写现有项目选择。

升级后先核对 migration head，再验证：旧项目的 region / bbox 类别仍完整、项目级原生 Mask 编辑已开启且仍可单独关闭、部署创建总闸关闭时所有项目保持只读、交互式 AI 总开关正确、视频追踪完成后进入待审且局部决定可用、同一轨迹的纠错活跃租约能在接受 / 拒绝 / 取消后释放。

### 视频媒体处理

视频元数据处理要求 API / Celery 镜像内有 `ffmpeg` 与 `ffprobe`。涉及 Dockerfile 或依赖变化时必须 rebuild API 与 Celery worker 镜像，不能只重启容器。

当前视频播放使用[已核验原件准备与范围读取](/api/guides/verified-content#视频原件准备与范围读取)。播放器不再自动回退旧转码地址；未知原件须先完成定位回填和内容核验，容器解码能力另行检查。

历史视频元数据、海报、时间表、帧缓存与分块缺少新版输出归属时，需要从已核验原件重新生成。先恢复元数据，再处理对应帧与分块；可使用数据集的 `POST /api/v1/datasets/{dataset_id}/backfill-media` 或存储管理的资产重试入口。`video_derivative_unrecorded_or_stale` 不能通过直接补写 `output_id` 或反复重启解决，已存在的原件与标注应保持不变。

验证镜像内依赖：

```bash
docker exec ai-annotation-platform-api-1 ffprobe -version
docker exec ai-annotation-platform-celery-worker-1 ffmpeg -version
```

如果视频导入后没有 `video_metadata` 或 poster，优先检查 `media` 队列 worker 日志：

```bash
docker logs ai-annotation-platform-celery-worker-1 --tail 200
```

### 视频标注类型

新建视频轨迹标注默认写 `video_track_bbox`，旧 `video_bbox` 继续可读。升级时重点检查 OpenAPI / 前端类型是否与后端同步，避免旧前端无法识别 `video_track_bbox` discriminator。

## 回滚步骤

1. **先暂停写入和任务领取**，备份当前数据库及对象存储，保留升级后的 checkout 和镜像。核对目标应用、schema 与已有业务事实是否兼容。
2. **应用回退与数据库降级分开处理**：涉及内容身份、存储归属、冻结版本或未收尾责任时保留新 schema，按上面的受控只读回退及专项 runbook 操作；涉及 `0174` 时使用其专用恢复流程。
3. **只有逐项确认可逆后才降级数据库**：在仍包含待降级迁移的升级版本中执行已批准的 `alembic downgrade <previous-revision>`，不能先切到不认识当前 revision 的旧代码。存在保护事实时停止，不使用 `stamp` 或删除账本绕过限制。
4. **确认数据状态后再切换应用版本**：选择兼容的目标标签和镜像，使用标准升级步骤中已核对归属的部署配置刷新 API、相关 Worker、Beat 与 Web，再检查健康状态及旧数据读取。不要用默认 Compose 配置覆盖独立生产或开发部署。

## 零停机升级（高级）

当前版本不支持滚动升级（同时运行两个 API 版本）。需要写入排空或数据转换的升级应安排维护窗口；停机时长取决于在途任务、迁移和实际数据量，先在隔离恢复环境演练，不承诺固定停机时间。

## 版本查询

```bash
# API 版本
curl http://localhost:8000/health | jq .version

# 数据库迁移版本
docker exec ai-annotation-platform-api-1 alembic current
```

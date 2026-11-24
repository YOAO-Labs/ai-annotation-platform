# M3c 运行账本：独立语言飞书私信通知渠道插件

记录日期：2026-11-24
对应计划：[平台插件化计划](../../1795366269_platform-pluginization.md) §4 M3 / M3c
对应架构决策：[ADR-0187](../../../adr/archive/0187-notification-channel-extension-point.md)（Accepted）

## 1. 交付范围（跨双仓边界）

- **`YOAO-Plugins` 独立插件仓库**（`plugins/notification-channel/feishu-dm/`，本地分支 `main` 提交 `b8cc96f`）：
  - 独立于 Python `Plugin-SDK` 与平台主仓，基于 Node.js 20+ 原生 ESM，零第三方 npm 依赖，MIT 许可。
  - 实现 `yoao-notification-channel/v1`（`GET /health`、`GET /setup`、`POST /deliver`、`POST /verify-address`）与 `yoao-extension-manifest/v1`（`manifest.json` 与 `Dockerfile` 内嵌 `LABEL io.yoao.plugin.manifest`）。
  - 实现飞书开放平台 v3 `tenant_access_token/internal` 缓存与过期码（`99991661`／`99991663`／`99991664`／`99991668`）并发合并刷新、单次投递共享总截止时间（`<= 9000ms`，适配平台 10s 读超时）、`im/v1/messages?receive_id_type=open_id` 交互式卡片投递、原生 `uuid` 叠加有界内存 `delivery_id` 幂等去重、凭据 `*_FILE` 只读文件加载与失败关闭，以及飞书错误码到 `delivered`／`permanent`／`retryable` 的准确分类。
  - 新增 `YOAO-Plugins` CI 工作流 `.github/workflows/plugin-notification-channel.yml` 与入口 `README.md` 索引。
- **平台主仓 `ai-annotation-platform`**（分支 `plugin/plugin-opt`）：
  - 新增开发者教程 `docs-site/dev/tutorials/notification-channel-plugin.md`（加入左侧导航）、更新 `docs-site/dev/index.md` 仓库索引与 `CHANGELOG.md`。
  - 平台主仓不混入插件源码或跨仓测试耦合；跨语言清单与镜像标签契约由隔离端到端验收实测 `ExtensionManifestV1.model_validate_json` 校验。

## 2. 自动化检查与单元测试

| 检查项                                       | 命令                                                                                               | 结果                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `YOAO-Plugins` Node 插件单元测试与标签一致性 | `node --test plugins/notification-channel/feishu-dm/test/plugin.test.mjs`                          | 6/6 通过（配置失败关闭与 `*_FILE`／URL 规范校验、原型安全卡片转义与路径遍历防线、鉴权／token 缓存与 `99991663` 并发合并刷新／`delivery_id` 去重／`230013`／`230006`／`230027`／`11232`／`230020`／未知 4xx 归类为 `retryable`／`413` 连接关闭／异常捕获与 `/verify-address`、`manifest.json` 与 `Dockerfile` 标签一致性） |
| 平台注释棘轮检查                             | `pnpm lint:comments`                                                                               | 0 新增解释性注释                                                                                                                                                                                                                                                                                                          |
| 文档版本号检查                               | `node scripts/check-doc-version-prefix.mjs docs-site/dev/tutorials/notification-channel-plugin.md` | 未发现产品版本沿革标记                                                                                                                                                                                                                                                                                                    |
| 文档站构建                                   | `pnpm docs:build`                                                                                  | 通过（含死链检查）                                                                                                                                                                                                                                                                                                        |

## 3. 隔离环境端到端验收（`YOAO-Plugins` 真实 Node 插件 + 飞书开放平台替身）

验收回执：`.artifacts/test-results/m3c-acceptance-746570df0d.json`（运行于可销毁 `postgres:16-alpine` + `redis:7-alpine` + 真实 Alembic 迁移与受限角色授权；验证后全部容器、进程与临时镜像已销毁，未触碰日常 `annotation` 库）。

### 3.1 直连档（Direct Tier，裸 `node src/server.mjs` 进程，零 Docker 依赖）

| 验证项                                                 | 实测结果                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 预注册、CAS 启用与地址验证                             | `POST /admin/notification-channels`（`mode="direct"`, `status="prepared"`）探测 `/setup` 自动回填 `feishu_dm`／`飞书私信`／`description`／`homepage`（指向 `YOAO-Plugins`），`enable(revision=1 -> 2)` 后绑定 `open_id` 并调用 `/verify-address`，替身飞书收到验证卡片（`om_fake_feishu_1`，`token_calls=1`） |
| 不变量 1：业务事务回滚                                 | 通知与业务回滚后发件箱 0 行、替身飞书 0 次调用                                                                                                                                                                                                                                                                |
| 不变量 6：重复入队、`in_app=false`、脱敏卡片与插件幂等 | 相同 `operation_id` 写入两次后站内 0 行、发件箱 1 行；worker 仅送出 1 张卡片，按钮链接拼接正确且不含 `reject_reason` 自由文本；以同一 `delivery_id` 再次直调插件 `/deliver` 返回缓存 `200 delivered`，替身飞书新增 0 次调用                                                                                   |
| 飞书 token 过期自动刷新                                | 替身返回 `400 code=99991663` 后插件单次刷新 `tenant_access_token`（`token_calls: 1 -> 2`）并在同一次 worker 尝试内投递成功                                                                                                                                                                                    |
| 不变量 2：上游不可用重试至耗尽                         | 替身依次返回 `503` 与 `429 Retry-After: 10`，worker 以相同 `uuid` 重试 2 次后记为 `failed_exhausted`                                                                                                                                                                                                          |
| 不变量 3：飞书永久错误不重试                           | 替身返回 `400 code=230013`，插件返回 `422 permanent`，worker 1 次尝试即记为 `failed_permanent`（`feishu_code_230013`）                                                                                                                                                                                        |
| 不变量 4：投递前权限撤销                               | `task` 改派、`bug_report` 删除、`export` 易主后均记为 `skipped: access_revoked`，替身飞书 0 次调用                                                                                                                                                                                                            |
| 不变量 7：用户解绑                                     | `DELETE /notification-channels/feishu_dm/binding` 后待投递行记为 `skipped: binding_removed`，替身飞书 0 次调用                                                                                                                                                                                                |
| 不变量 5：停用与退役                                   | `disable` 后待投递行记为 `skipped: channel_disabled`；`retire` 后 `auth_token_ciphertext IS NULL` 且 9 条历史投递记录完整保留                                                                                                                                                                                 |

### 3.2 安装档（Managed Tier，非 root 只读 OCI 镜像）

- 从 `YOAO-Plugins/plugins/notification-channel/feishu-dm` 使用本地缓存 `node:22-alpine` 离线构建镜像，通过 `docker image inspect` 读取 `io.yoao.plugin.manifest` 并经 `ExtensionManifestV1.model_validate_json` 校验通过。
- 以 `--read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --user 1000:1000` 启动容器，经 `docker inspect` 确认非特权、只读根文件系统、全 capability 剥离、128 MiB 内存上限、UID `1000:1000` 且不含平台数据库／JWT／加密密钥环境变量，应用文件归属 `root:root`（运行用户不可修改自身源码）。
- 经 YOAO `mode="managed"` 注册并携带 `manifest`，完成英文用户 `/verify-address` 与 `task.rejected` 端到端卡片投递（标题 `Task rejected`，零自由文本泄露）。

## 4. 未覆盖范围与待办

- **真实飞书开放平台测试应用冒烟**：尚未执行（`real_feishu_live_verified: false`）。需由维护者提供临时飞书测试应用凭据（`FEISHU_APP_ID`、`FEISHU_APP_SECRET` 及测试收件人 `open_id`，仅注入运行环境、严禁入库）后完成最后一次真实飞书私信验证。

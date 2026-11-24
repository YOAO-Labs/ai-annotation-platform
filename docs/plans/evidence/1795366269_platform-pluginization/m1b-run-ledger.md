# 平台插件化 M1b 运行账本

## 2026-11-24：M1b 安装档简化与本地隔离验收

**基线与范围。** 分支 `plugin/plugin-opt`，代码提交基于 `613948be6`。本账本覆盖 M1b 全部本机可授权交付项：从 OCI 镜像标签 `io.yoao.plugin.manifest` 读取插件清单以减少手写 `deployment.json`；把基于 `io.yoao.plugin.qualification-command` 的 GPU 生命周期资格报告生成接入 `yoao plugins qualify`／`install --qualify`／`upgrade --qualify` 而不放宽现有 GPU 资格门禁、安装身份或排空语义；将超管安装文档拆分为“操作步骤”和“安全原理”两层并把内部机制迁入开发概念页。**未向 PyPI 发布任何包**，正式发布继续等待维护者单独授权。所有运行写入均限定在本次可销毁容器、临时库与隔离虚拟环境中，未触碰维护者日常 `annotation` 数据库或默认端口。

### 代码与契约验证

- `apps/_shared/inference_contract`：`uv run --offline --project apps/_shared/inference_contract --extra test pytest apps/_shared/inference_contract/tests -q` 通过 **218** 项（覆盖 `PluginImageManifest` 禁止内联 `artifact`、`bind_artifact` 摘要绑定、严格有限 JSON 与重复键拒绝、64 KiB／4 KiB 体积上限、`validate_oci_qualification_command` 与 `parse_oci_qualification_command` 拒绝 shell 与 `python* -c`／捆绑 `-Ic`）。
- `apps/api` 安装观测与门禁回归：`apps/api/.venv/bin/pytest apps/api/tests/test_plugin_installation_api.py -k "observation or admission or gpu_activation" -q` 通过 **4** 项，服务端校验逻辑与 SQL 契约（`0388_inference_plugin_installation.sql`）保持不变。
- 离线发行包全套验证：`UV_OFFLINE=1 python3.11 scripts/test-sdk-distributions.py` 退出码 `0`：核心 sdist 自检通过，`sdk-repository` **49** 项通过（含 OpenAPI 同步、操作目录确定性生成与文档代码片段），`sdk-installed` **1233** 项通过（0 跳过），`yoao_inference_contract` **30** 项、`yoao_protocol_v2` **29** 项、`yoao_training_contract` **32** 项通过。
- 本次测试生成的发行包 SHA256：
  - `yoao_sdk-0.20.0-py3-none-any.whl`：`ef8da48edeb9bcb6896bc79d754ba3dbe9b102f379306fdcbd9475df86ede8ae`
  - `yoao_inference_contract-0.1.0-py3-none-any.whl`：`fdcb9493b776e463dd4a769e07ade37b8eb7b558becee9c7047a75d824feb9a5`
  - `yoao_lifecycle_contract-0.1.0-py3-none-any.whl`：`c748c04f27322348238b875f478bd5f70611368102e4a8b88132e052ba96fbc3`
  - `yoao_protocol_v2-0.2.0-py3-none-any.whl`：`b619686d0deefdb5eef5fb0cd04cf5e373c896c75b820567e8efb6d067a7a937`
  - `yoao_task_contract-0.1.0-py3-none-any.whl`：`9708aecbd2654643713781e3e4286a250013eb91b3da8d3a9a4dbc00aca93931`
  - `yoao_training_contract-0.1.0-py3-none-any.whl`：`09a74c6e4c9fc90da4f3df21f236b536eca46bcd47a8c47d79165f3e3be8b13d`

### 隔离环境 CPU 简化安装与退役实测

验收批次 ID `f356fc07`（2026-11-24T03:29:23Z），证据回执保存在 `.artifacts/test-results/m1b-cpu-acceptance-f356fc07.json`。环境组成：独立临时 `postgres:16-alpine`（执行 `alembic upgrade head` 并撤销 `PUBLIC` 默认 `TEMP`/`CREATE` 权限，仅授予 `yoao_inference` 最小函数账号）、`redis:7-alpine`、`minio/minio:latest`、回环 `registry:2.8.3`、真实 `yoao_inference` HTTP owner（`routing`／`health_observation`／`backend_control`／`execution`）、真实 `apps/api`（`ML_BACKEND_ROUTER_MODE=enforce`），以及仓库外新建的独立 Python 3.11 虚拟环境（从本地现建 wheel 安装 `yoao-sdk[plugins]` 与五个契约包）。

插件镜像基于缓存 `python:3.11-slim` 构建，写入 `io.yoao.plugin.manifest`（13 字段、不含 `artifact`、入口 `["m1b-cpu-plugin", "--port", "8011"]`），推送至临时回环 Registry 并按摘要拉取：`127.0.0.1:44545/m1b-cpu-detector@sha256:a0647f8c1bbfc89941e442e5dab5d5039938c6ee09e6cc94c1b80ea86eed89a9`（镜像 ID `sha256:e169a483027b261fc371820f571f08f7b9ffb3d03859b8a53f4834b90714349a`）。插件服务实现真实鉴权 `/health`（响应头回显 `X-YOAO-GPU-Health-Challenge`）、`/setup`、`/v1/yoao-routing/control` 与 `/lifecycle/terminal-drain`。

1. `yoao plugins inspect-image --image <pinned_ref> --json` 离线返回 `image_id`、绑定后的 14 字段 `manifest`（含摘要 `artifact`）与 `qualification_command: null`。
2. 不带 `--yes` 执行 `yoao plugins prepare --image <pinned_ref> ... --json`（全程无 `deployment.json`）被确认门禁拒绝且退出码非零；不创建状态目录，也不向 API 写入任何安装记录。
3. 带 `--yes` 执行 `yoao plugins prepare --image <pinned_ref> ... --json`（未显式传 `--container-port`）成功预留安装身份 `362bd46c-8c18-42da-8b86-925ce33b2e45`（`status=prepared`，`revision=1`）；从入口自动推导 `container_port=8011`，状态目录权限 `0700`，`receipt.json` 权限 `0600`。
4. 负例门禁：对同一 CPU 安装执行 `yoao plugins qualify` 与 `yoao plugins install --qualify --yes` 均失败关闭（`CPU installations do not accept GPU qualification`），未启动容器，状态保持 `prepared`。
5. `yoao plugins install --state-dir <state> --yes --json` 启动非 root 容器 `bb4250b63b94037964ae5ef223129e88adf288d3af5b82d95f8164c9a22048f0`（网络 `m1b-accept-net-f356fc07`、`Privileged=false`、`CapDrop=["ALL"]`、`no-new-privileges`、`NVIDIA_VISIBLE_DEVICES=void`、`CUDA_VISIBLE_DEVICES=""`、`DeviceRequests=null`），经 `yoao_inference` 鉴权观测 `/health`＋`/setup` 及强制路由 `drain`／`bind` 后进入 `status=active`（`revision=3`）；`yoao plugins status` 读回 `active`。
6. `yoao plugins uninstall --state-dir <state> --yes --json` 依次完成核心 `disable`、`quiesce`（真实调用 `/lifecycle/terminal-drain` 返回 `unloaded: true` 并签发 `safe_to_stop: true`）、`retire`（`revision=6`）、停止并删除已核验容器、删除 `data/token` 并从保留的 `receipt.json` 抹除 `token`；`yoao plugins status` 读回 `retired`，Docker 中无残留插件容器。
7. 验收结束后立即退役全部带 `org.aap.acceptance=m1b-cpu` 标签的临时容器、网络、镜像、进程与临时目录，复检确认零残留。

## 2026-11-24：推理插件运行时 `yoao` 命名升级复验

将平台侧推理插件容器标签（`yoao.installation`／`yoao.owner`／`yoao.image`／`yoao.config`／`yoao.candidate`）、容器名前缀（`yoao-plugin-`）、挂载目录（`/var/lib/yoao-plugin` 与 `/var/lib/yoao-qualification/*`）及注入环境变量（`YOAO_PLUGIN_*`、`YOAO_QUALIFICATION_*` 与 `ROUTE_REGISTRY_ID`，且 `yoao-protocol-v2` 对非空旧名 `AAP_ROUTE_REGISTRY_ID` 启动阶段失败关闭）统一切换后，在同一隔离架构下重跑全链路验收（批次 ID `3125e9c0`，2026-11-24T06:28:48Z，证据回执 `.artifacts/test-results/m1b-cpu-acceptance-3125e9c0.json`）：

- 镜像固定摘要：`127.0.0.1:49621/m1b-cpu-detector@sha256:983dc1164903efcc1e0be02f893696b6a700b6e9863e6169054b2f1fb71d8f0c`（镜像 ID `sha256:430eb2774f2ee383b4a0e883ec08e060ec3f62b90f78c08217e079a4c110bf4e`）；服务仅读取 `YOAO_PLUGIN_TOKEN_FILE`／`ROUTE_CONTROL_TOKEN_FILE`、`YOAO_PLUGIN_REGISTRY_ID` 与 `ROUTE_REGISTRY_ID`，无任何 `AAP_*` 回退。
- 验收脚本在运行中直接通过 `docker container inspect` 断言容器名 `/yoao-plugin-e79e6bf4-751b-4f17-a2c5-b79318448ed8`（ID `e2b49ea3346d88d9603df388ab02ecf9107359cc0c4faa1f3bfacb028611149b`）、仅含 `yoao.*` 标签（零 `aap.*`）、挂载 `/var/lib/yoao-plugin`、注入 `YOAO_PLUGIN_DATA_DIR=/var/lib/yoao-plugin` 与 `ROUTE_REGISTRY_ID=e79e6bf4-751b-4f17-a2c5-b79318448ed8`、容器环境零 `AAP_*` 变量，并将镜像摘要、隔离字段、修订迁移与 10 项检查状态写入 `.artifacts/test-results/m1b-cpu-acceptance-3125e9c0.json`（CPU 隔离 `NVIDIA_VISIBLE_DEVICES=void`、`CUDA_VISIBLE_DEVICES=""`、`DeviceRequests=null` 完整保持）。
- 全套 `inspect-image` → 无 `--yes` 拒绝 → `prepare --image --yes`（`revision=1`）→ CPU `qualify`／`install --qualify` 负例拒绝 → `install`（`revision=3`，真实强制路由 `drain`／`bind`）→ `status` → `uninstall`（`disable` → `/lifecycle/terminal-drain` `quiesce` → `retire`，`revision=6`，容器删除与 token 抹除）全部通过；所有带 `org.yoao.acceptance=m1b-cpu` 标签的临时资源验收后立即退役并复检零残留。
- 契约与发行包单测：`apps/_shared/inference_contract` **218** 项、`apps/_shared/protocol_v2` **226** 项、`apps/api/tests/test_plugin_installation_api.py` 观测／准入／激活 **4** 项、`scripts/test-sdk-distributions.py`（`sdk-repository` **49** 项、`sdk-installed` **1233** 项、三项契约分发共 **91** 项）全部通过；`Plugin-SDK` 与 `YOAO-Plugins` 独立仓须按同契约同步升级后方可与本版 `yoao-sdk`／`yoao-protocol-v2` 联用。

### 明确未覆盖边界

- **PyPI 发布未执行**：未向公网或私网 PyPI 上传 `yoao-sdk` 或契约包，等待维护者单独授权。
- **物理 GPU 在线资格生成未在本轮实机运行**：`yoao plugins qualify` 与 `install --qualify` 的容器隔离参数（`--network none`、只读挂载 `context.json`／`assets`／`fixture`、禁止挂载 `data/token` 与 `gpu-keyring.json`）、报告校验、回滚与 CLI 集成已由契约与 SDK 单测覆盖；带真实 `io.yoao.plugin.qualification-command` 的 NVIDIA GPU 实机端到端安装留待 `YOAO-Plugins` 镜像写入该标签后在单卡环境验证。
- **M1c Web 安装入口**：按计划在 M1b CLI 流程稳定后再评估，本轮未实现。

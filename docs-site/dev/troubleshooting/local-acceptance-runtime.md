---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-11-12
---

# 本地验收运行时踩坑

隔离工作树里的真实浏览器／容器验收会同时压到内核进程身份、Docker 网络、Celery broker、Uvicorn 生命周期与 Playwright 子进程。这里归档这些"看起来像产品失败、实际是运行时约束"的问题：**症状 → 根因 → 当前约定**，每条都指向本仓现有实现。

> 产品行为本身的问题见各自的 owner 文档；CI runner 侧的问题见 [CI 服务依赖踩坑](./ci-flaky-services)。

## 速查表

| 症状                                                             | 见本节 |
| ---------------------------------------------------------------- | ------ |
| `AttributeError: module 'os' has no attribute 'pidfd_open'`      | §1     |
| 旧 PID 被复用后仍被当作同一进程清理                              | §2     |
| 队列“看起来空了”却被判定仍有工作                                 | §3     |
| 接入新网络后既有 Redis／PostgreSQL 连接全部断开                  | §4     |
| `could not find an available, non-overlapping IPv4 address pool` | §5     |
| worker 容器启动即以 139 退出，任务从未创建                       | §6     |
| Uvicorn 完成优雅关闭后退出码为 `-15`                             | §7     |
| `mc` 退出 0，但对象/IAM 操作实际未生效                           | §8     |
| `AF_UNIX path too long`                                          | §9     |
| 容器访问宿主 `localhost` 端口被拒绝                              | §10    |
| 本地 Playwright 运行被 15 分钟总期限截断                         | §11    |
| `pg_restore` 在固定期限内超时                                    | §12    |
| 关闭浏览器夹具时 esbuild 被信号杀死                              | §13    |

## 1. Python 3.12 没有 `os.pidfd_open`

### 症状

退出探针或夹具 owner 直接调用 `os.pidfd_open` 时抛 `AttributeError`；或调用 `signal.pidfd_send_signal` 报不存在。

### 根因

规范入口使用的 Python 3.12 解释器没有绑定 `os.pidfd_open`、`signal.pidfd_send_signal` 与 `os.P_PIDFD`，而本机内核具备这些能力。把"解释器缺绑定"当成"内核不支持"，就会退化成不可靠的退出证明（轮询 `ps`、`kill -0`）。

### 规避

统一使用 `scripts/linux_pidfd.py`：它以 `ctypes` 直接发 `pidfd_open`(434)／`pidfd_send_signal`(424) 系统调用，并提供 `waitid`（`P_PIDFD`）回执与 `poll` 存活判定。`scripts/worktree_runtime.py`、`scripts/raster_web_process.py`、`scripts/raster_consumer_process.py` 与原生夹具都走这个 helper。不更换解释器，也不降低退出证明。

## 2. 进程身份用内核出生身份

### 症状

清理入口拒绝处理一条看似正常的进程记录（`进程记录缺少可验证的内核出生身份；保留记录及资源`），或者同一 PID 已被复用却仍被当作原 owner 结算。

### 根因

`ps lstart` 是墙钟字符串，随 locale、时钟漂移与第二次读取而变化；PID 在进程退出后可被复用。只比较 PID／启动时间字符串，既会误杀邻居，也会把新进程当成旧 owner。

### 规避

`scripts/worktree_runtime.py` 的 `process_handle()` 记录 `start_ticks`（`/proc/<pid>/stat` 第 22 字段）与 `boot_id`（`/proc/sys/kernel/random/boot_id`），`process_matches()` 只在内核出生身份、scope 解析路径与命令摘要全部一致时返回 `True`；缺少出生字段的记录直接拒绝，不会被当作"已停止进程"继续清理。`ps` 输出仅作可读附件，不作为身份判据。

## 3. Redis 队列空闲的判定

### 症状

所有 Celery 消费者都已停止，队列却仍被判为非空（`raster_queue_not_empty`），或在强制 `FLUSH`／删键后才发现真实消息被一起丢弃。

### 根因

Kombu 会留下被动的 `_kombu.binding.*` 集合；已封存的 worker 还会留下带 TTL 的 `celery:hb:<...>` 心跳字符串。这些都不是待处理工作。反过来，消息列表、未确认 hash／有序集合与未知键才是工作，必须拒绝。

### 规避

`scripts/run_raster_fault_e2e.py` 的 `require_broker_idle()` 用只读 Lua 脚本一次性核验：仅当键是 `_kombu.binding.` 前缀的 `set`，或是本 run 的 `celery:hb:` 键且 `PTTL > 0`、值为纯整数时才放行；其余类型与未知键都返回拒绝，并记录键的十六进制、类型、大小与剩余 TTL。守卫不消费、不删除任何状态，只读连接即可。

## 4. 接入新网络会断掉已有连接

### 症状

把一个已在运行、端口已发布的依赖容器 `docker network connect` 到验收专用网络后，原有 TCP 连接立刻断开，浏览器侧表现为 `Connection reset`／Redis client 断流。

### 根因

默认网关优先级下，新接入的网络会参与默认路由选择；已建立连接的源地址／路由一旦变化就不再匹配，内核侧连接被重置。重连或重置连接池只是掩盖症状。

### 规避

运行中依赖接入临时网络时显式使用较低优先级：`docker network connect --gw-priority -1 <network> <container>`，保留原网关与既有连接；移除时同样按身份核验。Workspace 和 Identity 的装配 helper 都使用这项规则，见[模块边界定向验收](../testing#module-boundary-acceptance)。

## 5. Docker 默认地址池耗尽

### 症状

`docker network create` 报 `could not find an available, non-overlapping IPv4 address pool among the defaults`，后续容器无法启动；反复重试无效。

### 根因

默认地址池被历史验收网络占满：每次验收都建专属 bridge 网络，而失败运行留下的网络没有按身份退役。

### 规避

验收网络一律带 owner 标签创建（`org.aap.acceptance` / `org.yoao.worktree-owner` 与唯一 id），创建后 `docker network inspect` 必须唯一并核对 IPAM 的 subnet／gateway，再由同一 owner 精确删除，例如 `scripts/verify_site_agent.py` 的资源台账与 `apps/api/tests/test_workspace_video_tracker_worker_live.py` 的 `aap-tracker-fixture-*` 桥。不要改装 daemon 的默认地址池来绕开泄漏；先按标签核对"哪些网络属于已退役运行"。`scripts/run_raster_fault_e2e.py` 的 `verify_store_physical()` 还要求 bridge 网络上只有本运行的容器，避免复用到邻居网络。

## 6. PID 受限容器里的 OpenBLAS 线程

### 症状

worker 容器启动后立刻以 139（SIGSEGV）退出，任务尚未创建；同一镜像在其他机器正常。

### 根因

OpenBLAS 默认按宿主 CPU 数创建线程。在单 CPU、64 PID 之类的受限容器中，线程创建超出 PID 预算，进程崩溃退出，看起来像应用段错误。

### 规避

给 worker／CLI 显式设置 `OPENBLAS_NUM_THREADS=1`，不放宽容器权限或资源上限。见 `apps/api/tests/workspace_import_cli_helpers.py`（"Match the one-CPU/64-PID container, not the host's BLAS thread count"）、`apps/api/tests/workspace_worker_assembly_helpers.py`；`apps/api/tests/identity_module_assembly_helpers.py` 还会在验收前断言该变量为 `1`。

## 7. Uvicorn 优雅关闭后的 `-15`

### 症状

向 API 进程发 SIGTERM 后，日志显示 lifespan shutdown 已完成，但进程退出码为 `-15`（或安装版 Uvicorn 把信号重抛）；以"退出码必须为 0"为断言的探针误报失败。

### 根因

Uvicorn 在排空后重放关闭信号，安装版本可能因此以负信号码退出。退出码本身不区分"优雅关闭后重放信号"与"被强杀"。把退出码当唯一成功判据，会把真实成功记成失败，也会把强杀误记成成功。

### 规避

成功以 lifespan 关闭完成＋后代排空为准。`scripts/raster_api_process.py` 只在观察到 `lifespan.shutdown.complete`、没有 `force_exit` 且无 lifespan 错误时返回 0；`scripts/worktree_resources.py` 结算原生运行时要求 API disposition 的 `lifespan_shutdown` 与 `descendants_drained` 均为 `True`，并把 `exit_code = -15` 当作损坏样本（`scripts/test_worktree_resources.py`）。诊断时先看 disposition 字段，再看退出码。

## 8. `mc` 退出码 0 也可能是拒绝

### 症状

容器内 `mc --json admin user info …` 返回 0，脚本继续执行，但对象或 IAM 实际没有变化；或把 `Access Denied` 当成"资源不存在"。

### 根因

`mc` 的 JSON 模式把错误写进 `status`／`error.cause.error.Code`，退出码在部分路径上仍为 0。

### 规避

解析结构化输出，不只判退出码：`scripts/fixture_store.py` 的 `_native_iam_info()` 要求 `returncode == 0 and value["status"] == "success"` 才认可成功，拒绝路径要求 `status == "error"` 且错误码精确匹配；`scripts/fixture_native_business.py` 的 `_mc_output()` 同样在"资源应缺失"时校验 `status` 与 `XMinioAdminNoSuch*` 码。不要在 `sh` 里用 `grep -q` 猜输出。

## 9. `AF_UNIX path too long`

### 症状

authority／relay 启动即失败，报 `OSError: AF_UNIX path too long`，尚未进入浏览器或故障注入。

### 根因

Unix domain socket 路径上限约 108 字节。工作树自带的 `TMPDIR` 本身很长，再拼接子目录与 socket 文件名就溢出。

### 规避

socket 目录固定在短路径 `/tmp` 下并校验归属：`scripts/fixture_inference_services.py` 只在 `Path("/tmp")` 的直接子目录（名字匹配受限正则）中创建控制目录，`scripts/run_raster_fault_e2e.py` 用 `tempfile.mkdtemp(prefix="aap-raster-", dir="/tmp")` 建 relay socket 目录并记录目录身份。不要通过加长工作树路径或放开校验绕过。

## 10. 容器访问宿主 loopback

### 症状

容器内用 `host.docker.internal`（或 `--add-host=host.docker.internal:host-gateway`）访问宿主进程的端口被拒绝、`ConnectionRefusedError`，宿主本机访问同一端口正常。

### 根因

宿主服务只监听 `127.0.0.1`，容器到 host-gateway 的连接源地址是 bridge 网关，不属于 loopback；不同 bridge 的网关也不通用。

### 规避

服务要绑定／发布到自有 bridge 的网关地址，而不是回环。`scripts/run_raster_fault_e2e.py` 的 `fixture_provider_host()` 只把核验过的私有 bridge 网关（IPAM 唯一、非 loopback、非 unspecified）通告给提供方；`scripts/verify_docker_host_forwarding.py` 用于诊断宿主路由与 TCP 元组复用。ML Backend 注册侧的 loopback 校验与 `ML_BACKEND_STORAGE_HOST` 见 [容器网络与 loopback 限制](./container-networking)。

## 11. 继承的 CI=true

### 症状

本地浏览器验收跑到一半被统一期限截断，夹具收尾被中断，报告里出现大量"已完成但与未完成混杂"的条目。

### 根因

`apps/web/playwright.config.ts` 以 `Boolean(process.env.CI)` 决定 `globalTimeout: 15 * 60_000`、`maxFailures: 1`、`retries: 1` 与 `preview` 模式；开发工具 shell（或宿主的 CI 变量）里残留的 `CI=true` 会被 `scripts/worktree_env.py` 的 `frontend_environment()` 原样透传给浏览器进程，于是本地运行套用了 CI 分片预算。运行入口见 [E2E README](https://github.com/YOAO-Labs/ai-annotation-platform/blob/dev/apps/web/e2e/README.md)。

### 规避

本地资格运行前显式清除 `CI` 与 `GITHUB_ACTIONS`，再启动浏览器入口；`scripts/run_raster_fault_e2e.py` 按 `os.environ.get("CI")` 决定重试次数，也能显示该变量是否泄漏。不要把 15 分钟总期限当成产品超时来加长。

## 12. 固定期限内的整库恢复

### 症状

夹具或冷消费者用例在限制时间内执行整库 `pg_restore`，触及期限后失败，随后清理因 clone 仍有活动连接而被拒绝。

### 根因

逐语句恢复在固定期限内无法完成（大表索引创建阶段尤其慢），超时后连接仍活跃，销毁合同按身份拒绝清理，现场被保留。

### 规避

原生 `pg_restore` 使用 `--single-transaction`（配合 `--exit-on-error`）：恢复要么整体提交、要么整体回滚，失败不会留下半成品与活动会话。现有实现见 `apps/api/tests/test_identity_recovery.py`（`_postgres_tool` 自动追加该参数）、`apps/api/tests/test_video_restore_native.py` 与 `apps/api/tests/test_identity_module_assembly_live.py`。超时后必须按身份核对遗留 clone 与连接，不能直接删容器。

## 13. Playwright `gracefulShutdown`

### 症状

浏览器套件结束后，日志出现 Web 进程组被 SIGTERM 终止、esbuild 子进程被杀；整体退出被判定为失败。

### 根因

Playwright 通用的 `webServer.gracefulShutdown` 向整个 Web 进程组发信号，dev 服务器派生的子进程（esbuild）一并被信号终止，无法区分「正常收尾」与「被接管的后代」。

### 规避

自有运行时不经由进程组信号收尾，而是显式关闭：`scripts/raster_web_process.py` 通过 stdin 控制管道发 `close` 命令、等待 pidfd 退出，并核对退出码与后代排空；`apps/web/scripts/raster-vite.mjs` 只在 `await server.close()` 完成后才允许 owner 退出，丢失控制管道即失败。失败时保留准确身份（`_unsettled_web_servers`），不按进程名忽略。

## 14. 后一个浏览器项目清理前一个项目的 trace

### 症状

若两个阶段共用同一个输出目录，Raster 产品用例报告中的 trace 路径在源码级 Worker 项目结束后可能已不存在。

### 根因

两个 Playwright 配置若共用默认 `test-results`，后启动的项目会清理其输出目录。`preserveOutput: "always"` 不阻止下一次运行清理相同目录。

### 规避

源码级 Worker 使用独立的 `test-results/browser-integration`，保留前一阶段产品用例的失败诊断；CI 继续递归收集 `test-results`。不同项目不要用共同的 `--output` 合并目录。分别保存两份报告及实际生成的失败附件，不把报告中的路径当作附件存在的证明。

## 15. 8K 编辑通过，但 Native 高清瓦片被 CORS 阻止

### 症状

工作台的 `data-image-ready=true`、8K Mask 编辑／保存／重载断言通过，预览图也能显示，但浏览器 `fetch` 高清瓦片报告缺少 `Access-Control-Allow-Origin`。不能据此判定 Native 图像读取通过。

### 根因与边界

私有逐 run 的 `fixture_native_output_proxy.py` 原先没有浏览器跨域响应。预览图与瓦片不是同一读取方式；金字塔 API 返回 ready 和完整 manifest，也不等于浏览器已经读取、解码实际瓦片。

该代理使用精确到 run／write ID、带到期时间的签名 capability，不使用浏览器 cookie 作为权限。CORS 仅支持非凭据 GET／HEAD、Range／If-Range，并暴露读取所需的范围与对象版本响应头；允许跨源消费不跳过签名、逐成员身份、读取许可或物理关闭校验。子进程仍保留最小环境，不为 CORS 导入主 API 的环境配置。

### 验收

首次打开与重载后，都必须观察当前路由存在可见瓦片、已解码保留字节为正、目标覆盖率为 1，且瓦片错误和可见瓦片重试均为 0；浏览器 console error／page error 也应使该 8K 用例失败。不能仅等待 `image-ready` 或检查 Mask 自身的内存预算。

CORS 配置位于 `scripts/fixture_native_playwright.py` 的 `_proxy_child`；逐成员读取与物理关闭仍由 `fixture_native_output_proxy.py` 负责。

## 诊断 macOS 宿主机转发 {#host-forwarding-diagnostics}

使用本机已有的不可变 Docker Python 镜像。镜像须有 `/opt/venv/bin/python`，并可由 UID `10003` 运行。从仓库根目录执行：

```bash
python3 scripts/verify_docker_host_forwarding.py \
  --image "$HOST_FORWARDING_IMAGE_ID" \
  --output .worktree/host-forwarding-diagnosis.json
```

探针只创建自己的临时网络、容器和回环监听器，不连接业务数据库。结果 `diagnosed_not_repaired` 和非零退出码表示复现问题，不表示修复。需要本机 TCP 元数据时，先在本机交互终端 `sudo -v`，再加 `--host-trace` 并选用新的报告路径。不要在命令或日志中传入管理员口令。证据及限制见[宿主机转发台账](../concepts/module-deployment#runtime-host-forwarding)。

## 相关

- 代码：`scripts/linux_pidfd.py`、`scripts/worktree_runtime.py`、`scripts/run_raster_fault_e2e.py`、`scripts/raster_web_process.py`、`apps/web/scripts/raster-vite.mjs`
- 相邻条目：[CI 服务依赖踩坑](./ci-flaky-services)、[容器网络与 loopback 限制](./container-networking)、[Docker rebuild vs restart](./docker-rebuild-vs-restart)

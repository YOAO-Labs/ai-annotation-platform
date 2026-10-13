# 仓库优化 P9 域执行证据：pointcloud 与 video-pipeline（共享构建产物预览）

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P9 同候选全域执行的本工作线程）
> 候选：`dc972be08a42855c836023da178487469aae2fc9`（分支 `worktree-agent-opt-p9-domains`，自候选建立，未 reset）
> 本线程范围：仅 `pointcloud` 与 `video-pipeline` 两个软件域；其余 10 个 suite 由 P9 另一 worker 执行
> 证据图例：**[V]** 实际执行；**[FAIL]** 实际失败（已保留证据）；**[GAP]** 未执行/边界
>
> **P10 收尾（2026-09-21）**：本文件记录的是 `dc972be08` 候选上的**历史失败**（pointcloud 37P/1F、video-pipeline 43P/20skip + teardown exit 1）；冻结 `898` 与集成根的最终状态见 [43](./43-repository-optimization-p9-shadow-comparison.md)，本文件不再更新。

## 1. 构建产物实际复用（未重建）**[V]**

| 项             | 值                                                                                                                           |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 共享产物       | `/tmp/opencode/web-e2e-dist-253b54a03c4f0fc663b26cd9038913bce188e6b7f772de67dad1a5ec616015e1.tar.gz`                         |
| SHA256（实测） | `754d61b997f69491520245dabe714b5f60102edd4d44bc770d80d54027ab913f`，与 `/tmp/opencode/SHA256SUMS` 第 1 条完全一致            |
| 指纹           | `253b54a03c4f0fc663b26cd9038913bce188e6b7f772de67dad1a5ec616015e1`（与归档名一致）                                           |
| 解包           | `tar -xzf <archive> -C apps/web` → `apps/web/dist`（270 文件；清单聚合 sha256 `67c6cf53…`）                                  |
| 复用证明       | `dist/index.html` sha256 `f31c3ecb…` 与归档内同名成员逐字节一致；`playwright.preview.e2e.config.ts` 无任何 `vite build` 步骤 |
| 预览入口       | `pnpm preview`（vite preview）静态服务解包 dist，`API_PROXY_TARGET` 指向自有 API                                             |

预览配置只覆盖 base config 的 `webServer`：API 仍用 worktree 自有 e2e 命令，Web 由 `dev` 改为 `preview`；项目、testDir/testMatch、grepInvert、retries、reporter 与全部 AAP 安全变量沿用 base。运行时仅端口等环境变量不同（本机自有端口 api 8102 / web 3100）。

## 2. 执行命令与结果 **[V]/[FAIL]**

经自有 e2e 模式 `aap_wt_de57dbd75d11794b_e2e`（新建一次性 DB `aap_wt_de57dbd75d11794b_e2e`、自有 Redis 与 8 个自有 bucket；`--config=playwright.preview.e2e.config.ts --retries=1`，workers=1，严格资格标志未设置，退出码经 `pipefail`/`PIPESTATUS` 采集）：

| suite          | 命令                                                            | 退出 | expected | unexpected | skipped | flaky | duration |
| -------------- | --------------------------------------------------------------- | ---- | -------- | ---------- | ------- | ----- | -------- |
| pointcloud     | `--project=pointcloud e2e/tests/workbench-pointcloud-*.spec.ts` | 1    | 37       | 1          | 0       | 0     | 289.4s   |
| video-pipeline | `e2e/tests/video-*.spec.ts e2e/tests/workbench-video-*.spec.ts` | 1    | 43       | 0          | 20      | 0     | 389.4s   |

- **pointcloud 失败（唯一用例失败）**：`e2e/tests/workbench-pointcloud-tools.spec.ts:379 'point-mask 多边形双击优先完成绘制，不触发框聚焦'`，retry0 与 retry1 均在 **fixture teardown** 失败：`seed/owned-cleanup failed: 500 {"code":"e2e_seed_cleanup_incomplete","residuals":{"users":3,"projects":2}}`（栈：`fixtures/seed.ts:571 cleanupOwned → :582 cleanupOwnedFixtures → :1012`）。非测试体/渲染器断言失败。
- **video-pipeline**：0 用例失败；20 个 skip 为**配置性有意跳过**（注解 `requires native Mask API writes`，`expectedStatus=skipped`），未削弱或改写任何 skip/断言。
- 两个 suite 的退出码 1 均来自 **globalTeardown 的同族 owned-cleanup 残余**（video：`users:4, projects:0`；pointcloud：`users:3, projects:0`）。

## 3. 只读残余与根因线索 **[V]**

对保留模式执行一次**只读 SELECT**（无任何写入/清理）：

- 命名空间：`cqbvntnn9aoq`
- 残留用户：`admin-cqbvntnn9aoq@e2e.test`、`anno-cqbvntnn9aoq@e2e.test`、`rev-cqbvntnn9aoq@e2e.test`，以及**非命名空间**账号 `takeover@e2e.test`
- 残留项目：`4f44c66c…  E2E Lidar cqbvntnn9aoq`、`d83c8158…  E2E Owned cqbvntnn9aoq`（owner 均为 admin-cqbvntnn9aoq）

与 cleanup 500 的 residuals 数量吻合；`takeover@e2e.test` 不在命名空间内，疑似清理范围缺口，留给诊断线程判定。本线程不做源码修复、不使用全局管理员/reset 绕过。

## 4. 交付物与保留状态

- 套件状态 JSON（供 P9 owner）：`/tmp/aap-p9-domain-status/pointcloud.status.json`、`video-pipeline.status.json`、`p9-domains.aggregate.json`（含候选/产物指纹/退出/stats/firstFailure/只读残余）。
- 原始 JSON 保留在 `/tmp/aap-p9-domain-raw/`，模式与 DB 保留供诊断（term 2726）只读取证；按指示在 P9/diagnoser ACK 前不清理、不销毁。
- 共享产物归 P9 owner；本线程未删除，清理前须先协调。

## 5. 边界

无性能/资格声明；未设置严格 WebGPU/WebCodecs 标志（非本矩阵要求）；未做单元/后端/文档等宽泛复跑；未 push。远程 CI 未运行。

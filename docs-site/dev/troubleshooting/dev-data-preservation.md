---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# E2E 测试数据污染与隔离

## 症状

开发库 `annotation` 中出现 `@e2e.test` 用户、`E2E Demo Project` 或全局
`E2E SAM Mock`；又或者运行 `pnpm test:e2e` 后发现开发数据发生变化。

## 当前隔离模型

本地 Playwright 不应连接开发进程或开发库：

| 用途           |      Web |      API | 数据库                     |
| -------------- | -------: | -------: | -------------------------- |
| 日常开发       |   `3000` |   `8000` | `annotation`               |
| Playwright E2E | 保留端口 | 保留端口 | 工作树自有 `yoao_wt_*_e2e` |

从仓库根目录运行 `pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e`；
启动器核验独立资源身份并分配保留端口，Playwright 启动专用 Web/API。
不能仅靠 `PLAYWRIGHT_E2E_DATABASE_URL` 或库名后缀证明资源归属。

## Seed 路由的三重门禁

`/api/v1/__test/seed/*` 会创建、修改或删除固定测试数据，因此必须同时满足：

1. 进程不是 `production`；
2. 进程显式设置 `E2E_SEED_ENABLED=true`；
3. 当前数据库会话的 `current_database()` 以 `_e2e` 或 `_test` 结尾。

缺少开关时路由不挂载；库名不合规时所有 seed/login/cleanup 操作都被
拒绝。production 即使设置开关也不挂载路由。不要在公共 staging 进程中
开启这个开关。

本地夹具通道还要求启动器提供的规范资源根、identity 和资源清单与数据库
owner 标记及受限 runtime LOGIN 一致；仅满足上述路由门禁不足以认证资源。

## Cleanup 是兜底，不是隔离

`reset` 会先清理固定 E2E 资源再重建 fixture；`POST /api/v1/__test/seed/cleanup`
可幂等删除这些资源。Playwright 的 `globalTeardown` 会在正常结束时调用 cleanup。

强制中断、进程崩溃或 `SIGKILL` 可能使 teardown 来不及执行。残留必须按已核验
的工作树 `e2e` 资源及对应夹具归属排查；不能因为存在 teardown 就把 E2E
指向开发库，也不能删除无法确认归属的数据。

## 排查步骤

1. 确认命令由 `dev:worktree -- exec --mode e2e` 启动，并核对该 mode 的资源根、
   identity、清单和数据库 owner 标记。
2. 确认 owner 夹具与受限 API runtime 的连接指向同一已核验数据库，账号角色各司其职。
3. 检查启动器分配的 Web/API 端口，不复用已在运行的开发服务。
4. [按需截图与录屏](../how-to/update-screenshots)也使用获授权的隔离环境和可丢弃数据，不借用日常开发库或另一套 E2E 环境。

如果开发库中已经存在历史 E2E 残留，新门禁会阻止 cleanup 连接该库。
先备份并核对资源所属，再由维护者对明确的历史 fixture 做一次性定向清理；
不要为清理方便而放宽库名守卫。

## 相关

- E2E 运行：`apps/web/e2e/README.md`
- Playwright 配置：`apps/web/playwright.config.ts`
- Seed 路由：`apps/api/app/api/v1/_test_seed.py`
- 路由挂载：`apps/api/app/api/v1/router.py`
- 专用库准备：`apps/api/scripts/prepare_e2e_db.py`

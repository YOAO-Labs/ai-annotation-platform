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

| 用途                |    Web |    API | 数据库                        |
| ------------------- | -----: | -----: | ----------------------------- |
| 日常开发            | `3000` | `8000` | `annotation`                  |
| Playwright E2E      | `3001` | `8010` | `annotation_e2e`              |
| 本地截图 / 视觉回归 | `3001` | `8010` | `annotation_screenshots_test` |
| 视觉回归 CI         | `3000` | `8000` | `annotation_screenshots_test` |

`pnpm test:e2e` 会幂等创建 `annotation_e2e`、执行迁移，再启动专用 Web/API。
`reuseExistingServer` 保持关闭，所以 `3001/8010` 被占用时测试直接失败，不会
静默改用 `3000/8000`。可用 `PLAYWRIGHT_E2E_DATABASE_URL` 覆盖默认测试库，
但库名必须以 `_e2e` 或 `_test` 结尾。

## Seed 路由的三重门禁

`/api/v1/__test/seed/*` 会创建、修改或删除固定测试数据，因此必须同时满足：

1. 进程不是 `production`；
2. 进程显式设置 `E2E_SEED_ENABLED=true`；
3. 当前数据库会话的 `current_database()` 以 `_e2e` 或 `_test` 结尾。

缺少开关时路由不挂载；库名不合规时所有 seed/login/cleanup 操作都被
拒绝。production 即使设置开关也不挂载路由。不要在公共 staging 进程中
开启这个开关。

## Cleanup 是兜底，不是隔离

`reset` 会先清理固定 E2E 资源再重建 fixture；`POST /api/v1/__test/seed/cleanup`
可幂等删除这些资源。Playwright 的 `globalTeardown` 会在正常结束时调用 cleanup。

强制中断、进程崩溃或 `SIGKILL` 可能使 teardown 来不及执行。这时残留只会留在
`annotation_e2e`，下次 reset/cleanup 会重新收敛。不能因为存在 teardown 就把
E2E 指向开发库。

## 排查步骤

1. 查看 Playwright 进程的 `PLAYWRIGHT_E2E_DATABASE_URL`，未设置时应使用
   `annotation_e2e`。
2. 确认 API 进程的 `DATABASE_URL` 与迁移、建库步骤完全一致。
3. 确认本地 E2E 连接 `3001/8010`，而不是已在运行的开发服务。
4. 截图自动化单独核对 `annotation_screenshots_test`，不要复用
   `annotation_e2e` 或 `annotation`。

如果开发库中已经存在历史 E2E 残留，新门禁会阻止 cleanup 连接该库。
先备份并核对资源所属，再由维护者对明确的历史 fixture 做一次性定向清理；
不要为清理方便而放宽库名守卫。

## 相关

- E2E 运行：`apps/web/e2e/README.md`
- Playwright 配置：`apps/web/playwright.config.ts`
- Seed 路由：`apps/api/app/api/v1/_test_seed.py`
- 路由挂载：`apps/api/app/api/v1/router.py`
- 专用库准备：`apps/api/scripts/prepare_e2e_db.py`

---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# 发布流程

## 版本号

[Semantic Versioning](https://semver.org/lang/zh-CN/) `MAJOR.MINOR.PATCH`：

- **MAJOR** — 破坏性 API 变更
- **MINOR** — 新功能（向后兼容）
- **PATCH** — 修 bug

`CHANGELOG.md` 是应用版本号的唯一真值。每次发布都要在同一个 release commit 中同步：

- `apps/api/app/config.py` 的 `app_version`
- `apps/api/pyproject.toml` 的 `project.version`
- `apps/web/package.json` 的 `version`

`apps/api/uv.lock` 由 `uv lock` 刷新；`apps/api/openapi.snapshot.json` 由 OpenAPI 生成流程刷新，不手改。

## 流程

1. 在发布分支同步代码与依赖，运行完整本地门禁：
   ```bash
   pnpm install
   pnpm codegen
   pnpm format:check
   pnpm test
   pnpm typecheck
   pnpm lint
   pnpm build:web
   cd apps/api
   DATABASE_URL="$RELEASE_TEST_DATABASE_URL" \
   TEST_DATABASE_URL="$RELEASE_TEST_DATABASE_URL" \
   REDIS_URL="$RELEASE_TEST_REDIS_URL" \
   TEST_REDIS_URL="$RELEASE_TEST_REDIS_URL" \
   PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider
   ```
   `RELEASE_TEST_DATABASE_URL` 必须指向当次创建的临时数据库，`RELEASE_TEST_REDIS_URL` 必须指向空的专用 Redis DB。
2. 把 `CHANGELOG.md` 的 `Unreleased` 折叠为当次日期版本，并在顶部留一个新的空 `Unreleased`。条目按 Keep a Changelog 的 `Added`、`Changed`、`Deprecated`、`Removed`、`Fixed`、`Security` 分类，空类别省略。
3. 同步三个手工版本源，然后刷新 lock 和 OpenAPI：
   ```bash
   cd apps/api && uv lock && cd ../..
   pnpm openapi:export
   pnpm openapi:check
   ```
   OpenAPI 在发布变更中只允许 `info.version` 不同。
4. 运行文档影响、文档构建、bundle size 和仓库当时配置的全部 required CI checks。测试数据库必须是专用临时库，不得对开发库执行 Alembic downgrade。
5. 提交 release commit 并推送 PR；新 HEAD 的 required checks 全部通过后 merge。
6. 在最终进入 `main` 的 commit 上打 tag，不在 feature branch 提前打 tag：
   ```bash
   git tag v<version> <main-release-commit>
   git push origin refs/tags/v<version>
   ```
7. 刷新运行栈，确认 `/health` 报告当次版本，Celery worker 加载新代码，GPU release latch 与 effective mode 没有被发布步骤改变。
8. （可选）GitHub Release 复用 CHANGELOG 当次段落。

## CHANGELOG 风格

参考现有条目（`/CHANGELOG.md`）：每个版本写明改了什么以及用户为什么会在意。**不要**只写一行「升级版本」，也不新增 Keep a Changelog 之外的分组。

## 数据库迁移

任何会动 schema 的版本，CHANGELOG 必须包含「迁移」小节，说明 alembic 版本号与回滚策略。

## 文档同步

发布前确认：

- [ ] CHANGELOG 顶部最新版本段落
- [ ] API、Web 与 lock 的版本副本一致
- [ ] `apps/api/openapi.snapshot.json` 与代码一致（`pnpm openapi:check`）
- [ ] `docs-site/` 中受影响的页面已更新
- [ ] `pnpm docs:impact` 与文档构建通过
- [ ] ADR：如有架构决策，新增 `docs/adr/0XXX-*.md`

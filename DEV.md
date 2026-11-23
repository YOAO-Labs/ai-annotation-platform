# 开发指南

这是仓库的开发命令速查。首次搭建按[本地开发教程](docs-site/dev/tutorials/local-dev.md)安装依赖、核验 Plugin-SDK 制品并初始化 checkout。部署、权限和故障排查请查阅对应专题。

## 启动

- **主 checkout**：新环境按[本地冷启动](docs-site/ops/deploy/development.md#development-cold-start)依次准备基础设施、数据库迁移和受限角色，再启动 API、Web 和 worker。不要直接启动未授权的 worker。
- **独立工作树**：完成 setup 后，在工作树根目录运行 `pnpm dev:worktree`。需要后台任务时运行 `pnpm dev:worktree -- up --with-worker`。诊断使用 `pnpm dev:worktree -- doctor`，端口以启动器实际输出为准。见[工作树隔离与清理](docs-site/dev/how-to/worktree-environments.md)。
- **演示数据**：按[演示数据与训练夹具](docs-site/dev/tutorials/local-dev.md)准备。先核对数据库和对象存储，自动化测试使用独立测试库。

改动挂载的 Celery 源码后，重启受影响的消费者；依赖或镜像构建输入改变时需要重建。修改运行配置、应用迁移或使用定时器时，按[开发部署手册](docs-site/ops/deploy/development.md)选择对应步骤。

## 提交前检查

按改动范围选择命令，不用在日常开发库运行会迁移或写数据的测试。

- 静态检查：`pnpm lint`、`pnpm format:check`。
- Web：`pnpm test`、`pnpm typecheck`。首次直接运行类型检查前用 `pnpm codegen`；`pnpm dev:web` 与 `pnpm build:web` 启动前会按需生成。
- 后端集成：`pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/...'`。将 `tests/...` 替换为本次要验证的文件。
- 浏览器 E2E：`pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e`。专项入口见 [Playwright 运行说明](apps/web/e2e/README.md)。
- 契约：API 变更运行 `pnpm openapi:export` 和 `pnpm codegen`，提交受跟踪快照。数据库变更按 [Alembic 迁移指南](docs-site/dev/how-to/add-migration.md)执行。
- 文档：`pnpm docs:dev` 预览，`pnpm docs:build` 检查站点构建，`pnpm --filter docs-site check:readability` 检查速查页。

测试隔离、模块定向用例及证据要求见[测试指南](docs-site/dev/testing.md)。

## 按主题查阅

- **代码与配置**：[仓库地图](docs-site/dev/concepts/repository-map.md)、[API 参考](docs-site/api/index.md)、[设计系统](docs-site/dev/reference/design-system.md)、[环境变量](docs-site/dev/reference/env-vars.md)。
- **模块与权限**：[默认进程与模块台账](docs-site/dev/concepts/module-deployment.md)、[同库事务数据接口](docs-site/api/guides/module-data.md)、[旧拓扑升级](docs-site/ops/upgrade-guide.md)。
- **部署与排障**：[Compose 部署](docs-site/ops/deploy/docker-compose.md)、[局域网部署](docs-site/ops/deploy/lan-production.md)、[故障排查](docs-site/dev/troubleshooting/index.md)。
- **数据集与存储**：[来源与增量同步](docs-site/dev/concepts/dataset-sources-and-sync.md)、[存储迁移](docs-site/ops/deploy/storage-migration-inventory.md)、[备份与恢复](docs-site/ops/deploy/storage-recovery.md)。
- **推理与训练**：[ML Backend 协议](docs-site/dev/reference/ml-backend-protocol.md)、[训练执行器部署](docs-site/ops/deploy/docker-compose.md)、[实例分割验收](docs-site/dev/how-to/validate-segmentation-online-learning.md)。
- **文档维护**：[编写与维护文档](docs-site/dev/how-to/write-documentation.md)、[按需界面验收与媒体](docs-site/dev/how-to/update-screenshots.md)。历史决策与验证记录查阅 [ADR](docs/adr/README.md) 和[开发计划](docs/plans/README.md)。

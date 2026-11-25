# 开发指南

这是仓库的开发命令速查。首次搭建按[本地开发教程](https://yoao-labs.github.io/YOAO-Docs/dev/tutorials/local-dev)安装依赖、核验 Plugin-SDK 制品并初始化 checkout。部署、权限和故障排查请查阅对应专题。

## 启动

- **主 checkout**：新环境按[本地冷启动](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/development#development-cold-start)依次准备基础设施、数据库迁移和受限角色，再启动 API、Web 和 worker。不要直接启动未授权的 worker。
- **独立工作树**：完成 setup 后，在工作树根目录运行 `pnpm dev:worktree`。需要后台任务时运行 `pnpm dev:worktree -- up --with-worker`。诊断使用 `pnpm dev:worktree -- doctor`，端口以启动器实际输出为准。见[工作树隔离与清理](https://yoao-labs.github.io/YOAO-Docs/dev/how-to/worktree-environments)。
- **演示数据**：按[演示数据与训练夹具](https://yoao-labs.github.io/YOAO-Docs/dev/tutorials/local-dev)准备。先核对数据库和对象存储，自动化测试使用独立测试库。

改动挂载的 Celery 源码后，重启受影响的消费者；依赖或镜像构建输入改变时需要重建。修改运行配置、应用迁移或使用定时器时，按[开发部署手册](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/development)选择对应步骤。

## 提交前检查

按改动范围选择命令，不用在日常开发库运行会迁移或写数据的测试。
排查 PR 等待时间时，先看 `CI scope` 选了哪些检查，再区分最长任务的排队时间和执行时间。

- 静态检查：`pnpm lint`、`pnpm format:check`。
- Web：`pnpm test`、`pnpm typecheck`。首次直接运行类型检查前用 `pnpm codegen`；`pnpm dev:web` 与 `pnpm build:web` 启动前会按需生成。
- 后端集成：`pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/...'`。将 `tests/...` 替换为本次要验证的文件。
- 浏览器 E2E：`pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e`。专项入口见 [Playwright 运行说明](apps/web/e2e/README.md)。
- 契约：API 变更运行 `pnpm openapi:export` 和 `pnpm codegen`，提交受跟踪快照。数据库变更按 [Alembic 迁移指南](https://yoao-labs.github.io/YOAO-Docs/dev/how-to/add-migration)执行。
- 文档：产品与开发文档站已迁移至独立仓库 `YOAO-Labs/YOAO-Docs`（[在线文档](https://yoao-labs.github.io/YOAO-Docs/)）；本仓库保留 `README.md`、`DEV.md`、`docs/adr/`、`docs/plans/`、`CHANGELOG.md` 与 `ROADMAP/`，可用 `node scripts/check-dead-links.mjs docs` 检查仓内链接。

测试隔离、模块定向用例及证据要求见[测试指南](https://yoao-labs.github.io/YOAO-Docs/dev/testing)。

## PR 检查

`CI scope` 比较 PR 基线与实际测试的合并提交。选中 `CI affected` 时，摘要列出本次要跑的检查。纯文档不启动应用；训练列表与占比图跑对应的组件用例和 Web 类型检查；审计月报纯计算只运行 `tests/test_audit_summary.py`，不启动数据库。改公共接口、共享夹具、依赖、CI 规则或未登记的代码会回退到完整检查。平台仓库不构建已迁出的文档站。

`ci/**` 集成分支上的普通 PR 只跑受影响检查。向 `dev` 提交时，先看独立的快速结果，完整 CI 在对照期仍运行。合并队列始终在候选 SHA 上跑完整 CI。仅在实测失败会阻止合并、成功的完整候选可以合并后，维护者才可设置仓库变量 `CI_AFFECTED_ONLY=true`。若 `dev` 的有效规则无法确认强制队列与 GitHub Actions 的 `CI verdict`，仍保留完整 CI。重跑请选择 **Re-run all jobs**，不把旧 attempt 的通过结果当作本次证据。

普通 PR 的十分钟从 GitHub 收到更新算到 `CI verdict` 完成，包含排队和安装。单次 `CI affected` 通过只证明该提交选中的检查；完整门禁和正式达标样本分开计时。

完整 CI 让四个 API 分片复用经清单校验的 runtime wheel；工作树与维护用例单独并行，不在各片重复。复现第 3 片时，在隔离测试环境的 `apps/api` 中运行 `uv run pytest -q --shard-index 3 --shard-count 4 --shard-plan tests/ci-shard-plan.json`。pytest 和 Vitest 默认不采集覆盖率。

## 按主题查阅

- **代码与配置**：[仓库地图](https://yoao-labs.github.io/YOAO-Docs/dev/concepts/repository-map)、[API 参考](https://yoao-labs.github.io/YOAO-Docs/api/)、[设计系统](https://yoao-labs.github.io/YOAO-Docs/dev/reference/design-system)、[环境变量](https://yoao-labs.github.io/YOAO-Docs/dev/reference/env-vars)。
- **模块与权限**：[默认进程与模块台账](https://yoao-labs.github.io/YOAO-Docs/dev/concepts/module-deployment)、[同库事务数据接口](https://yoao-labs.github.io/YOAO-Docs/api/guides/module-data)、[旧拓扑升级](https://yoao-labs.github.io/YOAO-Docs/ops/upgrade-guide)。
- **部署与排障**：[Compose 部署](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/docker-compose)、[局域网部署](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/lan-production)、[故障排查](https://yoao-labs.github.io/YOAO-Docs/dev/troubleshooting/)。
- **数据集与存储**：[来源与增量同步](https://yoao-labs.github.io/YOAO-Docs/dev/concepts/dataset-sources-and-sync)、[存储迁移](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/storage-migration-inventory)、[备份与恢复](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/storage-recovery)。
- **推理与训练**：[ML Backend 协议](https://yoao-labs.github.io/YOAO-Docs/dev/reference/ml-backend-protocol)、[训练执行器部署](https://yoao-labs.github.io/YOAO-Docs/ops/deploy/docker-compose)、[实例分割验收](https://yoao-labs.github.io/YOAO-Docs/dev/how-to/validate-segmentation-online-learning)。
- **文档维护**：[编写与维护文档](https://yoao-labs.github.io/YOAO-Docs/dev/how-to/write-documentation)、[按需界面验收与媒体](https://yoao-labs.github.io/YOAO-Docs/dev/how-to/update-screenshots)。历史决策与验证记录查阅 [ADR](docs/adr/README.md) 和[开发计划](docs/plans/README.md)。

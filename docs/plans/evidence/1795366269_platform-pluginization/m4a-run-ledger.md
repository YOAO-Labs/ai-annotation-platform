# M4a 运行账本：内置模块统一声明式装配

记录日期：2026-11-24
对应计划：[平台插件化计划](../../1795366269_platform-pluginization.md) §4 M4 / M4a
架构文档：[后端分层](../../../../docs-site/dev/concepts/backend-layers.md)

## 1. 交付范围

- **声明式装配包**（`apps/api/app/assembly/`）：
  - `contracts.py`：纯冻结数据契约（`BuiltinModuleManifest`、`RouterMount`、`BeatScheduleSpec`、`CronSpec`、`MigrationSurface`、`ComposeOverlaySpec`）与合法枚举集合（`VALID_ROUTER_SURFACES`、`VALID_COMPOSE_TIERS`），零 FastAPI／SQLAlchemy／Celery 顶层导入。
  - `modules/`：11 个内置模块清单（`platform`、`identity`、`workspace`、`assets`、`labeling`、`inference`、`learning`、`jobs`、`notifications`、`audit`、`insights`）。
  - `registry.py`：`BUILTIN_MODULES` 权威注册表与导入期即执行的冲突校验（防重复模块／owner 包／跨 surface 路由／任务／定时作业／ORM 模型／原生表／Compose 文件，以及非法 surface／tier、空 schema／表名与未声明任务目标）。
  - `builders.py`：纯装配器（共享 `_mount_routers` 驱动 `mount_api_v1_routers` 与 `mount_root_routers`，以及 `collect_celery_includes`、`build_beat_schedule`、`load_declared_orm_models`、`collect_native_managed_tables`、`collect_native_managed_relations`、`list_compose_overlays`）。
- **四类运行入口与 6 项既有契约测试同步**：
  - 路由：`apps/api/app/api/v1/router.py` 与 `apps/api/app/main.py`；同步 `packages/python-sdk/tests/test_openapi_contract.py` 的 WebSocket 根挂载检查。
  - Celery：`apps/api/app/workers/celery_app.py`。
  - 迁移与 Schema 排除：`apps/api/alembic/env.py` 与 `apps/api/app/db/native_schema.py`；将 5 项读取 `native_schema.py` 源码文本的测试改为校验 `NATIVE_MANAGED_TABLES` 实际导出集合。
  - Compose 与工作区 owner 包：全量 26 份 `docker-compose*.yml` 服务／覆盖分离语义与 `apps/api/pyproject.toml` `[tool.uv.sources]` 纳入可执行一致性守卫。
- **可执行架构守卫**：`apps/api/tests/test_builtin_module_assembly.py`（10 项测试，含 HTTP 与 WebSocket 路由冲突／`:path`／`{id}:action`／条件 `/__test` 遮蔽扫描、Celery 实际任务注册校验、`app.db.models.__init__` 同步校验及无重型导入／无相对导入校验）。

## 2. 验证证据

| 检查项                       | 命令                                                                                                                                                                                                                  | 结果                                                                                                                                                                                    |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 内置装配与队列守卫           | `apps/api/.venv/bin/pytest apps/api/tests/test_builtin_module_assembly.py apps/api/tests/test_jobs_queue_assembly.py -q`                                                                                              | 18/18 通过（10 项装配守卫 + 8 项 Jobs 队列装配）                                                                                                                                        |
| 既有契约测试同步             | `apps/api/tests/test_identity_{keys,challenges}_native.py`、`test_assets_input_manifests_migration.py` 与 `packages/python-sdk/tests/test_openapi_contract.py::test_public_stream_channels_match_root_mounted_router` | `NATIVE_MANAGED_TABLES` 断言与 SDK 流通道根挂载检查全部通过（`test_identity_{sessions,groups}_native.py` 中两条无关 SQL 注释断言属既有基线失败，其 `NATIVE_MANAGED_TABLES` 断言已通过） |
| OpenAPI 快照字节一致         | `apps/api/.venv/bin/python scripts/export_openapi.py --check`                                                                                                                                                         | 通过（794 条路由零碰撞、零动态段遮蔽、快照完全一致）                                                                                                                                    |
| 冷进程 ORM 元数据一致        | `load_declared_orm_models()` 对比 `import app.db.models`                                                                                                                                                              | 218/218 表零差异                                                                                                                                                                        |
| 可销毁库冷迁验证             | 临时 `postgres:16-alpine` 执行 `alembic upgrade head`（走新 `alembic/env.py`）并校验 `alembic current`                                                                                                                | 升至 `bo0510 (head)`，验证后销毁临时容器                                                                                                                                                |
| Compose 真实解析核验         | `docker compose -f docker-compose.yml --profile '*' config --services` 对比 `platform.MANIFEST`                                                                                                                       | 16 服务完全一致                                                                                                                                                                         |
| 静态检查、注释棘轮与文档构建 | `ruff check`、`ruff format --check`、`pnpm lint:comments`、`pnpm docs:build`                                                                                                                                          | 全部通过                                                                                                                                                                                |

## 3. 未覆盖范围

- 本切片不引入运行时插件热加载、动态包扫描或按部署关闭核心内置模块的开关，符合计划“不提前建设空的通用框架”约束。
- M4b（下一个扩展点）与 M4c（所选扩展点对应的 Assets／Learning／Insights owner 收口）待维护者选型后实施。

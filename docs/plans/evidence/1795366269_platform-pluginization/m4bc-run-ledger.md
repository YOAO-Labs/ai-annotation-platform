# M4b / M4c 运行账本：格式适配器扩展点与纯值对象边界收口

- **日期**：2026-11-24
- **分支**：`plugin/plugin-opt`（平台仓）、`main`（`YOAO-Plugins`）
- **架构决定**：[ADR-0188](../../../adr/archive/0188-format-adapter-extension-point.md)（Accepted）

## 交付范围

### M4c（owner 边界收口）

- 从 `apps/api/app/services/mask_formats/contracts.py` 与全部内置适配器（`adapters.py`、`image_adapters.py`、`video_adapters.py`）移除 `AsyncSession` 与 ORM `Project` / `Task` / `Annotation` / `DatasetItem` 参数。
- 引入冻结值对象 `FormatProjectContext`、`ResolvedTaskInfo`、`ExportTaskGeometrySummary`、`ExportMaskRecord` 与窄端口协议 `FormatHostPort`；由核心运行时 `apps/api/app/services/mask_formats/runtime.py`（`SqlAlchemyFormatHost`）集中承接任务匹配、`AssetsTaskMediaSummaryReader.current_image_summary`（闭合原 `image_adapters.py` 直读 `DatasetItem` ORM 缺口）、`current_video_summary`、导出快照读取与 `import_native_json_annotations` 原子提交。
- 新增 `apps/api/tests/test_mask_format_boundary.py` 以 AST 与签名反射强制锁定零 `sqlalchemy` / `app.db.models` 导入（含 `remote_adapter.py`）。

### M4b（格式适配器扩展点与参考插件）

- **平台仓**：
  - `apps/labeling/src/yoao_labeling/format_adapter_contract.py`：`yoao-extension-manifest/v1`（`extension_point: "format_adapter"`）与 `yoao-format-adapter/v1` 封闭契约、内置 22 格式防冲突校验、抗 ReDoS `option_schema` 子集与导出相对路径穿越防护。
  - `apps/labeling/src/yoao_labeling/format_adapter_security.py`：独立 `FORMAT_ADAPTER_ENCRYPTION_KEY` 失败关闭 Fernet 加密、连接期 DNS 解析与 IP 绑定 SSRF 护栏、禁用重定向与代理的有界 `FormatAdapterHttpClient`。
  - `apps/labeling/src/yoao_labeling/sql/0511_format_adapter_plugins.sql` 与 `apps/api/alembic/versions/0511_format_adapter_plugins.py`（`bo0511`）：`yoao_labeling_v1.format_adapter_plugins` 私有表（撤销 `PUBLIC` 权限）、5 个 `SECURITY DEFINER` owner 函数（含必填 CAS `revision`、禁止同态空转与退役不可逆清空 `auth_token_ciphertext`）。
  - `apps/api/app/api/v1/format_adapter_plugins.py`、`apps/api/app/services/mask_formats/{remote_adapter,dynamic_registry}.py`、`apps/api/app/assembly/modules/labeling.py`：7 条超管接口、动态项目格式目录、导入预检／执行／恢复与逐项执行期活跃状态复核。
  - 同步更新 `apps/api/openapi.snapshot.json`、`.env.example` 与 `docs-site/dev/reference/env-vars.md`。
- **插件仓（`YOAO-Plugins`）**：
  - `plugins/format-adapter/labelme-json/`：零依赖 Node.js 20+ ESM LabelMe JSON 格式适配器（`rectangle` / `polygon` 导入导出、`crypto.timingSafeEqual` 鉴权、有限数值坐标校验、`io.yoao.plugin.manifest` OCI 标签与非 root 只读 `Dockerfile`）及 `.github/workflows/plugin-format-adapter.yml`。

## 验证证据

| 检查项                                                                                   | 环境与命令                                                                                                                                                    | 结果                                                                                                                                                                                                                                                   |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| M4c 纯边界与规划测试                                                                     | `uv run pytest tests/test_mask_format_boundary.py tests/test_mask_format_planning.py`                                                                         | 11/11 通过                                                                                                                                                                                                                                             |
| M4b/M4c + M4a 装配回归（可销毁 Postgres 16）                                             | `alembic upgrade head`（`bo0511`）+ `pytest tests/test_format_adapter_plugins.py tests/test_mask_format_boundary.py tests/test_builtin_module_assembly.py`    | 17/17 通过                                                                                                                                                                                                                                             |
| `YOAO-Plugins` Node.js 插件测试                                                          | `node --test plugins/format-adapter/labelme-json/test/plugin.test.mjs`                                                                                        | 2/2 通过（含清单一致性、双工转换与非数值坐标 422 拒绝）                                                                                                                                                                                                |
| 双档隔离实测（直连档裸进程 + 受管档非 root 只读容器 + 真实 Postgres 16 + 真实 YOAO API） | 可销毁 `postgres:16-alpine` + `application_role` 权限核验 + `--read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --user 1000:1000` 容器 | 通过：`restricted_role_zero_table_privs=true`、`oci_manifest_bound=true`、`import_preflight_and_commit_count=2`、`export_render_files=1`、`cas_and_409_guards=true`、`disable_and_retire_preserved_history=true`；全部容器、进程与临时镜像已即刻清理   |
| 静态检查与注释棘轮                                                                       | `ruff==0.15.22 check` + `pnpm lint:comments`                                                                                                                  | 0 错误，0 新增解释性注释                                                                                                                                                                                                                               |
| 只读安全与边界审计                                                                       | `research` 子代理全量审计两仓变更                                                                                                                             | 检出 2 High / 4 Medium / 2 Low（非有限坐标校验、批次中途停用复核、`resume_import` 活跃校验、`already_retired` 409 映射、`/probe` 500 防护、SQL CAS 必填与同态拒绝、503 密钥错误映射、IPv6 Host 头与 `remote_adapter.py` AST 覆盖），全部修复并复测通过 |

## 已知基线项与未覆盖范围

- `test_mask_formats_m7.py::test_coco_annotation_adapter_round_trip_and_unknown_mapping`、`test_mask_formats_m8.py`（4 项 `media_owner` fixture 旧基线漂移）与 `test_mask_staging_migration_reads.py`（2 项既有 mock 漂移）属于范围外既有测试基线，未纳入本里程碑修改。
- 导出打包 `build_export_zip` 仍由内置格式主路径负责；本里程碑已冻结并实测外部插件 `POST /export/render` 协议与路径穿越防护，大体积流式导出打包集成留待后续按需接入。

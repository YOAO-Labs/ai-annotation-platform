# 仓库优化 P2：后端 fixture 迁移、兼容层删除与测试库守卫

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P2 工作包，§5.3）
> 基线提交：`0144b734c`（P0+P1 已合入点，本工作树起点）
> 输入：`docs/research/28-repository-optimization-p1-contracts.md` §7（C8 conftest 回退缺口归 P2）
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[GAP]** 未执行或留待后续阶段
> 并行边界：本阶段不修改 `docs/research/26`、`27`、README 与共享清单；`apps/api/app/api/v1/_test_seed.py`
> 与新文件 `apps/api/tests/test_seed_owned.py` 归 P7 所有，未触碰。

## 0. 结论

1. 测试专用 ORM 构造器 shim `_install_legacy_class_kwargs_shim()` 已删除：43 处
   `Project(...)`/`ProjectTemplate(...)` 旧扁平 kwargs（`classes/classes_config/attribute_schema`）
   调用点全部迁移为现行 `tool_bindings` 数据 [V]。
2. `httpx_client_bound` 别名已删除：70 个测试文件、1315 处使用统一迁移到事务绑定的
   `httpx_client`（fixture 语义不变，仅统一名称）[V]。
3. conftest 默认测试库解析不再吞配置错误：显式 `TEST_DATABASE_URL` 优先且同样过守卫校验；
   解析失败明确报错、绝不回退硬编码连接串；目标必须是 postgresql + `_test` 后缀的一次性库；
   错误信息不回显原始 URL / 底层异常文本，异常链抑制，避免畸形 URL 凭据泄漏。配 13 个
   无 DB 纯规则测试 [V]。
4. 生产兼容层 `coalesce_legacy_into_tool_bindings` 保留（生产 API 路由 4 处调用 +
   P7 所属 `_test_seed.py` 1 处），fixture 与工厂零依赖；其行为契约由既有
   `tests/test_tool_bindings_helpers.py` 显式兼容测试继续保护 [V]。
5. P1 固化的保护全部保持有效：`test_worker_signals.py`、`test_discussion_notifications_commit.py`、
   `test_project_access.py` 等独立事务/授权测试未改动并在全量套件中通过 [V]。

## 1. 范围与边界

- 只做 P2（§5.3）：fixture 迁移、shim/别名删除、测试库守卫、工厂收敛与 conftest 注释治理。
- 产品代码零改动（`git diff --name-only apps/api/app` 为空）[V]。
- 未改事务隔离模型：`db_session` 仍是 SAVEPOINT 隔离；跨连接可见性测试
  （`test_worker_signals.py` 的独立 engine、`test_discussion_notifications_commit.py`）原样保留。
- 工作树测试库：`aap_wt_f66a2832be51a163_test`（本 checkout 启动器自有库，head `0174`），
  未触碰共享 `annotation_test`/`annotation_e2e` 或主 compose 栈。

## 2. fixture 迁移：旧扁平 kwargs → 现行 tool_bindings

### 2.1 工厂：`tests/factory.py::build_tool_bindings`

按评审意见（msg_4f61dad59371）实现为**调用方显式 unit** 的现行模型构造器，不做任何
type_key→unit 推断、不经过旧字段兼容翻译：

```python
build_tool_bindings(classes, *, unit="bbox", attribute_schema=None) -> dict
```

- `classes` 条目为 `str` 或 `dict`（直接携带 `name/color/alias/order`），缺省按入参顺序编号；
- 产出一个启用工具单位 `{"<unit>": {"enabled": True, "classes": [...], "attribute_schema": ...}}`，
  与 API 写入路径的绑定结构同形；
- `unit` 缺省 `bbox`（image-det / video-track 常规标注）；分割/掩码语义由调用点显式传
  `unit="region"`；
- `create_project` 工厂改为直接调用本构造器，删除了对 coalesce 的内部依赖。

### 2.2 迁移清单（28 文件，43 处构造点）

AST 全量扫描（`Project`/`ProjectTemplate` 构造调用上的 legacy kwargs）驱动迁移，迁移后
复扫为 0 残留 [V]：

| 形态                                                     | 文件（处数）                                                                                                                                                                                                                                                                                                                                                          | 处理                                               |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `classes=["car"]` 等简单列表                             | accept_attribute_overrides、annotation_shape_metadata、attribute_audit、batch_lifecycle(2)、comment_polish、scheduler、task_batch_visibility(3)、task_discussion_page、task_lock、task_lock_dedup、task_reopen_notification、task_skip、v0_7_2、v0_7_6、video_chapters_api、video_frame_service(3)、video_tracker_jobs_list、video_tracker_worker、video_workbench(6) | `tool_bindings=build_tool_bindings([...])`         |
| `classes + classes_config`（颜色/别名）                  | prediction_shape_index、project_templates(2)                                                                                                                                                                                                                                                                                                                          | 类别条目直接携带 `alias/color/order`               |
| `classes + attribute_schema`                             | video_workbench:988                                                                                                                                                                                                                                                                                                                                                   | `attribute_schema=` 参数                           |
| `classes=[]`（空类别）                                   | export_aap_json:173                                                                                                                                                                                                                                                                                                                                                   | `build_tool_bindings([])`                          |
| `type_key="image-segmentation"`                          | image_pyramid                                                                                                                                                                                                                                                                                                                                                         | 显式 `unit="region"`（与 region 类别门控语义一致） |
| 旧 kwargs 与显式 `tool_bindings` 并存（shim 下为死参数） | predictions_import、video_tracker_multi_instance                                                                                                                                                                                                                                                                                                                      | 删除死参数，保留显式绑定                           |
| `tool_bindings or {}` 兜底                               | annotations_import                                                                                                                                                                                                                                                                                                                                                    | `tool_bindings or build_tool_bindings([...])`      |
| `_seed_template(**overrides)` 间接构造                   | project_templates(2)                                                                                                                                                                                                                                                                                                                                                  | 调用点改传 `tool_bindings=`                        |

### 2.3 shim 删除

conftest 中 `_install_legacy_class_kwargs_shim()`（含对 `Project.__init__` /
`ProjectTemplate.__init__` 的全局替换）整体删除。迁移后测试直接构造真实模型字段，
不再存在任何测试专用 ORM 构造器补丁 [V]。

## 3. `httpx_client_bound` 别名删除

- 两个名称本是完全相同的 fixture（别名行 `httpx_client_bound = httpx_client`）；
  统一为 conftest 文档声明的唯一名称 `httpx_client`。
- 迁移 70 个文件 1315 处（参数、调用点），含两名称同函数共存文件的参数合并检查
  （AST 扫描重复形参为 0）[V]。
- conftest 头部说明修正：不再有"未绑定/绑定"双名称叙事，明确 `httpx_client` 即事务绑定客户端；
  需要真实独立事务/跨连接可见性的场景指向 `test_worker_signals.py`、
  `test_discussion_notifications_commit.py` 的独立连接写法。
- 历史归档 `docs/plans/archive/**` 中的旧名属真实历史记录，按 §7.3 保留不改写。

## 4. 测试库解析守卫与配置错误显式化（P0 C8 缺口关闭）

### 4.1 之前的问题

`_default_test_db_url()` 用 `except Exception: return "postgresql+asyncpg://user:pass@..."`
吞掉一切配置错误：settings 导入失败、迁移 URL 畸形都会静默落到历史默认串，最终表现为
莫名其妙的连接认证失败，且可能指向未获准的数据库。`TEST_DB_DEFAULT` 在 import 时求值，
显式 `TEST_DATABASE_URL` 也无法避免这次失败的求值。

### 4.2 现在的行为（`_resolve_test_db_url` + `_validate_test_db_target`）

1. 显式 `TEST_DATABASE_URL` 优先返回，且**同样过守卫**（显式覆盖不能绕过校验）；
2. 否则从 `settings.effective_migration_database_url` 派生 `annotation_test` 库
   （`hide_password=False` 保留真实密码）；
3. 任一步失败 → `RuntimeError` 指出修复路径（设置 `TEST_DATABASE_URL`），**无任何硬编码回退**；
4. 守卫规则：连接串必须可解析；backend 必须 postgresql；库名必须 `_test` 结尾
   （覆盖文档化历史默认 `annotation_test` 与启动器分配的 `aap_wt_*_test`）；
5. 凭据安全：用户可见错误只含异常类型名/驱动名/库名，原始 URL 与底层异常文本不回显，
   `from None` 抑制异常链，畸形 URL 中的密码不会出现在输出或 traceback；
6. `apply_migrations` 打印目标库名仅为人工核对，注释明确"不构成安全边界"（守卫才是）；
7. 开发模式 import 拒绝（`AAP_WORKTREE_MODE=dev`）与 seed router 的库名后缀纵深校验原样保留。

### 4.3 焦点测试：`tests/test_conftest_db_url.py`（13 例，无 DB）

| 测试                                                        | 保护点                                                        |
| ----------------------------------------------------------- | ------------------------------------------------------------- |
| `test_explicit_env_override_wins`                           | 显式覆盖优先生效，且不再读取 settings（坏 settings 下仍通过） |
| `test_explicit_override_cannot_bypass_target_guard`         | 显式指向 `_dev` 库被拒                                        |
| `test_derived_url_fixes_annotation_test_and_keeps_password` | 派生库名固定 annotation_test、保留真实密码与 host/port        |
| `test_settings_failure_raises_without_fallback`             | settings 异常 → 明确报错，无硬编码回退                        |
| `test_malformed_settings_url_raises_without_fallback`       | 畸形迁移 URL → 明确报错                                       |
| `test_invalid_targets_rejected`（5 参数）                   | `_dev`/无后缀/空库名/sqlite 驱动/畸形 URL 全部拒绝            |
| `test_malformed_url_error_does_not_leak_credentials`        | 错误文本与异常链均不含 URL/密码                               |
| `test_documented_disposable_targets_accepted`（2 参数）     | `annotation_test` 与 `aap_wt_*_test` 放行                     |

### 4.4 其它 conftest 治理

- `db_session` teardown 的 `try: SystemSettingsService.invalidate() except Exception: pass`
  改为直接调用：进程内缓存清理失败必须可见，不再静默吞掉污染（§5.3-9）。
- 头部 docstring 删除 v0.6.x"解锁旧测套"版本史叙事，改为描述当前契约：
  事务绑定、身份 fixture（平台身份 + 显式 membership）、一次性测试库前提与运行方式（§7.1）。

## 5. 保留与不做的决定

| 项                                                                      | 决定                                                                                                                          | 依据                                                                                 |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| 生产 `coalesce_legacy_into_tool_bindings`                               | 保留于 `app/services/project.py`，调用方：`app/api/v1/projects.py`(2)、`project_templates.py`(2)、`_test_seed.py`(1, P7 所有) | §5.3-2：生产兼容接口仍在用；不能因测试不再调用而删                                   |
| `test_tool_bindings_helpers.py` 的 coalesce 契约测试                    | 原样保留                                                                                                                      | 显式兼容边界的既有覆盖                                                               |
| `db_session` SAVEPOINT 模型、`test_engine` function-scope               | 不变                                                                                                                          | 独立事务保护由专门测试承担（§5.3-6），未 mock 掉                                     |
| 75 个文件的内联 `ProjectMember(...)` 构造、3 个本地 `_seed_user` helper | 本阶段不收编                                                                                                                  | 属 P4/P6 的 factory 全面收敛；本阶段仅新增工具绑定工厂并保持 membership 显式创建语义 |
| 纯规则测试布局                                                          | 不搬文件                                                                                                                      | §5.3-5"按现有测试布局标记与选择"；新增的守卫测试本身即无 DB 纯规则测试               |

## 6. 实际执行的检查与结果

均在 `pnpm dev:worktree -- exec --mode test`（自有库 `aap_wt_f66a2832be51a163_test`，
head `0174`）执行；日志留存于 `/tmp/aap-opt-p2/`。

| 检查                | 命令/范围                                                                                                                                                                                                               | 结果                                   |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| AST 残留扫描        | legacy kwargs / `httpx_client_bound` / shim 全仓扫描                                                                                                                                                                    | 0 残留 [V]                             |
| 焦点守卫测试        | `pytest -q tests/test_conftest_db_url.py`                                                                                                                                                                               | 13 passed，exit 0 [V]                  |
| 迁移冒烟（分批）    | task_lock、prediction_shape_index、export_aap_json、project_templates、annotations_import、image_pyramid、video_mask_corrections、video_workbench、batch_lifecycle                                                      | 全部 exit 0 [V]                        |
| ruff check / format | `apps/api/tests/`（0.15.22，与 pre-commit 同版本）                                                                                                                                                                      | All checks passed；25 文件已格式化 [V] |
| 全量后端套件        | `pytest -q --durations=20 -p no:cacheprovider`，junit `/tmp/aap-opt-p2/pytest-full.xml`：**4398 tests / 0 failures / 0 errors / 15 skipped（均为环境条件跳过：opt-in 环境变量、可选夹具、空参数集），405s，exit 0** [V] |
| `git diff --check`  | 提交前                                                                                                                                                                                                                  | 无空白错误 [V]                         |

quiet addopts 会抑制计数行：以退出码 + junit XML（`/tmp/aap-opt-p2/pytest-full.xml`）
中的 failures=0/errors=0 为准，不做无意义重跑。

## 7. 未执行、保留与后续归属

- **[GAP] factory 全面收敛（§5.3-7 后半）**：成员/批量造数的内联构造仍分散在 75 个文件；
  本阶段只收敛工具绑定构造。归 P4/P6，需避免与并行波冲突。
- **[GAP] 纯规则/集成布局标记**：未引入新的标记体系（§5.3-5 前半按现有布局即可运行），
  后续若 CI 需要分级选择再补标记。
- **[GAP] e2e seed 归属**：`_test_seed.py` 内的 coalesce 调用与 `tests/test_seed_owned.py`
  归 P7；其新代码将在集成时对本阶段迁移后的 fixture 做回归。
- `docs/research/26`、`27` 的台账回填由协调方合并时处理（并行约定）。

## 8. 回退边界

- 单一提交序列（消费者迁移 → 兼容层删除/守卫），`git revert` 对应提交即可整体回退；
- 回退不涉及迁移、锁文件、生成类型、CI 工作流与远端保护；
- 产品代码零改动，无需产品侧联动回退。

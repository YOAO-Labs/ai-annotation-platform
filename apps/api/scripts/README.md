# apps/api/scripts

Dev / ops 用一次性 / 周期性脚本,各自带 `--help`。

## validate_gpu_arbitration

跨 Backend GPU 显存仲裁的非生产验收器。`preflight` 只读采集 PostgreSQL、Redis、backend
challenge health 与 `nvidia-smi`，并校验 action 与资源域内全部现存 allocation Backend；
`run` 通过真实 dispatch authority 执行 manifest 中的
workload，并支持响应丢失、grant 后取消和 health timeout 故障注入；`verify` 从无故障主报告的
原始快照重算断言，不采信报告中自报的 `passed` 摘要。故障报告在 `run` 进程内完成机器检查，
当前不作为 `verify` 的输入。

```bash
cd apps/api
PYTHONPATH=../_shared/protocol_v2/src:. uv run python \
  scripts/validate_gpu_arbitration.py preflight \
  --manifest /tmp/gpu-acceptance/manifest.json \
  --output /tmp/gpu-acceptance/preflight.json

PYTHONPATH=../_shared/protocol_v2/src:. uv run python \
  scripts/validate_gpu_arbitration.py run \
  --manifest /tmp/gpu-acceptance/manifest.json \
  --run-id acceptance-node-a-01 \
  --confirm-run-id acceptance-node-a-01 \
  --output /tmp/gpu-acceptance/run.json

PYTHONPATH=../_shared/protocol_v2/src:. uv run python \
  scripts/validate_gpu_arbitration.py verify \
  --scenario single-card-co-residency /tmp/gpu-acceptance/run.json
```

`run` 拒绝 production，且 `--confirm-run-id` 必须与 `--run-id` 完全一致。它不会迁移数据库、
修改 rollout mode、repair Redis 或启停服务；但会真实加载、drain、恢复或卸载 manifest 指定的
backend，必须在隔离维护窗口执行。报告不会写入鉴权 token 或 action 原始业务 body；只记录原始
manifest 内容摘要，因此 verifier 能校验脱敏后的拓扑/action 元数据，不能脱离原文件还原或复验 body。

完整 manifest、单卡/双卡/跨宿主步骤、阈值与证据边界见
[GPU 显存仲裁验收 Runbook](../../../docs-site/ops/runbooks/gpu-arbitration-acceptance.md)。

## configure_gpu_collector_role.sql

为 `gpu.control` tombstone collector 配置独立 PostgreSQL 最小权限。脚本不创建登录角色或处理
密码；先由 DBA 建立普通应用角色与 collector 角色，再由独立 schema owner 执行：

```bash
psql -d annotation \
  -v application_role=annotation_app \
  -v collector_role=annotation_gpu_collector \
  -f scripts/configure_gpu_collector_role.sql
```

脚本会拒绝同角色、普通应用仍有有效 membership/fence DELETE、可 `SET ROLE` 的成员关系、
collector 越权或高权限角色。
普通应用角色不能是表 owner；数据库迁移继续使用独立 schema owner。

## backfill_scenes

对历史 dataset 补 `scene_id` + `frame_index`(跨 task 帧序列地基)。

```bash
cd apps/api

# 1 个 dataset
PYTHONPATH=. uv run python scripts/backfill_scenes.py --dataset-id <uuid>

# 所有"无 scene"的 dataset(批量)
PYTHONPATH=. uv run python scripts/backfill_scenes.py --all-missing

# 预览(不写库)
PYTHONPATH=. uv run python scripts/backfill_scenes.py --all-missing --dry-run

# 显式指定 mode(默认 auto)
PYTHONPATH=. uv run python scripts/backfill_scenes.py --dataset-id <uuid> --mode single
```

**幂等**:已有 scene → 跳过(notes 提示);部分 items 有 scene_id → 跳过("partial migration",人工 review)。

**不在 docker 启动时自动跑**——管理员人工 review 后执行。

## prepare_original_catalog

管理员在运行本地图片或 nuScenes 导入器前，使用与目标平台一致的所有 bucket 配置，
单独以数据库 owner 和对象存储管理员身份运行此命令。它验证既有目录和 datasets 绑定，
准备该精确 bucket 并启用版本化；目标配置漂移时拒绝更改目录或创建其他 bucket。
必须显式设置以下变量，不能借用普通导入进程的凭据：

```bash
cd apps/api
DATABASE_URL="$CATALOG_OWNER_DATABASE_URL" \
MINIO_ENDPOINT="$PLATFORM_MINIO_ENDPOINT" MINIO_USE_SSL="$PLATFORM_MINIO_USE_SSL" \
MINIO_DATASETS_BUCKET="$PLATFORM_DATASETS_BUCKET" \
MINIO_ACCESS_KEY="$STORAGE_ADMIN_ACCESS_KEY" MINIO_SECRET_KEY="$STORAGE_ADMIN_SECRET_KEY" \
uv run python -m scripts.prepare_original_catalog
```

准备完成后，在独立进程中用各导入器的受限数据库及对象存储身份运行；不要向导入器传递
owner 数据库 URL 或存储管理员密钥。缺少或失效的目标仍明确拒绝，不会由导入器自动修复。

## import_nuscenes_scene

把 nuScenes-mini 的一个/多个 scene 转成本平台点云原生目录 + 入库(MinIO + DB)。
脚本按 scene name 显式创建 scene（每个 scene 一个顶层子目录），调用
scene service 创建 scene 并分配 frame_index，不依赖目录启发式推断。
不引入 nuscenes-devkit,只用 numpy + Pillow 自读 JSON。

`seed.py` 会从 nuScenes 官方地址下载 Mini split 到
`$XDG_CACHE_HOME/ai-annotation-platform/nuscenes-mini`（未设置时使用 `~/.cache`），
校验固定大小与 SHA-256 后只解压导入所需的 key-frame samples；下载支持分段续传。
也可单独执行 `uv run python scripts/nuscenes_fixture.py` 预热缓存。

```bash
cd apps/api

# 单 scene
uv run python scripts/import_nuscenes_scene.py \
    --nuscenes-root /data/nuscenes-mini --scene-tokens scene-0061 \
    --dataset-name nu-scene-0061

# 多个 scene 共用一个 dataset
uv run python scripts/import_nuscenes_scene.py \
    --nuscenes-root /data/nuscenes-mini \
    --scene-tokens scene-0061,scene-0103,scene-0553 --dataset-name nu-mini-multi
```

`--scene-tokens` 实际匹配 scene.json 的 `name` 字段(如 scene-0061)。**幂等**:
dataset 按 DS-NU-<name> 复用;同名 scene 已存在则跳过。详见脚本顶部 docstring 的手动测试 checklist。

## 其他脚本

- `seed.py`:dev 账号 + 示例项目；`--profile screenshots` 使用严格素材与截图 catalog 契约
- `seed_assets.py`:按 `seed-assets.toml` 下载固定版本素材，校验大小/SHA-256、安全解压并缓存
- `seed_coco8.py`:真实 `third-party/coco8`(8 图)→ 图片检测项目 + 每图 Task + YOLO 框走 `import_yolo` 作**预标注**导入(非人工标注)
- `seed_large_images.py`:校验 Web 侧现实大图清单，幂等创建 `P-LARGE-IMG` / `DS-LARGE-IMG`，并可入队、等待图片金字塔生成
- `nuscenes_fixture.py`:从官方地址分段续传 nuScenes mini，在仓库外缓存并安全解压 key-frame samples
- `seed_pointcloud.py`:nuScenes mini scene-0061 → 固定 dev 点云项目，并按 scene 建包（截图 profile 不建包）
- `seed_scale.py`:在隔离测试库中为明确的 `--run-id UUID` 创建大规模压测夹具；使用同一 UUID 的 `--purge` 清理精确清单，未收尾的运行会阻止清理。
- `import_images.py`:批量导入本地图片到 dataset，启动时输出实际数据库 session／current 用户以便核对受限身份。
- `bootstrap_admin.py`:从 ENV 读取首次超管信息，提交由 CLI 持有；用户创建归 `IdentityCommandService`，`application/bootstrap_admin.py` 组合 `AuditService` 写入同事务审计。已存在邮箱不改资料、不追加审计。
- `backfill_task_datasets.py`:`--dry-run` / `--verify` 使用 PostgreSQL 只读事务，`--apply` 按批提交；`application/task_dataset_backfill.py` 负责扫描/报告并委托 `project_task_dataset.ensure_membership` 写入，不改冻结版本内容。
- `reset_datasets.py`:清空 datasets + 关联表
- `dump-openapi.py`:导出 openapi.snapshot.json

## Screenshot seed 素材

素材清单同时记录来源、摘要、仓库许可证和媒体公开文档状态。默认缓存目录为
`$XDG_CACHE_HOME/ai-annotation-platform/seed-assets`，也可显式指定：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed_assets.py \
  --profile screenshots --cache-dir /tmp/aap-seed-assets

# 已有完整缓存时禁止联网
PYTHONPATH=. uv run python scripts/seed_assets.py \
  --profile screenshots --cache-dir /tmp/aap-seed-assets --offline
```

`--asset-dir <path>` 使用 `<path>/<asset-id>/` 下已经解压的本地目录。下载器拒绝摘要或
大小不符、路径穿越、符号链接、设备文件和超出解压上限的归档。

严格截图 profile 使用固定摘要的 CC0 道路照片、CC BY 城市交通视频、PCL BSD-3-Clause
RGB-D 扫描和 RapidOCR 示例图；派生素材会先完成许可门禁、摘要校验和确定性转换，再上传
到对象存储。`demo` 模式不受该门禁影响。

截图 profile 必须在已准备独立原片存储的 `test` 或 `e2e` worktree 运行，并为每次运行提供
唯一 UUID。`DATABASE_URL` 是受限产品账号，`PLAYWRIGHT_E2E_DATABASE_URL` 只供隔离夹具
进程验证并连接同一个测试库的 owner；不得把 owner 账号交给 API、浏览器或其他测试。
真实截图 seed 由私有启动器分配准入、启动受监管 API，并把当前根的控制绑定交给新生产者
进程；不要直接调用 `seed.py --profile screenshots` 或自行拼接 admission。
从仓库根单独准备并检查真实截图 API：

```bash
pnpm dev:worktree -- exec --mode e2e --with-worker -- \
  node apps/web/scripts/run-screenshot-native.mjs --seed -- --ml-backend-mode live
```

入口显示实际 API 地址与 run ID，并保持运行；`Ctrl+C` 通过原 owner 排空和清理。
仅验证非 ML 素材时追加 `--backend-requirements none`。专用协议 stub 已就绪时可使用
`--ml-backend-mode stub`，不能把远程 live 地址当作 stub。运行截图矩阵时不预先启动此命令；
Playwright 自行准备准入、seed 与 API 生命周期，见[截图工作流](../../../docs-site/dev/how-to/update-screenshots.md)。

相同 run ID 的重跑只核对和对账该运行已登记的项目、数据集和原片，不接管共享夹具；
中断后存在未确认写入时保留 admission 和存储收据，先查明实际工作是否结束，不得扫桶清理。
旧普通输出没有本 run 的准确版本收据时明确拒绝认领；Beta 数据使用新的隔离 run，不补造兼容凭据。
nuScenes 的私有原件同样先在一个事务内登记真实条目、creation 和恢复指纹，再执行原件写入与校验；
校验完成后才挂接来源。operator 回执按本 run 的 creation／item 归属核验，不使用普通导入的
提前发布路径，也不因条目已存在就将尚未校验的原件标为可用。
标定 JSON 的读取使用当前原生 owner 核验过的原件只读身份；启动器显式交接这组读取配置，
不继承调用方的 S3 凭据，也不向 seed 进程提供桶管理身份。
媒体 producer 作用域使用实际业务 Session；异常先完成该事务的回滚，再释放原连接上的准入锁。
回滚、解锁或关闭失败会保留原错误及清理错误，未结算的准入与发布责任不会因连接退出而自动消失。
媒体发布使用独立的、仅有函数权限的 Jobs 发布账号（`JOBS_PUBLICATION_DATABASE_URL` 或
`JOBS_PUBLICATION_DATABASE_URL_FILE`），不能借用夹具 owner 或业务账号。投递沿用平台当前队列入口：
配置 `JOBS_QUEUE_URL` 时使用该远程服务，否则使用实际 Celery；远程发布失败不回退到另一条通道。
stub URL 必须由当前运行独占，或已登记为该运行所有；live 模式只读取现有健康事实。
真实异步预测、重试、导出或视频追踪作业若没有对应的执行与物理退役凭据，将阻止根清理。
未完成并发隔离验收的夹具族仍应串行执行。

## 超大图开发夹具

浏览器基准、文档截图与后端 pyramid 共用
`apps/web/scripts/image-bench/fixtures.json` 中的现实大图清单。先在仓库根下载并校验原图，再从
`apps/api` 导入当前开发栈：

```bash
pnpm --filter web image:seeds

cd apps/api
PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --enqueue-pyramids --wait-seconds 1800
```

默认导入清单内全部图片；可重复传 `--id <fixture-id>` 只选部分。脚本在任何数据库或对象存储写入前
复核本地字节数与 SHA-256，上传使用固定对象 key 和摘要 metadata，重复运行不会重复建
DatasetItem、Task 或 generation。固定项目和数据集被人工改名、改 owner 或接入其它资源时会拒绝覆盖；
production 环境始终拒绝运行。

截图 catalog 由私有夹具进程使用相同 `SCREENSHOT_FIXTURE_RUN_ID` 读取；普通 API
和浏览器不持有 owner 凭据。目录按该运行的精确资源清单解析用户、项目、任务、批次
和运行时 UUID，并校验所需 Backend 能力；数据不完整时返回错误，不回退到共享项目。

## audit_project_roles（项目级员工角色 · 增量 A 准备，只读）

项目级员工角色改造的准备工具（计划见
`docs/plans/1789807315_project-scoped-employee-roles.md`）。它只读数据库：CLI 开启
`REPEATABLE READ, READ ONLY` 事务（由 PostgreSQL 强制），绝不写入角色、成员或分派；
用于员工身份转换前的差异盘点与对账，不改变任何现有行为。

审计不直接读取 Identity、Workspace、Task、TaskBatch、Annotation 或 TaskLock 私表。
它只读三个领域的固定审计关系；0173 准备列由各自的固定 `schema_capabilities`
关系报告。运行前由维护者按 Identity → Workspace → Labeling 顺序显式安装：

```bash
DB=annotation_test
MIGRATION_ROLE=owner
AUDIT_ROLE=role_auditor
psql -U "$MIGRATION_ROLE" -d "$DB" \
  -v expected_database="$DB" -v migration_role="$MIGRATION_ROLE" \
  -v identity_owner=identity_code -v consumer_role="$AUDIT_ROLE" \
  -f infra/docker/identity-role-audit-grants.sql
psql -U "$MIGRATION_ROLE" -d "$DB" \
  -v expected_database="$DB" -v migration_role="$MIGRATION_ROLE" \
  -v workspace_owner=workspace_code -v consumer_role="$AUDIT_ROLE" \
  -f infra/docker/workspace-role-audit-grants.sql
psql -U "$MIGRATION_ROLE" -d "$DB" \
  -v expected_database="$DB" -v migration_role="$MIGRATION_ROLE" \
  -v labeling_owner=labeling_code -v consumer_role="$AUDIT_ROLE" \
  -f infra/docker/labeling-role-audit-grants.sql
```

上述安装器要求目标库已有迁移角色、各领域非 LOGIN owner 和受限审计 LOGIN；
安装器仅为审计投影向 Labeling owner 授予以下私表列读取权限：`tasks`、`task_batches`、
`annotations`、`task_locks`；审计 LOGIN 只获固定审计视图的 `SELECT`，不获这些私表的表级或列级权限。还需显式授予该 LOGIN 读取
`public.alembic_version` 的 `SELECT`、`public` schema 的 `USAGE`，以及目标数据库的
`CONNECT`；安装器不会恢复已从 `PUBLIC` 撤销的连接权限。

若目标库之后应用 0173，维护者须显式重新运行 `labeling-role-audit-grants.sql`，
使 task 投影读取新增准备列；审计 CLI 不自动安装或刷新任何关系。

```bash
cd apps/api
uv run python scripts/audit_project_roles.py --pretty
uv run python scripts/audit_project_roles.py --output /tmp/project-role-audit.json
# 直接运行（脚本自带 apps/api sys.path 引导，不会再报 ModuleNotFoundError: app）
.venv/bin/python scripts/audit_project_roles.py --max-rows 1
```

审计目标库默认取 `settings.database_url`，可用 `--database-url` 覆盖；只读冒烟/CI 场景可通过
环境变量 `DATABASE_URL` 传入隔离测试库，避免把连接串放到命令行参数里。

输出为带版本的 JSON（`report_version: project-role-audit/2`），包含 run ID、Alembic
仓储/库基线（含各增量列是否存在）、各清单总数与实体 ID/角色，不包含口令哈希、邀请 token、
签名 URL 或自由文本原因。`--max-rows` 必须为正整数，只截断明细（总数仍精确）；
`--database-url` 可覆盖默认连接。

报告内容：

- 未知或 NULL 角色、非法/停用项目 owner、管理员成员关系；把「全局/成员角色不一致」
  （legacy annotator/reviewer 与成员角色不同）与「跨项目角色多样化」
  （同一账号在不同项目承担不同角色）分开列出；`employee` 作为转换后的合法账号值被识别。
- 生效任务分派（任务显式分派优先于继承的批次默认）缺少有效成员关系、错误成员角色，
  以及停用账号仍持有未完成工作。
- 待处理邀请按账号级/项目级、角色、过期与目标项目已删除分类，并标出需要回填
  `project_role` 的历史项目邀请。
- 有审核活动的任务其贡献者证据按缺失轮次 / 缺失提交人 / 非数组或含非法 UUID 元素 /
  未知或非法标注累积器 / 冻结集缺少提交人或累积器成员 分类；只有轮次、提交人、两个均为
  合法 UUID 数组且包含全部累积器成员与提交人 才算完整，历史未知保持 NULL；报告不回显
  原始数组内容（只给存在性与 JSON 类型）。
- 用户、成员、活跃分派、锁、审核认领、任务状态、标注量与历史审核总量的对账计数。

该命令属于增量 A 的附加准备（schema 附加列 + 只读审计），不代表员工角色功能已上线或已部署。

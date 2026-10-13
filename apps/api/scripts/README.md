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

## backfill_scenes (v0.14.0)

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

## import_nuscenes_scene (v0.14.2)

把 nuScenes-mini 的一个/多个 scene 转成本平台点云原生目录 + 入库(MinIO + DB)。
**按 scene name 显式建多 scene**(每个 scene 一个顶层子目录),显式调用 v0.14.0 的
scene service 建 scene + 赋 frame_index,不依赖目录启发式 single-scene inference。
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

# 多 scene 共用 dataset(验证 v0.14.0 多 scene 隔离)
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
- `seed_scale.py`:大规模压测夹具种子
- `import_images.py`:批量导入本地图片到 dataset
- `bootstrap_admin.py`:从 ENV 建超管
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

截图 profile 还必须绑定满足场景能力的 ML Backend。真实服务就绪时使用 live 模式；无 GPU
时先启动 `docker-compose.ml.yml` 的 `screenshot-ml-stub`，再使用 stub 模式：

```bash
PYTHONPATH=. uv run python scripts/seed.py \
  --profile screenshots --repair --ml-backend-mode live

PYTHONPATH=. uv run python scripts/seed.py \
  --profile screenshots --repair --ml-backend-mode stub
```

stub 默认复用 `ML_BACKEND_STORAGE_HOST` 的主机部分并使用 `9100`，可用
`--ml-backend-url` 覆盖。图片、视频和 OCR 项目都会创建唯一启用关联并设置主 backend；
切回 live 模式时会移除 screenshot seed 自有的 stub registry。

## 超大图开发夹具

浏览器基准、文档截图与后端 pyramid 共用
`apps/web/scripts/image-bench/fixtures.json` 中的现实大图清单。先在仓库根下载并校验原图，再从
`apps/api` 导入当前开发栈：

```bash
pnpm --filter @anno/web image:seeds

cd apps/api
PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --enqueue-pyramids --wait-seconds 1800
```

默认导入清单内全部图片；可重复传 `--id <fixture-id>` 只选部分。脚本在任何数据库或对象存储写入前
复核本地字节数与 SHA-256，上传使用固定对象 key 和摘要 metadata，重复运行不会重复建
DatasetItem、Task 或 generation。固定项目和数据集被人工改名、改 owner 或接入其它资源时会拒绝覆盖；
production 环境始终拒绝运行。

截图 API 进程需要指向 `annotation_screenshots_test` 并显式设置
`E2E_SEED_ENABLED=true`，才会提供只读
`GET /api/v1/__test/seed/catalog`。路由会在当前数据库会话中复核 `_test` 后缀；
production 始终不挂载。catalog 按固定逻辑键解析用户、项目、任务、批次和运行时
UUID，并硬校验 ML Backend 主绑定、项目启用关联、connected 状态及场景级能力；
数据不完整时返回 `409 screenshot_seed_not_ready`，不会退回“最新项目”。

## audit_project_roles（项目级员工角色 · 增量 A 准备，只读）

项目级员工角色改造的准备工具（计划见
`docs/plans/1789807315_project-scoped-employee-roles.md`）。它只读数据库：CLI 开启
`REPEATABLE READ, READ ONLY` 事务（由 PostgreSQL 强制），绝不写入角色、成员或分派；
用于员工身份转换前的差异盘点与对账，不改变任何现有行为。

该审计同时可在 0173 迁移之前运行：它先检查 `information_schema` 中是否存在增量列，
缺失时改用 `NULL` 投影并把相应证据归类为“未知/不完整”，因此不会因读取尚不存在的列而失败。

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

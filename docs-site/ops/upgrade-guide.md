---
title: 版本升级指南
audience: [ops]
type: how-to
since: v0.9.0
status: stable
last_reviewed: 2026-07-29
---

# 版本升级指南

本文描述将平台从一个版本升级到下一个版本的标准流程。

## 升级前准备

1. **阅读 CHANGELOG**：查看目标版本的 Breaking Changes 和 Migration Notes
2. **备份数据库**：
   ```bash
   docker exec ai-annotation-platform-postgres-1 pg_dump -U user annotation > backup-$(date +%Y%m%d).sql
   ```
3. **备份 MinIO**（如有重要标注数据）
4. **通知用户**：计划维护窗口

## 标准升级步骤

```bash
# 1. 拉取最新代码
git pull origin main

# 2. 对比“当前部署 commit”到新 HEAD，检查依赖 / Dockerfile / compose 变化
git diff <deployed-commit>..HEAD -- apps/api/pyproject.toml apps/api/uv.lock \
  apps/web/package.json pnpm-lock.yaml infra/docker apps/*-backend/Dockerfile \
  docker-compose*.yml

# 3a. 若有依赖变更 → 重新构建镜像
docker compose build api web celery-worker

# 3b. 若无依赖变更 → 仅重启容器
docker compose up -d

# 4. 运行数据库迁移
docker exec ai-annotation-platform-api-1 alembic upgrade head

# 5. 验证服务正常
curl -f http://localhost:8000/api/v1/health
curl -f http://localhost:8088
```

## 迁移相关说明

- 数据库迁移通过 Alembic 自动管理，每次升级必须执行 `alembic upgrade head`
- 只有目标 migration 明确提供可逆 downgrade 时才能执行 `alembic downgrade`；数据归位、合并或候选暂存类迁移可能是 no-op downgrade 或会丢数据，回滚前必须先读 migration docstring
- 前端静态资源由 Vite 构建，版本号在文件名中，无缓存问题

## 重点注意事项

### 旧数据集内容身份

升级新增的内容与位置映射保留既有 Item、任务、标注及冻结版本。先运行[定位回填](/dev/concepts/storage-connections#内容身份与兼容回填)并核对报告；其中 `--verify` 只检查数据库定位，不读取原件，不能作为内容可用证明。

未知摘要的旧托管原件需按[显式补算流程](/dev/concepts/storage-connections#显式补算旧托管原件)先预览、核对当前字节，再按清单应用。不要把历史 hash 或 ETag 当作可信 SHA，不要为恢复读取而修改旧 Item／Task 路径。原件缺失或发生变化时先保留旧身份并处理数据问题。

回退前停止写入并排空后台消费者，保留已使用的内容身份、来源、用途绑定和归属账本。旧应用不理解外部引用时，不得接管这些记录的写入、删除或清理；按具体回退方案维持受控只读。不能统一执行 `alembic downgrade -1`，也不能以 `stamp` 绕过迁移屏障；具体应用回退须在一次性环境按目标版本演练。

### 数据集存储的受控只读应用回退

数据集存储采用保留schema的受控应用回退，不提供独立读取开关、双路径回退或回退计数。当前应用继续严格校验内容身份；未知摘要的旧托管原件须先按上文显式补算，再核对目标旧应用的可用读取范围。

旧应用不理解 `__aap_reference__/` 内容引用，也不能依赖新版的删除前检查。仅保留新表和迁移屏障仍可能被旧版级联删除，因此应先停全部API写入、Worker和Beat，排空活动任务，并保留可恢复的数据库及对象存储备份。不要让新旧应用同时写同一数据库，也不要运行旧镜像中自动迁移的入口。

紧急只读回退使用独立的非owner数据库账号；不要复用schema owner、迁移账号或原运行账号。只授予选定旧读取页面需要的表SELECT，不授予写权限，并设置 `default_transaction_read_only=on`。以下为数据集列表和任务元数据涉及的表；任务详情的锁查询限制见下文，连接凭据表及新来源配置表不在其中：

```sql
CREATE ROLE rollback_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
-- 在交互式 psql 中使用 \password rollback_reader 设置独立密码，不写入脚本或历史。
GRANT CONNECT ON DATABASE annotation TO rollback_reader;
GRANT USAGE ON SCHEMA public TO rollback_reader;
GRANT SELECT ON users, projects, project_members, task_batches,
  datasets, dataset_items, project_datasets, tasks, annotations,
  predictions, image_pyramid_assets, task_dataset_item_links TO rollback_reader;
ALTER ROLE rollback_reader SET default_transaction_read_only = on;
```

数据库名须按部署替换。只读默认值不能替代权限裁剪：应在演练中临时设置事务READ WRITE，确认INSERT、UPDATE、DELETE和TRUNCATE仍因权限被拒。不要赋予连接凭据表SELECT，也不要给旧进程挂载外部目录或提供来源解密／连接凭据。

对象存储也必须使用独立只读账号，不能沿用旧root凭据。仅允许对九个已存在用途桶执行HEAD所需的ListBucket／GetBucketLocation，并只对已核对的旧托管对象授予GetObject；不授予PUT、DELETE、生命周期变更或建桶。旧应用启动会HEAD九桶，HEAD失败可能转为建桶并导致启动失败；生命周期写入拒绝由旧代码记录告警，不能为消除告警授予写权限。即使平台桶中碰巧存在与不透明引用同名的对象，也不能允许旧应用读取该键。

旧应用仅在内部维护入口启动，使用匹配的部署产物及显式环境，不执行数据库降级；不启动后台消费者，禁用环境模型发现和推理连接。入口层拒绝写方法与WebSocket，并限定维护人员访问。GET本身也可能有副作用，因此数据库和对象存储只读权限必须同时保留。使用仍有效的JWT；旧API Key认证会更新 `last_used_at`，密码登录也可能写入，不属于此只读回退范围。维护入口应明确显示只读，不能承诺继续编辑或提交标注。

此模式主动停止Worker，旧版聚合 `/health` 因Celery无响应返回503是预期结果。分别核对数据库、Redis和MinIO健康，以及实际旧数据读取；不要为让聚合健康变绿而启动旧消费者。服务端返回的签名原图URL应使用不带平台Authorization头的独立请求下载。

回退核对旧数据集列表返回的原图URL，并下载完整字节比较摘要。原始基线的任务详情GET会执行 `SELECT FOR SHARE`，因此在真正只读事务中返回500；这一路径不可用，不能为恢复它而放开写权限。关联DatasetItem的任务原件按datasets桶保存，也不能用旧 `/files/tasks/{id}/file-url` 默认annotations桶的结果代替。再经旧HTTP尝试改名、删样本、删数据集，并直接测试数据库与对象写权限。入口正常应在调用旧处理器前拒绝写请求；底层演练绕过入口层时，旧处理器可能返回500，但每次请求必须有对应的数据库只读或权限拒绝证据，不能将任意500视为保护成功。

切回当前应用前停止旧进程并撤销专用凭据，保持schema不变；复核数据库身份、关系、标注、冻结快照及托管／外部字节，分别验证两类当前原件读取，再恢复匹配的Worker／Beat和写入口。应用回退不使外部引用变成旧版可读原件，也不恢复已丢失的外部文件。

本地可复现演练只操作已初始化的工作树test命名空间，使用本地虚拟环境、现有 `mc` CLI、原始基线完整代码归档，以及随机临时DB角色和一小时内到期的MinIO service account：

```bash
pnpm dev:worktree -- init --mode test
pnpm dev:worktree -- exec --mode test -- apps/api/.venv/bin/python \
  scripts/verify_legacy_storage_rollback.py --report /private/rollback-report.json
```

报告记录实际旧提交、检查结果和完整数据库行摘要，不包含密码或签名URL。脚本退出前撤销临时账号并停止旧API；合成业务夹具留在test命名空间，按工作树doctor给出的精确确认值停止并销毁。失败时保留私有诊断日志，清理本轮诊断后再交付。该演练不操作人工环境，不重建镜像，不提供生产回退资格，也不实现读取模式开关、双路径回退或计数。

### 跨过员工项目职责迁移

迁移链包含 `0173`、`0174` 时，按[员工项目角色迁移与回滚 runbook](./runbooks/project-role-migration.md)安排一次协调切换：先完成角色与遗留审核证据审计，在停写窗口排空旧写入者，保留经验证可恢复的迁移前快照，再同步部署匹配的 API、Celery worker 和 Web。

`0174` 将全局标注员、质检员转换为员工，授权改由项目成员职责决定；迁移不猜测补建成员关系，也不自动补造历史贡献者证据。自定义集成须适配 `employee` 与项目职责；遗留审核任务的未知证据可能阻断审核，应在切换前确定恢复方案。

::: danger 不可自动降级
`0174` 的 downgrade 在任何窗口都拒绝。重开写入前只能恢复精确的迁移前快照并回退旧二进制；重开写入后优先保留新身份模型向前修复，恢复旧快照则属于需要业务确认、会丢失新增写入的数据回滚。不要用下面的通用 downgrade 示例跨过 `0174`。
:::

### 跨过工具单位退役与视频候选迁移

从较旧环境直接升级、迁移链包含 `0115`–`0117` 时，必须安排维护窗口并先做数据库备份：

- `0115` 新增项目级 `ai_interactive_enabled`，并把 annotation 中退役的 `ai_interactive` 按几何分批归位到 `region` / `bbox`。分批降低单事务持锁，但大表仍会产生读写压力。
- `0116` 分批归位 prediction，并把项目 / 模板 `tool_bindings.ai_interactive` 中的类别与属性合并到真实几何单位。冲突时保留目标单位配置；downgrade 是 no-op，无法恢复退役单位。
- `0117` 新增 `video_tracker_jobs.staged_result`。downgrade 会删除该列，所有尚未接受的视频追踪候选随之丢失；回滚前先处理 `pending_review` / 带候选的 `cancelled` job。
- `0134` 新增 `raster_mask_uploads`，用于 task 级匿名 Mask 上传归属和并发配额。downgrade 会丢失尚未认领对象的额度账本，但内容寻址对象仍由 24 小时引用扫描 GC 回收。
- `0135` 新增项目级 `raster_mask_native_editing_enabled`，既有项目默认为关闭。downgrade 会删除该列并丢失项目的原生 Mask 灰度选择，不会删除已有 annotation 或内容对象。
- `0136` 新增原生 Mask 候选接受幂等账本。downgrade 会删除接受快照；回滚前不要依赖旧 idempotency key 重放结果。
- `0137` 为接受账本增加过期时间，使过期快照不再永久保留内容引用。downgrade 会移除 expiry，需确认清理任务与旧代码的引用口径一致。
- `0138` 增加 Tracker 局部审核 revision、稳定候选与部分状态。downgrade 会把部分审核状态退回待审，但已从 staged result 移除的候选不会恢复。
- `0139` 增加 correction job 种类、纠错帧 / track 快照和同轨活跃租约。downgrade 会取消活跃 correction 并清除其 staged result，已经保存到 annotation 的人工纠错关键帧不会回滚。
- `0148` 将图片原生 Mask 编辑的列默认值和所有既有项目改为开启。项目管理员升级后仍可单独关闭；downgrade 只把新项目默认值恢复为关闭，不批量改写现有项目选择。

升级后先核对 migration head，再验证：旧项目的 region / bbox 类别仍完整、项目级原生 Mask 编辑已开启且仍可单独关闭、部署创建总闸关闭时所有项目保持只读、交互式 AI 总开关正确、视频追踪完成后进入待审且局部决定可用、同一轨迹的纠错活跃租约能在接受 / 拒绝 / 取消后释放。

### 视频媒体处理

视频元数据处理要求 API / Celery 镜像内有 `ffmpeg` 与 `ffprobe`。涉及 Dockerfile 或依赖变化时必须 rebuild API 与 Celery worker 镜像，不能只重启容器。

media worker 会为非 H.264 视频生成浏览器播放用的 `playback/*.mp4`。升级后如存量视频播放按钮无效，先 rebuild/restart Celery worker，再对相关 dataset 重新触发 media backfill。

```bash
docker compose build api celery-worker
docker compose up -d api celery-worker
```

验证镜像内依赖：

```bash
docker exec ai-annotation-platform-api-1 ffprobe -version
docker exec ai-annotation-platform-celery-worker-1 ffmpeg -version
```

如果视频导入后没有 `video_metadata` 或 poster，优先检查 `media` 队列 worker 日志：

```bash
docker logs ai-annotation-platform-celery-worker-1 --tail 200
```

### 视频标注类型

新建视频轨迹标注默认写 `video_track_bbox`，旧 `video_bbox` 继续可读。升级时重点检查 OpenAPI / 前端类型是否与后端同步，避免旧前端无法识别 `video_track_bbox` discriminator。

## 回滚步骤

以下步骤仅适用于已经确认可逆的迁移；涉及 `0174` 时使用上面的专用 runbook，先确定数据恢复边界再处理二进制。

```bash
# 回滚代码
git checkout <previous-tag>

# 回滚数据库（若迁移已执行）
docker exec ai-annotation-platform-api-1 alembic downgrade <previous-revision>

# 重启服务
docker compose up -d
```

## 零停机升级（高级）

当前版本不支持滚动升级（同时运行两个 API 版本）。建议在低流量时段进行升级，停机时间约 1–3 分钟。

## 版本查询

```bash
# API 版本
curl http://localhost:8000/api/v1/health | jq .version

# 数据库迁移版本
docker exec ai-annotation-platform-api-1 alembic current
```

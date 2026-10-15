# async_jobs 单一真值表收敛（2026-05-23）

> Status: archived; PredictionJob consolidation completed. VideoTrackerJob consolidation was explicitly declined; its domain table remains intentional.

> 把 `async_jobs` 从「索引层」升级为「单一真值表」，收敛掉 `prediction_jobs`(PredictionJob) 与
> `video_tracker_jobs`(VideoTrackerJob) 两张专表的双写双轨。
>
> 来源：ROADMAP §B「性能 / 扩展 · async_jobs 统一表收敛」(P3) + 取经合集 §1.7；底线见决策表「Task 双重含义」行。
>
> 分支：`feat/async-jobs-unification`，分批提交（Phase 1 / Phase 2 各自成 commit）。

## 背景与现状

- `async_jobs`（[async_job.py](../../apps/api/app/db/models/async_job.py)）已是所有长任务的汇总索引表，前端任务铃铛 / 历史页 / `/ai-pre/jobs` / 视频 job 页**全部已迁到 `/async-jobs`**（v0.10.45）。
- 专表仍作 domain 真值双写双轨：
  - `PredictionJob`（批×模型，图像预标）—— worker 把它当**运行时工作记录**用。
  - `VideoTrackerJob`（任务×标注，帧级追踪）—— runner 把它当**运行时状态机** + WebSocket `event_channel` 载体。
- 专表读取端点（`/admin/preannotate-jobs`、`/video-tracker-jobs`）前端仅有 api 定义、**无页面调用**。

## 设计决策

1. **收敛后 domain 字段去向**：所有 domain 专属字段进 `async_jobs.payload`（输入）/ `result`（输出）JSONB。
2. **外键完整性取舍**：收敛会丢掉专表的 FK（`batch_id` / `ml_backend_id` / `task_id` / `annotation_id` / `segment_id`）。
   - `project_id` / `user_id` 已在 `async_jobs` 上（保留 FK + cascade）。
   - 其余 domain id 进 payload，**接受失去引用完整性**（这些 job 是瞬时执行记录，非长期实体关系；删除关联实体时 job 记录变为孤儿索引可接受，retention 会清）。
3. **不可逆迁移**：Phase 1 / Phase 2 各含 drop table 迁移。项目未上生产，dev 库可重置，风险可控。

## Phase 1 — 收敛 PredictionJob（v0.10.49，低风险）

字段全部已双写进 async_jobs payload/result，前端已迁离，最独立。

1. **worker 改以 async_jobs 为工作状态**：[tasks.py](../../apps/api/app/workers/tasks.py) `batch_predict`
   - 删除 PredictionJob 的建行（145-156）/ 进度统计更新（285-292）；统计写入 `async_job.result`。
   - 批次自动翻状态依赖的 `batch_id` 从 `async_job.payload["batch_id"]` 取。
   - `_mark_job_failed`（327-376）去掉专表分支，只走 `async_job_svc.mark_failed`。
   - → verify：现有 `test_prediction_jobs_worker.py` 改写为断言 async_jobs；batch_predict 端到端跑通。
2. **弃用读取端点**：`admin_preannotate_jobs.py`（历史读取端点，已删除；当前见 [async_jobs.py](../../apps/api/app/api/v1/async_jobs.py)） `/admin/preannotate-jobs` 改读 `async_jobs WHERE kind=batch_predict`，或直接删端点 + 前端 api 定义。
   - [admin_preannotate.py](../../apps/api/app/api/v1/admin_preannotate.py) 的 `MAX(started_at) GROUP BY project_id` 改查 async_jobs。
3. **删表迁移**：drop `prediction_jobs`；删 `prediction_job.py`（已删除的历史模型；当前 [AsyncJob](../../apps/api/app/db/models/async_job.py)） model。
   - → verify：`alembic upgrade head` + 全后端测试通过。

## Phase 2 — 收敛 VideoTrackerJob：**决定不做，保留专表**（2026-05-23 拍板）

读完 runner 后评估：收敛 VideoTrackerJob 收益小于风险与副作用，**保留 `video_tracker_jobs` 专表**。

理由：

1. **丢 FK 完整性**（与 prediction_jobs 的本质区别）：专表带 `annotation_id / task_id / dataset_item_id / segment_id` 的 FK + CASCADE，引用的是**正在被编辑的活标注**。标注删除时 job 级联清理；塞进 payload JSONB 后会留孤儿 job。prediction_jobs 是纯历史记录无此问题。
2. **runner 是紧耦合实时状态机**：[video_tracking/runner.py](../../apps/api/app/services/video_tracking/runner.py) 用 `with_for_update()` 行锁 + `db.refresh` 轮询 `cancel_requested_at` 做协作取消，边追踪边 WS 逐帧推送，读十余个 domain 字段当工作状态。迁移到 async_jobs 需全量改写 + 重写 18.8K worker 测试。
3. **收益已被前置满足**：前端列表 / 铃铛 / 历史页早已统一走 `/async-jobs?kind=video_tracker`（v0.10.45 索引层），专表只服务运行时 + FK。

底线（写回决策表「Task 双重含义」行）：**新 job 类型默认进 async_jobs；仅当需 FK 级联到活实体 / 复杂运行时状态机时才建专表**——VideoTrackerJob 正属后者。

## 收尾（已落）

- ROADMAP §B「async_jobs 统一表收敛」改为「已部分落地」+ 决策表「Task 双重含义」行更新。
- CHANGELOG 记 v0.10.49（仅 Phase 1）。

## Outcome

- [Migration 0085](../../apps/api/alembic/versions/0085_drop_prediction_jobs.py) removed PredictionJob in the recorded first phase. The second phase was
  rejected after reviewing foreign-key and runtime-state ownership; it is not an
  unimplemented consolidation task.
- Maintained contracts: [async-job API guide](../../docs-site/api/guides/async-jobs.md)
  and [video-tracker API guide](../../docs-site/api/guides/video-tracker-jobs.md).
- CHANGELOG: historical delivery recorded under `docs/changelogs/0.10.x.md`.
- Release milestone: no new release assigned by this archival pass.

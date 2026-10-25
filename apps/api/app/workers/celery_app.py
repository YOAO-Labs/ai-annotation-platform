from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from app.config import settings

celery_app = Celery(
    "annotation_worker",
    broker=settings.effective_celery_broker,
    backend=settings.effective_celery_broker,
    include=[
        "app.workers.tasks",
        "app.workers.media",
        "app.workers.image_pyramid",
        "app.workers.cleanup",
        "app.workers.avatar_cleanup",
        "app.workers.bug_attachment_cleanup",
        "app.workers.audit",
        "app.workers.deactivation",
        "app.workers.audit_partition",
        "app.workers.task_events",
        "app.workers.presence",
        "app.workers.ml_health",
        "app.workers.predictions_retry",
        "app.workers.prediction_input_cleanup",
        "app.workers.original_cache",
        "app.workers.video_tracker",
        "app.workers.video_track_quality",
        "app.workers.mask_qc",
        "app.workers.point_cloud_quality",
        "app.workers.mask_repair",
        "app.workers.mask_format_import",
        # v0.10.16 · async_jobs 兜底信号 + DuckDB 分析同步
        "app.workers.signals",
        "app.workers.analytics",
        "app.workers.async_jobs_cleanup",
        # v0.10.27 · 导出异步化 + 过期产物清理
        "app.workers.export",
        "app.workers.training_dataset",
        "app.workers.sample_evaluations",
        "app.workers.active_learning",
        "app.workers.training_preview_cleanup",
        "app.workers.training_observability_cleanup",
        "app.workers.learning_policy",
        "app.workers.training_study",
        "app.workers.export_cleanup",
        # v0.11.15 · 外部连接器数据集导入
        "app.workers.dataset_import",
        "app.workers.storage_probe",
        "app.workers.storage_usage",
        "app.workers.managed_original_verification",
        "app.workers.storage_migration_inventory",
        "app.workers.storage_migration_copy",
        "app.workers.dataset_scan",
        "app.workers.dataset_registration",
        "app.workers.dataset_quality",
        "app.workers.dataset_quality_review",
        "app.workers.dataset_source_sync",
        # v0.12.0 · B4 建任务异步化（大 dataset link → Celery 建 task）
        "app.workers.create_tasks",
        "app.workers.cross_frame_job",
        # v0.10.25 · worker 心跳上报
        "app.workers.heartbeat",
        # v0.10.25 · predictions 月分区维护（ADR-0006 Stage 2）
        "app.workers.prediction_partition",
        # v0.11.0 · ADR-0027 双写一致性对账（A 组安全网）
        "app.workers.feedback_reconcile",
        # v0.21.7 · 单帧分支批量逐帧预标注 fan-out (段任务 + chord 收尾)
        "app.workers.frame_preannotate",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    worker_max_memory_per_child=512_000,
    worker_direct=True,
    # v0.10.25 · 默认队列从 Celery 内置 "celery" 改为 "default"。worker 订阅
    # default,ml,media,gpu,cleanup,audit —— 不含 "celery", 故任何未在 task_routes 显式
    # 路由的任务(beat 的 ensure_future_*_partitions / mark_inactive_offline
    # / process_deactivation_requests / refresh_user_perf_mv 等)此前都落进无人消费的 "celery"
    # 队列堆积、永不执行。收口到 default 让兜底任务真正跑起来。
    task_default_queue="default",
    task_routes={
        "app.workers.dataset_quality.detect_quality": {"queue": "media"},
        "app.workers.dataset_source_sync.tick_source_syncs": {"queue": "default"},
        "app.workers.tasks.batch_predict": {"queue": "ml"},
        # v0.21.7 · 逐帧段任务跑 GPU predict → ml 队列; finalize 轻量走 default。
        "app.workers.frame_preannotate.predict_video_segment": {"queue": "ml"},
        # v0.21.8 · 两阶段: 抽帧 (下载+ffmpeg) 走 media 队列 (跨视频并行, 有 ffmpeg);
        #   衔接回调轻量派 chord 走 default。
        "app.workers.frame_preannotate.extract_frames_task": {"queue": "media"},
        "app.workers.frame_preannotate.launch_predict_phase": {"queue": "default"},
        "app.workers.predictions_retry.retry_failed_prediction": {"queue": "ml"},
        "app.workers.prediction_input_cleanup.recover_prediction_inputs": {
            "queue": "cleanup"
        },
        "app.workers.original_cache.maintain_original_cache": {"queue": "cleanup"},
        "app.workers.media.generate_thumbnail": {"queue": "media"},
        "app.workers.media.generate_video_metadata": {"queue": "media"},
        "app.workers.media.generate_task_thumbnail": {"queue": "media"},
        "app.workers.media.backfill_media": {"queue": "media"},
        "app.workers.media.backfill_tasks": {"queue": "media"},
        "app.workers.media.ensure_video_chunks": {"queue": "media"},
        "app.workers.media.extract_video_frames": {"queue": "media"},
        "app.workers.media.cleanup_video_frame_assets": {"queue": "media"},
        "app.workers.image_pyramid.generate_image_pyramid": {"queue": "image-pyramid"},
        "app.workers.image_pyramid.reconcile_image_pyramids": {"queue": "cleanup"},
        "app.workers.video_tracker.run_video_tracker_job": {"queue": "gpu"},
        "app.workers.video_track_quality.run": {"queue": "media"},
        "app.workers.mask_qc.run_mask_qc": {"queue": "media"},
        "app.workers.point_cloud_quality.run": {"queue": "media"},
        "app.workers.mask_repair.run_mask_repair": {"queue": "media"},
        "app.workers.mask_repair.rollback_mask_repair": {"queue": "media"},
        "app.workers.mask_format_import.run_mask_format_import": {"queue": "media"},
        "app.workers.avatar_cleanup.purge_retired_avatars": {"queue": "cleanup"},
        "app.workers.bug_attachment_cleanup.purge_bug_attachments": {
            "queue": "cleanup"
        },
        "app.workers.cleanup.purge_soft_deleted_attachments": {"queue": "cleanup"},
        "app.workers.cleanup.purge_guide_assets": {"queue": "cleanup"},
        "app.workers.cleanup.purge_unreferenced_raster_masks": {"queue": "cleanup"},
        # v0.10.16 · DuckDB 同步 + async_jobs 清理走 cleanup 队列
        "app.workers.analytics.sync_to_duckdb": {"queue": "cleanup"},
        "app.workers.async_jobs_cleanup.purge_old_async_jobs": {"queue": "cleanup"},
        # v0.10.27 · 导出 worker 独立 export 队列；过期产物清理走 cleanup
        "app.workers.export.run_export": {"queue": "export"},
        "app.workers.training_dataset.run_training_dataset": {"queue": "export"},
        "app.workers.sample_evaluations.run_sample_evaluation": {"queue": "ml.cpu"},
        "app.workers.sample_evaluations.maintain_evaluations": {"queue": "default"},
        "app.workers.active_learning.run_round": {"queue": "ml.cpu"},
        "app.workers.active_learning.maintain_rounds": {"queue": "default"},
        "app.workers.training_preview_cleanup.purge_expired_previews": {
            "queue": "cleanup"
        },
        "app.workers.training_observability_cleanup.purge_observations": {
            "queue": "cleanup"
        },
        "app.workers.learning_policy.check_policies": {"queue": "default"},
        "app.workers.training_study.check_studies": {"queue": "default"},
        "app.workers.export_cleanup.purge_expired_export_artifacts": {
            "queue": "cleanup"
        },
        "app.workers.dataset_import.run_dataset_import": {"queue": "media"},
        # v0.12.0 · B4 建任务异步化走 default 队列
        "app.workers.create_tasks.run_create_tasks": {"queue": "default"},
        "app.workers.cross_frame_job.run_cross_frame_job": {"queue": "default"},
        # v0.7.6 · audit 异步 INSERT 走独立队列，不与 ml/media 抢资源
        "app.workers.audit.persist_audit_entry": {"queue": "audit"},
        # v0.8.4 · task_events 批量 INSERT 走独立队列
        "app.workers.task_events.persist_task_events_batch": {"queue": "audit"},
        # Owner-required tasks have a dedicated queue, separate from ordinary workers.
        "app.workers.cleanup.refresh_user_perf_mv": {"queue": "maintenance"},
        "app.workers.cleanup.refresh_audit_bi_mv": {"queue": "maintenance"},
        "app.workers.audit_partition.ensure_future_audit_partitions": {
            "queue": "maintenance"
        },
        "app.workers.audit_partition.archive_old_audit_partitions": {
            "queue": "maintenance"
        },
        "app.workers.prediction_partition.ensure_future_prediction_partitions": {
            "queue": "maintenance"
        },
        # v0.9.11 PerfHud · 1s 推送任务走 default queue (worker 默认订阅 default,ml,media)
        "app.workers.ml_health.publish_ml_backend_stats": {"queue": "default"},
        # v0.8.6 · check_ml_backends_health 历史也漏在路由表外, 同步补上避免 stale celery 队列堆积
        "app.workers.ml_health.check_ml_backends_health": {"queue": "default"},
        # ADR-0049 · 只有独立控制 worker 持有 tombstone collector 数据库凭据。
        "app.workers.ml_health.repair_gpu_arbiter_resources": {"queue": "gpu.control"},
    },
    # v0.7.0：beat schedule。运维侧需 deploy `celery -A app.workers.celery_app beat` 进程
    # （或 worker --beat 单进程模式）才会触发。
    beat_schedule={
        "maintain-original-cache": {
            "task": "app.workers.original_cache.maintain_original_cache",
            "schedule": 60.0,
        },
        "recover-prediction-inputs": {
            "task": "app.workers.prediction_input_cleanup.recover_prediction_inputs",
            "schedule": 60.0,
        },
        "check-source-sync-schedules": {
            "task": "app.workers.dataset_source_sync.tick_source_syncs",
            "schedule": 60.0,
        },
        "maintain-active-learning-rounds": {
            "task": "app.workers.active_learning.maintain_rounds",
            "schedule": crontab(minute="*"),
        },
        "maintain-sample-evaluations": {
            "task": "app.workers.sample_evaluations.maintain_evaluations",
            "schedule": crontab(minute="*"),
        },
        "purge-training-observations": {
            "task": "app.workers.training_observability_cleanup.purge_observations",
            "schedule": crontab(minute=0),
        },
        "purge-expired-training-previews": {
            "task": "app.workers.training_preview_cleanup.purge_expired_previews",
            "schedule": crontab(minute="*"),
        },
        "check-learning-policies": {
            "task": "app.workers.learning_policy.check_policies",
            "schedule": crontab(minute="*"),
        },
        "check-training-studies": {
            "task": "app.workers.training_study.check_studies",
            "schedule": timedelta(seconds=10),
        },
        "purge-retired-avatars": {
            "task": "app.workers.avatar_cleanup.purge_retired_avatars",
            "schedule": 300.0,
        },
        "purge-bug-attachments": {
            "task": "app.workers.bug_attachment_cleanup.purge_bug_attachments",
            "schedule": crontab(hour=3, minute=20),
        },
        "purge-soft-deleted-attachments": {
            "task": "app.workers.cleanup.purge_soft_deleted_attachments",
            "schedule": crontab(hour=3, minute=0),  # 每日 03:00 UTC
        },
        "purge-guide-assets": {
            "task": "app.workers.cleanup.purge_guide_assets",
            "schedule": 300.0,
        },
        "purge-unreferenced-raster-masks": {
            "task": "app.workers.cleanup.purge_unreferenced_raster_masks",
            "schedule": crontab(hour=3, minute=20),
        },
        # v0.8.1 · 自助注销冷静期到期处理（每日 04:00 UTC）
        "process-deactivation-requests": {
            "task": "app.workers.deactivation.process_deactivation_requests",
            "schedule": crontab(hour=4, minute=0),
        },
        # v0.8.1 · 审计分区每月维护：25 日提前建未来分区
        "ensure-future-audit-partitions": {
            "task": "app.workers.audit_partition.ensure_future_audit_partitions",
            "schedule": crontab(day_of_month=25, hour=3, minute=0),
        },
        # v0.10.25 · predictions 月分区维护（ADR-0006 Stage 2）：25 日 03:30 提前建未来分区
        "ensure-future-prediction-partitions": {
            "task": "app.workers.prediction_partition.ensure_future_prediction_partitions",
            "schedule": crontab(day_of_month=25, hour=3, minute=30),
        },
        # v0.11.0 · ADR-0027 双写一致性对账：每日 03:00 UTC（避开 03:30 分区维护）
        "reconcile-annotation-feedback": {
            "task": "app.workers.feedback_reconcile.reconcile_annotation_feedback",
            "schedule": crontab(hour=3, minute=0),
        },
        # v0.8.1 · 审计冷数据归档：每月 2 日把保留期外分区归档至 MinIO 后 DROP
        "archive-old-audit-partitions": {
            "task": "app.workers.audit_partition.archive_old_audit_partitions",
            "schedule": crontab(day_of_month=2, hour=3, minute=0),
        },
        # v0.8.4 · 效率看板物化视图：每小时第 5 分钟 REFRESH MATERIALIZED VIEW CONCURRENTLY
        "refresh-user-perf-mv": {
            "task": "app.workers.cleanup.refresh_user_perf_mv",
            "schedule": crontab(minute=5),
        },
        # 审计月报只物化已经结束的 UTC 日；每日刷新一次即可。
        "refresh-audit-bi-mv": {
            "task": "app.workers.cleanup.refresh_audit_bi_mv",
            "schedule": crontab(hour=0, minute=10),
        },
        # v0.8.3 · 在线状态心跳：每 2 分钟扫描，把超 OFFLINE_THRESHOLD_MINUTES 未活跃的 online 用户置 offline
        "mark-inactive-offline": {
            "task": "app.workers.presence.mark_inactive_offline",
            "schedule": crontab(minute="*/2"),
        },
        # v0.8.6 F2 · ML Backend 周期健康检查：每 60s 扫所有 backend 调 /health，串行 + 0-3s 抖动错峰
        "check-ml-backends-health": {
            "task": "app.workers.ml_health.check_ml_backends_health",
            "schedule": crontab(minute="*"),
            "options": {"expires": 55},
        },
        # 与健康扫描使用独立队列和锁；repair 会自行取得 challenge-bound fresh health。
        "repair-gpu-arbiter-resources": {
            "task": "app.workers.ml_health.repair_gpu_arbiter_resources",
            "schedule": crontab(minute="*"),
            "options": {"expires": 55},
        },
        # v0.9.11 PerfHud · ML Backend 实时统计推送：每 1s 拉所有 active backend /health → publish 到
        # ml-backend-stats:global. 仅在 WS 订阅者计数 > 0 时执行实拉, 0 订阅者时短路 skip 节省 GPU 成本.
        "publish-ml-backend-stats": {
            "task": "app.workers.ml_health.publish_ml_backend_stats",
            "schedule": timedelta(seconds=1),
        },
        # v0.9.25 · 视频帧服务缓存 TTL housekeeping。
        "cleanup-video-frame-assets": {
            "task": "app.workers.media.cleanup_video_frame_assets",
            "schedule": crontab(hour=2, minute=30),
        },
        "reconcile-image-pyramids": {
            "task": "app.workers.image_pyramid.reconcile_image_pyramids",
            "schedule": crontab(hour=2, minute=45),
        },
        # v0.10.16 · DuckDB 离线分析视图同步（每日 02:30 UTC）
        "sync-to-duckdb": {
            "task": "app.workers.analytics.sync_to_duckdb",
            "schedule": crontab(hour=2, minute=30),
        },
        # v0.10.16 · async_jobs 终态 retention purge：30 天后清 completed/failed/cancelled
        "recover-storage-usage": {
            "task": "app.workers.storage_usage.recover_usage_inventories",
            "schedule": 30.0,
        },
        "recover-dataset-quality-reviews": {
            "task": "app.workers.dataset_quality_review.recover_quality_reviews",
            "schedule": 30.0,
        },
        "recover-dataset-quality": {
            "task": "app.workers.dataset_quality.recover_quality",
            "schedule": 30.0,
        },
        "recover-dataset-registrations": {
            "task": "app.workers.dataset_registration.recover_registrations",
            "schedule": 30.0,
        },
        "purge-old-async-jobs": {
            "task": "app.workers.async_jobs_cleanup.purge_old_async_jobs",
            "schedule": crontab(hour=4, minute=15),
        },
        # v0.10.27 · 导出产物缓存清理：每日 04:30 删 expires_at 过期的 export_artifacts 行
        "purge-expired-export-artifacts": {
            "task": "app.workers.export_cleanup.purge_expired_export_artifacts",
            "schedule": crontab(hour=4, minute=30),
        },
        # v0.11.18 · worker 心跳已从 beat 任务改为 worker bootstep（每个 worker 进程自身
        # 定时写 Redis，见 heartbeat.py），不再由 beat 派发，故此处移除。
    },
)

# v0.11.18 · 注册心跳 bootstep：每个 worker 进程在自身内部定时器里周期写 celery:hb:{node}，
# 不依赖 broker fanout 投递（Redis broker 的 Broadcast 队列分发不可靠），多 worker 各自上报。
from app.workers.heartbeat import HeartbeatStep  # noqa: E402

celery_app.steps["worker"].add(HeartbeatStep)

from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from aap_jobs.queue_contract import load_queue_configuration
from app.config import settings

celery_app = Celery(
    "annotation_worker",
    broker=settings.effective_celery_broker,
    backend=settings.effective_celery_broker,
    include=[
        "app.workers.tasks",
        "app.workers.media",
        "app.workers.image_pyramid",
        "app.workers.dataset_import_delivery",
        "app.workers.identity_mail_delivery",
        "app.workers.identity_mail_v2_delivery",
        "app.workers.notification_delivery",
        "app.workers.cleanup",
        "app.workers.audit",
        "app.workers.deactivation",
        "app.workers.audit_partition",
        "app.workers.task_events",
        "app.workers.presence",
        "app.workers.ml_health",
        "app.workers.predictions_retry",
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
    **load_queue_configuration(),
    timezone="UTC",
    task_track_started=True,
    worker_max_memory_per_child=512_000,
    # v0.7.0：beat schedule。运维侧需 deploy `celery -A app.workers.celery_app beat` 进程
    # （或 worker --beat 单进程模式）才会触发。
    beat_schedule={
        "relay-credential-mail": {
            "task": "app.workers.identity_mail_delivery.relay_credential_mail",
            "schedule": timedelta(seconds=10),
        },
        "relay-invitation-mail": {
            "task": "app.workers.identity_mail_v2_delivery.relay_invitation_mail",
            "schedule": timedelta(seconds=10),
        },
        "relay-identity-notifications": {
            "task": "app.workers.notification_delivery.relay_identity_notifications",
            "schedule": timedelta(seconds=10),
        },
        "relay-prediction-retry-deliveries": {
            "task": "app.workers.predictions_retry.relay_prediction_retry_deliveries",
            "schedule": timedelta(seconds=10),
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
        "purge-soft-deleted-attachments": {
            "task": "app.workers.cleanup.purge_soft_deleted_attachments",
            "schedule": crontab(hour=3, minute=0),  # 每日 03:00 UTC
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
        # Recover pending local owner jobs when a worker dies before lease expiry;
        # remote owner mode makes this generic drain a no-op.
        "execute-video-media-recovery": {
            "task": "app.workers.media.execute_video_media",
            "schedule": timedelta(seconds=60),
            "options": {"expires": 55},
        },
        "reconcile-image-pyramids": {
            "task": "app.workers.image_pyramid.reconcile_image_media",
            "schedule": crontab(hour=2, minute=45),
        },
        # v0.10.16 · DuckDB 离线分析视图同步（每日 02:30 UTC）
        "sync-to-duckdb": {
            "task": "app.workers.analytics.sync_to_duckdb",
            "schedule": crontab(hour=2, minute=30),
        },
        # v0.10.16 · async_jobs 终态 retention purge：30 天后清 completed/failed/cancelled
        "purge-old-async-jobs": {
            "task": "app.workers.async_jobs_cleanup.purge_old_async_jobs",
            "schedule": crontab(hour=4, minute=15),
        },
        # v0.10.27 · 导出产物缓存清理：每日 04:30 删 expires_at 过期的 export_artifacts 行
        "purge-expired-export-artifacts": {
            "task": "app.workers.export_cleanup.purge_expired_export_artifacts",
            "schedule": crontab(hour=4, minute=30),
        },
        "relay-dataset-import-deliveries": {
            "task": "app.workers.dataset_import_delivery.relay_dataset_import_deliveries",
            "schedule": timedelta(seconds=10),
        },
        # v0.11.18 · worker 心跳已从 beat 任务改为 worker bootstep（每个 worker 进程自身
        # 定时写 Redis，见 heartbeat.py），不再由 beat 派发，故此处移除。
    },
)

# v0.11.18 · 注册心跳 bootstep：每个 worker 进程在自身内部定时器里周期写 celery:hb:{node}，
# 不依赖 broker fanout 投递（Redis broker 的 Broadcast 队列分发不可靠），多 worker 各自上报。
from app.workers.heartbeat import HeartbeatStep  # noqa: E402

celery_app.steps["worker"].add(HeartbeatStep)

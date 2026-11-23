from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from yoao_jobs.queue_contract import load_queue_configuration
from app.config import settings
from yoao_assets_media_runtime.additional_original import (
    load_additional_original_config,
)

load_additional_original_config()

celery_app = Celery(
    "annotation_worker",
    broker=settings.effective_celery_broker,
    backend=settings.effective_celery_broker,
    include=[
        "app.workers.tasks",
        "app.workers.media",
        "app.workers.assets_media",
        "app.workers.image_pyramid",
        "app.workers.dataset_import_delivery",
        "app.workers.jobs_dispatch",
        "app.workers.notification_delivery",
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
        "app.workers.signals",
        "app.workers.analytics",
        "app.workers.async_jobs_cleanup",
        "app.workers.export",
        "app.workers.training_dataset",
        "app.workers.sample_evaluations",
        "app.workers.active_learning",
        "app.workers.training_preview_cleanup",
        "app.workers.training_observability_cleanup",
        "app.workers.learning_policy",
        "app.workers.training_study",
        "app.workers.export_cleanup",
        "app.workers.storage_probe",
        "app.workers.storage_usage",
        "app.workers.managed_original_verification",
        "app.workers.managed_original_reclaim",
        "app.workers.storage_migration_inventory",
        "app.workers.storage_migration_copy",
        "app.workers.dataset_scan",
        "app.workers.dataset_registration",
        "app.workers.dataset_quality",
        "app.workers.dataset_quality_review",
        "app.workers.dataset_source_sync",
        "app.workers.create_tasks",
        "app.workers.cross_frame_job",
        "app.workers.heartbeat",
        "app.workers.prediction_partition",
        "app.workers.feedback_reconcile",
        "app.workers.frame_preannotate",
    ],
)


def _build_beat_schedule():
    return {
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
            "schedule": crontab(hour=3, minute=0),
        },
        "purge-guide-assets": {
            "task": "app.workers.cleanup.purge_guide_assets",
            "schedule": 300.0,
        },
        "purge-unreferenced-raster-masks": {
            "task": "app.workers.cleanup.purge_unreferenced_raster_masks",
            "schedule": crontab(hour=3, minute=20),
        },
        "process-deactivation-requests": {
            "task": "app.workers.deactivation.process_deactivation_requests",
            "schedule": crontab(hour=4, minute=0),
        },
        "ensure-future-audit-partitions": {
            "task": "app.workers.audit_partition.ensure_future_audit_partitions",
            "schedule": crontab(day_of_month=25, hour=3, minute=0),
        },
        "ensure-future-prediction-partitions": {
            "task": "app.workers.prediction_partition.ensure_future_prediction_partitions",
            "schedule": crontab(day_of_month=25, hour=3, minute=30),
        },
        "reconcile-annotation-feedback": {
            "task": "app.workers.feedback_reconcile.reconcile_annotation_feedback",
            "schedule": crontab(hour=3, minute=0),
        },
        "archive-old-audit-partitions": {
            "task": "app.workers.audit_partition.archive_old_audit_partitions",
            "schedule": crontab(day_of_month=2, hour=3, minute=0),
        },
        "refresh-user-perf-mv": {
            "task": "app.workers.cleanup.refresh_user_perf_mv",
            "schedule": crontab(minute=5),
        },
        "refresh-audit-bi-mv": {
            "task": "app.workers.cleanup.refresh_audit_bi_mv",
            "schedule": crontab(hour=0, minute=10),
        },
        "mark-inactive-offline": {
            "task": "app.workers.presence.mark_inactive_offline",
            "schedule": crontab(minute="*/2"),
        },
        "check-ml-backends-health": {
            "task": "app.workers.ml_health.check_ml_backends_health",
            "schedule": crontab(minute="*"),
            "options": {"expires": 55},
        },
        "repair-gpu-arbiter-resources": {
            "task": "app.workers.ml_health.repair_gpu_arbiter_resources",
            "schedule": crontab(minute="*"),
            "options": {"expires": 55},
        },
        "publish-ml-backend-stats": {
            "task": "app.workers.ml_health.publish_ml_backend_stats",
            "schedule": timedelta(seconds=1),
        },
        "cleanup-video-frame-assets": {
            "task": "app.workers.media.cleanup_video_frame_assets",
            "schedule": crontab(hour=2, minute=30),
        },
        **(
            {
                "reconcile-accepted-video-media": {
                    "task": "app.workers.assets_media.reconcile_video_media",
                    "schedule": crontab(hour=2, minute=30),
                },
                "execute-video-media-recovery": {
                    "task": "app.workers.assets_media.run_video_media",
                    "schedule": timedelta(seconds=60),
                    "options": {"expires": 55},
                },
            }
            if settings.assets_video_executor == "celery"
            else {}
        ),
        **(
            {
                "reconcile-image-pyramids": {
                    "task": "app.workers.image_pyramid.reconcile_image_media",
                    "schedule": crontab(hour=2, minute=45),
                },
            }
            if settings.assets_image_executor == "celery"
            else {}
        ),
        "sync-to-duckdb": {
            "task": "app.workers.analytics.sync_to_duckdb",
            "schedule": crontab(hour=2, minute=30),
        },
        "reconcile-managed-original-references": {
            "task": "app.workers.managed_original_reclaim.schedule_reference_reconciliation",
            "schedule": crontab(hour=3, minute=45),
        },
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
        "purge-expired-export-artifacts": {
            "task": "app.workers.export_cleanup.purge_expired_export_artifacts",
            "schedule": crontab(hour=4, minute=30),
        },
        "relay-dataset-import-deliveries": {
            "task": "app.workers.dataset_import_delivery.relay_dataset_import_deliveries",
            "schedule": timedelta(seconds=10),
        },
        "recover-job-dispatches": {
            "task": "app.workers.jobs_dispatch.recover_job_dispatches",
            "schedule": timedelta(seconds=5),
            "options": {"queue": "jobs.recovery", "expires": 5},
        },
        **(
            {
                "relay-notification-deliveries": {
                    "task": "app.workers.notification_delivery.relay_identity_notifications",
                    "schedule": timedelta(seconds=10),
                },
            }
            if not settings.notification_publication_url
            else {}
        ),
    }


celery_app.conf.update(
    **load_queue_configuration(),
    timezone="UTC",
    task_track_started=True,
    worker_max_memory_per_child=512_000,
    worker_direct=True,
    beat_schedule=_build_beat_schedule(),
)

from app.workers.heartbeat import HeartbeatStep  # noqa: E402

celery_app.steps["worker"].add(HeartbeatStep)

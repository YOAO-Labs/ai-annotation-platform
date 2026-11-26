"""Jobs built-in module manifest."""

from __future__ import annotations

from datetime import timedelta

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    CronSpec,
    MigrationSurface,
    RouterMount,
)

MANIFEST = BuiltinModuleManifest(
    module_id="jobs",
    owner_packages=("yoao-jobs",),
    routers=(RouterMount(module_path="app.api.v1.async_jobs", tags=("async-jobs",)),),
    celery_includes=(
        "app.workers.jobs_dispatch",
        "app.workers.async_jobs_cleanup",
        "app.workers.export",
        "app.workers.export_cleanup",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="recover-job-dispatches",
            task="app.workers.jobs_dispatch.recover_job_dispatches",
            schedule=timedelta(seconds=5),
            options=(("queue", "jobs.recovery"), ("expires", 5)),
        ),
        BeatScheduleSpec(
            name="purge-old-async-jobs",
            task="app.workers.async_jobs_cleanup.purge_old_async_jobs",
            schedule=CronSpec(hour=4, minute=15),
        ),
        BeatScheduleSpec(
            name="purge-expired-export-artifacts",
            task="app.workers.export_cleanup.purge_expired_export_artifacts",
            schedule=CronSpec(hour=4, minute=30),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "async_job",
            "export_artifact",
            "export_output",
            "export_media_bundle",
        ),
        native_managed_tables=(
            ("yoao_jobs_v1", "video_execution_authority"),
            ("yoao_jobs_v1", "video_executions"),
            ("yoao_jobs_v1", "video_execution_route_intents"),
            ("yoao_jobs_v1", "video_execution_checkpoint_restore"),
            ("yoao_jobs_v1", "video_execution_journals"),
            ("yoao_jobs_v1", "video_execution_window_attempts"),
            ("yoao_jobs_v1", "video_execution_captures"),
            ("yoao_jobs_v1", "video_execution_capture_parts"),
            ("yoao_jobs_v1", "video_execution_windows"),
            ("yoao_jobs_v1", "video_execution_window_parts"),
            ("yoao_jobs_v1", "video_execution_span_closures"),
            ("yoao_jobs_v1", "video_restore_fence"),
        ),
    ),
)

"""Labeling, tasks, batches, annotations, masks, and quality built-in module manifest."""

from __future__ import annotations

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    CronSpec,
    MigrationSurface,
    RouterMount,
)

MANIFEST = BuiltinModuleManifest(
    module_id="labeling",
    owner_packages=(
        "yoao-labeling",
        "yoao-labeling-mask-gc",
        "yoao-task-contract",
    ),
    routers=(
        RouterMount(module_path="app.api.v1.task_views", tags=("task-views",)),
        RouterMount(module_path="app.api.v1.data_manager", tags=("data-manager",)),
        RouterMount(module_path="app.api.v1.tasks", tags=("tasks",)),
        RouterMount(
            module_path="app.api.v1.videos", prefix="/videos", tags=("videos",)
        ),
        RouterMount(
            module_path="app.api.v1.video_tracker_jobs",
            prefix="/video-tracker-jobs",
            tags=("video-tracker-jobs",),
        ),
        RouterMount(
            module_path="app.api.v1.batches",
            prefix="/projects/{project_id}/batches",
            tags=("batches",),
        ),
        RouterMount(
            module_path="app.api.v1.annotation_comments",
            tags=("annotation-comments",),
        ),
        RouterMount(
            module_path="app.api.v1.annotation_history",
            tags=("annotation-history",),
        ),
        RouterMount(module_path="app.api.v1.annotations", tags=("annotations",)),
        RouterMount(
            module_path="app.api.v1.annotation_feedbacks",
            tags=("annotation-feedbacks",),
        ),
        RouterMount(module_path="app.api.v1.mask_qc", tags=("mask-qc",)),
        RouterMount(
            module_path="app.api.v1.point_cloud_quality",
            tags=("point-cloud-quality",),
        ),
        RouterMount(module_path="app.api.v1.mask_formats", tags=("mask-formats",)),
    ),
    celery_includes=(
        "app.workers.cleanup",
        "app.workers.task_events",
        "app.workers.video_tracker",
        "app.workers.video_track_quality",
        "app.workers.mask_qc",
        "app.workers.point_cloud_quality",
        "app.workers.mask_repair",
        "app.workers.mask_format_import",
        "app.workers.create_tasks",
        "app.workers.cross_frame_job",
        "app.workers.feedback_reconcile",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="purge-soft-deleted-attachments",
            task="app.workers.cleanup.purge_soft_deleted_attachments",
            schedule=CronSpec(hour=3, minute=0),
        ),
        BeatScheduleSpec(
            name="purge-unreferenced-raster-masks",
            task="app.workers.cleanup.purge_unreferenced_raster_masks",
            schedule=CronSpec(hour=3, minute=20),
        ),
        BeatScheduleSpec(
            name="reconcile-annotation-feedback",
            task="app.workers.feedback_reconcile.reconcile_annotation_feedback",
            schedule=CronSpec(hour=3, minute=0),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "ai_mask_accept_decision",
            "annotation",
            "annotation_comment",
            "annotation_conversion_plan",
            "annotation_feedback",
            "annotation_operation",
            "comment_attachment_upload",
            "mask_annotation_revision",
            "mask_format_import",
            "mask_format_upload",
            "mask_qc",
            "mask_repair_batch",
            "mask_review_scope",
            "point_cloud_quality",
            "project_task_view",
            "raster_mask_object",
            "raster_mask_upload",
            "scene_track",
            "task",
            "task_append_operation",
            "task_batch",
            "task_creation_chunk",
            "task_creation_operation",
            "task_dataset_item_link",
            "task_event",
            "task_lock",
            "task_original",
            "video_chapter",
            "video_track_quality",
            "video_tracker_job",
        ),
        native_managed_tables=(
            ("public", "mask_gc_intents"),
            ("public", "mask_gc_key_epochs"),
            ("public", "mask_gc_sweeps"),
            ("public", "mask_gc_roots"),
            ("public", "mask_gc_write_permit_secrets"),
            ("yoao_labeling_v1", "video_execution_checkpoint_mask_sets"),
            ("yoao_labeling_v1", "video_writer_receipts"),
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.labeling-mask-gc.yml",
            tier="optional_split",
            services=("labeling-mask-gc",),
        ),
    ),
)

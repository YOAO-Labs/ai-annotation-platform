"""Assets, storage, media, dataset sources, and site agents built-in module manifest."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    CronSpec,
    MigrationSurface,
    RouterMount,
)


def _celery_video_executor(settings: Any) -> bool:
    return settings.assets_video_executor == "celery"


def _celery_image_executor(settings: Any) -> bool:
    return settings.assets_image_executor == "celery"


MANIFEST = BuiltinModuleManifest(
    module_id="assets",
    owner_packages=(
        "yoao-assets-catalog",
        "yoao-assets-media",
        "yoao-assets-mask-content",
        "yoao-assets-cleanup",
        "yoao-assets-source",
        "yoao-media-probe",
        "yoao-filesystem-read",
        "yoao-site-agent-contract",
    ),
    routers=(
        RouterMount(
            module_path="app.api.v1.media_cache",
            prefix="/media-cache",
            tags=("media-cache",),
        ),
        RouterMount(module_path="app.api.v1.originals", tags=("originals",)),
        RouterMount(module_path="app.api.v1.original_playback", tags=("originals",)),
        RouterMount(module_path="app.api.v1.files", prefix="/files", tags=("files",)),
        RouterMount(
            module_path="app.api.v1.datasets", prefix="/datasets", tags=("datasets",)
        ),
        RouterMount(module_path="app.api.v1.dataset_lifecycle", tags=("datasets",)),
        RouterMount(module_path="app.api.v1.dataset_views", tags=("datasets",)),
        RouterMount(module_path="app.api.v1.dataset_quality", tags=("datasets",)),
        RouterMount(
            module_path="app.api.v1.dataset_quality_review", tags=("datasets",)
        ),
        RouterMount(
            module_path="app.api.v1.scenes", prefix="/scenes", tags=("scenes",)
        ),
        RouterMount(
            module_path="app.api.v1.storage", prefix="/storage", tags=("storage",)
        ),
        RouterMount(
            module_path="app.api.v1.storage_reclamation",
            prefix="/storage",
            tags=("storage",),
        ),
        RouterMount(
            module_path="app.api.v1.storage_locations",
            prefix="/storage-locations",
            tags=("storage-locations",),
        ),
        RouterMount(
            module_path="app.api.v1.storage_maintenance",
            tags=("storage-locations",),
        ),
        RouterMount(
            module_path="app.api.v1.storage_connections",
            prefix="/storage-connections",
            tags=("storage-connections",),
        ),
        RouterMount(
            module_path="app.api.v1.dataset_sources", tags=("dataset-sources",)
        ),
        RouterMount(
            module_path="app.api.v1.dataset_reference_replicas",
            tags=("dataset-sources",),
        ),
        RouterMount(module_path="app.api.v1.site_agents", tags=("site-agents",)),
        RouterMount(
            module_path="app.api.v1.site_agent_transport", tags=("site-agents",)
        ),
        RouterMount(
            module_path="app.api.v1.storage_migration_inventories",
            prefix="/storage/migration-inventories",
            tags=("storage",),
        ),
        RouterMount(
            module_path="app.api.v1.storage_migration_copies",
            prefix="/storage",
            tags=("storage",),
        ),
        RouterMount(
            module_path="app.api.v1.storage_migration_windows",
            prefix="/storage/migration-windows",
            tags=("storage",),
        ),
    ),
    celery_includes=(
        "app.workers.media",
        "app.workers.assets_media",
        "app.workers.image_pyramid",
        "app.workers.dataset_import_delivery",
        "app.workers.original_cache",
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
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="maintain-original-cache",
            task="app.workers.original_cache.maintain_original_cache",
            schedule=60.0,
        ),
        BeatScheduleSpec(
            name="check-source-sync-schedules",
            task="app.workers.dataset_source_sync.tick_source_syncs",
            schedule=60.0,
        ),
        BeatScheduleSpec(
            name="cleanup-video-frame-assets",
            task="app.workers.media.cleanup_video_frame_assets",
            schedule=CronSpec(hour=2, minute=30),
        ),
        BeatScheduleSpec(
            name="reconcile-accepted-video-media",
            task="app.workers.assets_media.reconcile_video_media",
            schedule=CronSpec(hour=2, minute=30),
            when=_celery_video_executor,
        ),
        BeatScheduleSpec(
            name="execute-video-media-recovery",
            task="app.workers.assets_media.run_video_media",
            schedule=timedelta(seconds=60),
            options=(("expires", 55),),
            when=_celery_video_executor,
        ),
        BeatScheduleSpec(
            name="reconcile-image-pyramids",
            task="app.workers.image_pyramid.reconcile_image_media",
            schedule=CronSpec(hour=2, minute=45),
            when=_celery_image_executor,
        ),
        BeatScheduleSpec(
            name="reconcile-managed-original-references",
            task="app.workers.managed_original_reclaim.schedule_reference_reconciliation",
            schedule=CronSpec(hour=3, minute=45),
        ),
        BeatScheduleSpec(
            name="recover-storage-usage",
            task="app.workers.storage_usage.recover_usage_inventories",
            schedule=30.0,
        ),
        BeatScheduleSpec(
            name="recover-dataset-quality-reviews",
            task="app.workers.dataset_quality_review.recover_quality_reviews",
            schedule=30.0,
        ),
        BeatScheduleSpec(
            name="recover-dataset-quality",
            task="app.workers.dataset_quality.recover_quality",
            schedule=30.0,
        ),
        BeatScheduleSpec(
            name="recover-dataset-registrations",
            task="app.workers.dataset_registration.recover_registrations",
            schedule=30.0,
        ),
        BeatScheduleSpec(
            name="relay-dataset-import-deliveries",
            task="app.workers.dataset_import_delivery.relay_dataset_import_deliveries",
            schedule=timedelta(seconds=10),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "asset_image",
            "asset_media",
            "asset_storage_deletion",
            "dataset",
            "dataset_asset",
            "dataset_auxiliary_original",
            "dataset_catalog_operation",
            "dataset_import",
            "dataset_import_delivery",
            "dataset_legacy_upload_operation",
            "dataset_operator_operation",
            "dataset_quality",
            "dataset_quality_review",
            "dataset_reference_replica",
            "dataset_saved_view",
            "dataset_source",
            "dataset_source_deletion_operation",
            "dataset_source_ingest",
            "dataset_spatial_operation",
            "dataset_unlink_operation",
            "dataset_upload_intent",
            "image_pyramid",
            "managed_original_creation",
            "managed_original_reclaim",
            "managed_original_retention_hold",
            "managed_original_retirement",
            "managed_original_verification",
            "original_cache",
            "scene_pose",
            "site_agent",
            "site_agent_command",
            "storage_connection",
            "storage_connection_operation",
            "storage_fault",
            "storage_maintenance",
            "storage_migration_copy",
            "storage_migration_cutovers",
            "storage_migration_finalization",
            "storage_migration_inventory",
            "storage_migration_read_publication",
            "storage_migration_window",
            "storage_observation_rate",
            "storage_usage",
            "thumbnail_output",
            "video_derivative",
        ),
        native_managed_tables=(
            ("public", "asset_accepted_image_inputs"),
            ("public", "asset_accepted_image_input_attempts"),
            ("public", "asset_accepted_input_garbage"),
            ("public", "asset_accepted_image_input_manifests"),
            ("public", "asset_accepted_image_input_manifest_members"),
            ("public", "annotation_input_sources"),
            ("public", "asset_accepted_video_manifests"),
            ("public", "asset_accepted_video_members"),
            ("public", "asset_accepted_video_inputs"),
            ("public", "asset_accepted_video_input_attempts"),
            ("public", "asset_accepted_video_times"),
            ("public", "asset_accepted_video_frames"),
            ("public", "asset_accepted_video_frame_attempts"),
            ("public", "annotation_video_input_sources"),
            ("public", "video_tracker_input_scopes"),
            ("public", "annotation_video_tracker_input_sources"),
            ("yoao_assets_v1", "saved_binding_probe"),
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.assets-source.yml",
            tier="optional_split",
            services=("assets-source",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.assets-import.yml",
            tier="optional_split",
            services=("assets-import",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.assets-cleanup.yml",
            tier="optional_split",
            services=("assets-cleanup",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.assets-media.yml",
            tier="optional_split",
            services=(
                "assets-media-control",
                "assets-media-worker",
                "assets-images-worker",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.assets-mask-content.yml",
            tier="optional_split",
            services=(
                "assets-mask-content",
                "assets-mask-cleanup",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.assets-native.yml",
            tier="dev_override",
            overrides=(
                "assets-images-worker",
                "assets-media-control",
                "assets-source",
                "celery-worker-image-pyramid",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.modules.yml",
            tier="optional_split",
            services=("media-probe",),
        ),
    ),
)

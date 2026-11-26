"""Audit built-in module manifest."""

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
    module_id="audit",
    owner_packages=(
        "yoao-audit",
        "yoao-audit-contract",
    ),
    routers=(
        RouterMount(
            module_path="app.api.v1.audit_logs", prefix="/audit-logs", tags=("audit",)
        ),
    ),
    celery_includes=(
        "app.workers.audit",
        "app.workers.audit_partition",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="ensure-future-audit-partitions",
            task="app.workers.audit_partition.ensure_future_audit_partitions",
            schedule=CronSpec(day_of_month=25, hour=3, minute=0),
        ),
        BeatScheduleSpec(
            name="archive-old-audit-partitions",
            task="app.workers.audit_partition.archive_old_audit_partitions",
            schedule=CronSpec(day_of_month=2, hour=3, minute=0),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "audit_log",
            "audit_archive_output",
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.audit.yml",
            tier="optional_split",
            services=("audit-maintenance",),
        ),
        ComposeOverlaySpec(
            file="docker-compose.experiments.yml",
            tier="experiment",
            services=("audit-summary",),
        ),
    ),
)

"""Insights and diagnostics built-in module manifest."""

from __future__ import annotations

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    CronSpec,
    MigrationSurface,
    RouterMount,
)

MANIFEST = BuiltinModuleManifest(
    module_id="insights",
    routers=(
        RouterMount(
            module_path="app.api.v1.project_performance",
            tags=("project-performance",),
        ),
        RouterMount(
            module_path="app.api.v1.admin_analytics",
            prefix="/admin/analytics",
            tags=("admin-analytics",),
        ),
        RouterMount(
            module_path="app.api.v1.admin_system_health",
            prefix="/admin/system-health",
            tags=("admin-system-health",),
        ),
        RouterMount(
            module_path="app.api.v1.bug_reports",
            tags=("bug-reports",),
        ),
    ),
    celery_includes=(
        "app.workers.analytics",
        "app.workers.bug_attachment_cleanup",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="purge-bug-attachments",
            task="app.workers.bug_attachment_cleanup.purge_bug_attachments",
            schedule=CronSpec(hour=3, minute=20),
        ),
        BeatScheduleSpec(
            name="refresh-user-perf-mv",
            task="app.workers.cleanup.refresh_user_perf_mv",
            schedule=CronSpec(minute=5),
        ),
        BeatScheduleSpec(
            name="refresh-audit-bi-mv",
            task="app.workers.cleanup.refresh_audit_bi_mv",
            schedule=CronSpec(hour=0, minute=10),
        ),
        BeatScheduleSpec(
            name="sync-to-duckdb",
            task="app.workers.analytics.sync_to_duckdb",
            schedule=CronSpec(hour=2, minute=30),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "bug_report",
            "bug_attachment_upload",
        ),
    ),
)

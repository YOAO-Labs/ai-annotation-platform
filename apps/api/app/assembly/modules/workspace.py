"""Workspace built-in module manifest."""

from __future__ import annotations

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    MigrationSurface,
    RouterMount,
)

MANIFEST = BuiltinModuleManifest(
    module_id="workspace",
    owner_packages=(
        "yoao-workspace",
        "yoao-workspace-contract",
    ),
    routers=(
        RouterMount(
            module_path="app.api.v1.projects", prefix="/projects", tags=("projects",)
        ),
        RouterMount(
            module_path="app.api.v1.guide_assets",
            prefix="/projects",
            tags=("guide-assets",),
        ),
        RouterMount(
            module_path="app.api.v1.project_templates",
            prefix="/project-templates",
            tags=("project-templates",),
        ),
        RouterMount(
            module_path="app.api.v1.project_pipelines",
            prefix="/project-pipelines",
            tags=("project-pipelines",),
        ),
        RouterMount(
            module_path="app.api.v1.search", prefix="/search", tags=("search",)
        ),
        RouterMount(
            module_path="app.api.v1.dashboard",
            prefix="/dashboard",
            tags=("dashboard",),
        ),
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="purge-guide-assets",
            task="app.workers.cleanup.purge_guide_assets",
            schedule=300.0,
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "project",
            "project_member",
            "project_pipeline",
            "project_template",
            "project_deletion_operation",
            "project_guide_asset",
            "guide_asset_upload",
        ),
        native_managed_tables=(
            ("public", "workspace_membership_operation_receipts"),
            ("public", "workspace_owner_transfer_operation_receipts"),
            ("public", "workspace_configuration_operation_attempts"),
            ("public", "workspace_configuration_operation_states"),
            ("public", "workspace_configuration_operation_receipts"),
        ),
    ),
)

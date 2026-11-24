"""Declarative built-in backend module assembly."""

from app.assembly.builders import (
    build_beat_schedule,
    collect_celery_includes,
    collect_native_managed_relations,
    collect_native_managed_tables,
    collect_orm_model_modules,
    list_compose_overlays,
    load_declared_orm_models,
    mount_api_v1_routers,
    mount_root_routers,
)
from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    CronSpec,
    MigrationSurface,
    RouterMount,
)
from app.assembly.registry import (
    BUILTIN_MODULES,
    validate_builtin_module_manifests,
)

__all__ = [
    "BUILTIN_MODULES",
    "BeatScheduleSpec",
    "BuiltinModuleManifest",
    "ComposeOverlaySpec",
    "CronSpec",
    "MigrationSurface",
    "RouterMount",
    "build_beat_schedule",
    "collect_celery_includes",
    "collect_native_managed_relations",
    "collect_native_managed_tables",
    "collect_orm_model_modules",
    "list_compose_overlays",
    "load_declared_orm_models",
    "mount_api_v1_routers",
    "mount_root_routers",
    "validate_builtin_module_manifests",
]

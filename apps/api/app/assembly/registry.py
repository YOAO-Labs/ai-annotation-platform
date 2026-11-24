"""Authoritative built-in backend module registry and invariant validator."""

from __future__ import annotations

from app.assembly.contracts import (
    VALID_COMPOSE_TIERS,
    VALID_ROUTER_SURFACES,
    BuiltinModuleManifest,
)
from app.assembly.modules import (
    assets,
    audit,
    identity,
    inference,
    insights,
    jobs,
    labeling,
    learning,
    notifications,
    platform,
    workspace,
)

BUILTIN_MODULES: tuple[BuiltinModuleManifest, ...] = (
    platform.MANIFEST,
    identity.MANIFEST,
    workspace.MANIFEST,
    assets.MANIFEST,
    labeling.MANIFEST,
    inference.MANIFEST,
    learning.MANIFEST,
    jobs.MANIFEST,
    notifications.MANIFEST,
    audit.MANIFEST,
    insights.MANIFEST,
)


def validate_builtin_module_manifests(
    modules: tuple[BuiltinModuleManifest, ...] = BUILTIN_MODULES,
) -> None:
    seen_module_ids: set[str] = set()
    seen_packages: set[str] = set()
    seen_routers: set[tuple[str, str]] = set()
    seen_workers: set[str] = set()
    seen_beats: set[str] = set()
    seen_models: set[str] = set()
    seen_native_relations: set[tuple[str, str]] = set()
    seen_compose_files: set[str] = set()

    for manifest in modules:
        if not manifest.module_id or manifest.module_id in seen_module_ids:
            raise ValueError(f"Duplicate or empty module_id: {manifest.module_id!r}")
        seen_module_ids.add(manifest.module_id)

        for package in manifest.owner_packages:
            if not package or package in seen_packages:
                raise ValueError(f"Duplicate or empty owner_package: {package!r}")
            seen_packages.add(package)

        for router in manifest.routers:
            if router.surface not in VALID_ROUTER_SURFACES:
                raise ValueError(f"Invalid router surface: {router.surface!r}")
            key = (router.module_path, router.attr)
            if key in seen_routers:
                raise ValueError(f"Duplicate router mount declared: {key!r}")
            seen_routers.add(key)

        for worker_mod in manifest.celery_includes:
            if worker_mod in seen_workers:
                raise ValueError(f"Duplicate Celery include declared: {worker_mod!r}")
            seen_workers.add(worker_mod)

        for beat in manifest.beat_schedules:
            if beat.name in seen_beats:
                raise ValueError(f"Duplicate Beat schedule declared: {beat.name!r}")
            seen_beats.add(beat.name)

        for model_stem in manifest.migrations.orm_models:
            if model_stem in seen_models:
                raise ValueError(f"Duplicate ORM model declared: {model_stem!r}")
            seen_models.add(model_stem)

        for relation in manifest.migrations.native_managed_tables:
            schema, table = relation
            if not schema or not table:
                raise ValueError(
                    f"Empty schema or table in native_managed_tables: {relation!r}"
                )
            if relation in seen_native_relations:
                raise ValueError(
                    f"Duplicate native-managed relation declared: {relation!r}"
                )
            seen_native_relations.add(relation)

        for overlay in manifest.compose_overlays:
            if overlay.tier not in VALID_COMPOSE_TIERS:
                raise ValueError(f"Invalid compose tier: {overlay.tier!r}")
            if overlay.file in seen_compose_files:
                raise ValueError(f"Duplicate compose file declared: {overlay.file!r}")
            seen_compose_files.add(overlay.file)
            overlap = set(overlay.services) & set(overlay.overrides)
            if overlap:
                raise ValueError(
                    f"Compose overlay {overlay.file} lists services in both services and overrides: {sorted(overlap)}"
                )

    for manifest in modules:
        for beat in manifest.beat_schedules:
            task_module = beat.task.rsplit(".", 1)[0]
            if task_module not in seen_workers:
                raise ValueError(
                    f"Beat schedule {beat.name!r} targets undeclared worker module {task_module!r}"
                )


validate_builtin_module_manifests(BUILTIN_MODULES)

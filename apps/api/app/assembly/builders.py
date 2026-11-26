"""Pure assembly builders turning declared built-in module manifests into runtime wiring."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from app.assembly.contracts import (
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    CronSpec,
    RouterSurface,
)

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI


def _resolve_modules(
    modules: tuple[BuiltinModuleManifest, ...] | None,
) -> tuple[BuiltinModuleManifest, ...]:
    if modules is not None:
        return modules
    from app.assembly.registry import BUILTIN_MODULES

    return BUILTIN_MODULES


def _resolve_schedule(raw: Any, settings: Any) -> Any:
    value = raw(settings) if callable(raw) else raw
    if isinstance(value, CronSpec):
        from celery.schedules import crontab

        return crontab(
            minute=value.minute,
            hour=value.hour,
            day_of_week=value.day_of_week,
            day_of_month=value.day_of_month,
            month_of_year=value.month_of_year,
        )
    return value


def _mount_routers(
    target: APIRouter | FastAPI,
    settings: Any,
    *,
    surface: RouterSurface,
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> None:
    for manifest in _resolve_modules(modules):
        for mount in manifest.routers:
            if mount.surface != surface:
                continue
            if mount.when is not None and not mount.when(settings):
                continue
            module = import_module(mount.module_path)
            router = getattr(module, mount.attr)
            kwargs: dict[str, Any] = {}
            if mount.prefix:
                kwargs["prefix"] = mount.prefix
            if mount.tags:
                kwargs["tags"] = list(mount.tags)
            target.include_router(router, **kwargs)


def mount_api_v1_routers(
    target: APIRouter,
    settings: Any,
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> None:
    _mount_routers(target, settings, surface="api_v1", modules=modules)


def mount_root_routers(
    app: FastAPI,
    settings: Any,
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> None:
    _mount_routers(app, settings, surface="root", modules=modules)


def collect_celery_includes(
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> list[str]:
    return [
        worker_mod
        for manifest in _resolve_modules(modules)
        for worker_mod in manifest.celery_includes
    ]


def build_beat_schedule(
    settings: Any,
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    schedule: dict[str, dict[str, Any]] = {}
    for manifest in _resolve_modules(modules):
        for spec in manifest.beat_schedules:
            if spec.when is not None and not spec.when(settings):
                continue
            entry: dict[str, Any] = {
                "task": spec.task,
                "schedule": _resolve_schedule(spec.schedule, settings),
            }
            if spec.options:
                entry["options"] = dict(spec.options)
            schedule[spec.name] = entry
    return schedule


def collect_orm_model_modules(
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> tuple[str, ...]:
    return tuple(
        model_stem
        for manifest in _resolve_modules(modules)
        for model_stem in manifest.migrations.orm_models
    )


def load_declared_orm_models(
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> tuple[str, ...]:
    loaded = collect_orm_model_modules(modules)
    for stem in loaded:
        import_module(f"app.db.models.{stem}")
    return loaded


def collect_native_managed_relations(
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> frozenset[tuple[str, str]]:
    return frozenset(
        relation
        for manifest in _resolve_modules(modules)
        for relation in manifest.migrations.native_managed_tables
    )


def collect_native_managed_tables(
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> frozenset[str]:
    return frozenset(
        table
        for schema, table in collect_native_managed_relations(modules)
        if schema == "public"
    )


def list_compose_overlays(
    modules: tuple[BuiltinModuleManifest, ...] | None = None,
) -> tuple[ComposeOverlaySpec, ...]:
    return tuple(
        overlay
        for manifest in _resolve_modules(modules)
        for overlay in manifest.compose_overlays
    )

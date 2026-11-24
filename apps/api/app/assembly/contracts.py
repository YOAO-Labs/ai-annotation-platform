"""Typed declarative contracts for built-in backend module assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

RouterSurface = Literal["api_v1", "root"]
ComposeTier = Literal[
    "base",
    "optional_split",
    "ml_plugin",
    "dev_override",
    "experiment",
]

VALID_ROUTER_SURFACES: frozenset[str] = frozenset({"api_v1", "root"})
VALID_COMPOSE_TIERS: frozenset[str] = frozenset(
    {"base", "optional_split", "ml_plugin", "dev_override", "experiment"}
)


@dataclass(frozen=True, slots=True)
class CronSpec:
    minute: int | str = "*"
    hour: int | str = "*"
    day_of_week: int | str = "*"
    day_of_month: int | str = "*"
    month_of_year: int | str = "*"


ScheduleValue = (
    float | timedelta | CronSpec | Callable[[Any], float | timedelta | CronSpec]
)


@dataclass(frozen=True, slots=True)
class RouterMount:
    module_path: str
    attr: str = "router"
    surface: RouterSurface = "api_v1"
    prefix: str = ""
    tags: tuple[str, ...] = ()
    when: Callable[[Any], bool] | None = None


@dataclass(frozen=True, slots=True)
class BeatScheduleSpec:
    name: str
    task: str
    schedule: ScheduleValue
    options: tuple[tuple[str, Any], ...] = ()
    when: Callable[[Any], bool] | None = None


@dataclass(frozen=True, slots=True)
class MigrationSurface:
    orm_models: tuple[str, ...] = ()
    native_managed_tables: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ComposeOverlaySpec:
    file: str
    tier: ComposeTier
    services: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuiltinModuleManifest:
    module_id: str
    owner_packages: tuple[str, ...] = ()
    routers: tuple[RouterMount, ...] = ()
    celery_includes: tuple[str, ...] = ()
    beat_schedules: tuple[BeatScheduleSpec, ...] = ()
    migrations: MigrationSurface = MigrationSurface()
    compose_overlays: tuple[ComposeOverlaySpec, ...] = ()

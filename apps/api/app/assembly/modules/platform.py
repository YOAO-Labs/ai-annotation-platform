"""Core platform runtime glue, health, settings, internal probes, and base compose stacks."""

from __future__ import annotations

from typing import Any

from app.assembly.contracts import (
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    MigrationSurface,
    RouterMount,
)


def _e2e_seed_routes_enabled(settings: Any) -> bool:
    return settings.environment != "production" and settings.e2e_seed_enabled


MANIFEST = BuiltinModuleManifest(
    module_id="platform",
    owner_packages=("yoao-owner-data",),
    routers=(
        RouterMount(
            module_path="app.api.health",
            surface="root",
            prefix="/health",
            tags=("health",),
        ),
        RouterMount(
            module_path="app.api.v1.system_settings",
            prefix="/settings",
            tags=("settings",),
        ),
        RouterMount(
            module_path="app.api.v1.internal",
            prefix="/internal",
            tags=("internal",),
        ),
        RouterMount(
            module_path="app.api.v1._test_seed",
            prefix="/__test",
            tags=("_test_seed",),
            when=_e2e_seed_routes_enabled,
        ),
    ),
    celery_includes=(
        "app.workers.signals",
        "app.workers.heartbeat",
    ),
    migrations=MigrationSurface(
        orm_models=(
            "system_setting",
            "fixture_run",
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.yml",
            tier="base",
            services=(
                "alertmanager",
                "celery-beat",
                "celery-worker",
                "celery-worker-cpu",
                "celery-worker-export",
                "celery-worker-gpu",
                "celery-worker-gpu-control",
                "celery-worker-image-pyramid",
                "celery-worker-jobs-recovery",
                "celery-worker-maintenance",
                "grafana",
                "mailpit",
                "minio",
                "postgres",
                "prometheus",
                "redis",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.prod.yml",
            tier="base",
            services=(
                "api",
                "web",
            ),
            overrides=(
                "alertmanager",
                "celery-beat",
                "celery-worker",
                "celery-worker-cpu",
                "celery-worker-export",
                "celery-worker-gpu",
                "celery-worker-gpu-control",
                "celery-worker-image-pyramid",
                "celery-worker-jobs-recovery",
                "celery-worker-maintenance",
                "mailpit",
                "minio",
                "postgres",
                "redis",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.lan-prod.yml",
            tier="base",
            services=(
                "api",
                "celery-beat",
                "celery-worker",
                "celery-worker-cpu",
                "celery-worker-export",
                "celery-worker-gpu",
                "celery-worker-gpu-control",
                "celery-worker-image-pyramid",
                "celery-worker-jobs-recovery",
                "celery-worker-maintenance",
                "gateway",
                "migrate",
                "redis-production",
                "web",
            ),
        ),
        ComposeOverlaySpec(
            file="docker-compose.hostvols.yml",
            tier="dev_override",
        ),
    ),
)

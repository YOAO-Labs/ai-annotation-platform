"""v0.10.58 · super_admin system health aggregate endpoint."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api import health
from app.config import settings
from app.db.enums import UserRole
from app.deps import require_roles
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()

HealthStatus = Literal["ok", "degraded", "down"]


class HealthComponent(BaseModel):
    name: str
    label: str
    status: HealthStatus
    latency_ms: float | None = None
    detail: str | None = None


class CeleryWorkerHealth(BaseModel):
    name: str
    last_heartbeat_seconds_ago: float | None = None
    pool_max: int | None = None
    status: HealthStatus


class CeleryQueueHealth(BaseModel):
    name: str
    length: int
    status: HealthStatus


class CeleryHealthSummary(BaseModel):
    active_count: int
    workers: list[CeleryWorkerHealth]
    queues: list[CeleryQueueHealth]


class SystemHealthResponse(BaseModel):
    status: HealthStatus
    version: str
    components: list[HealthComponent]
    celery: CeleryHealthSummary


def _raw_ok(raw: dict[str, Any]) -> bool:
    return raw.get("status") == "ok"


def _queue_status(length: int) -> HealthStatus:
    if length >= 100:
        return "down"
    if length >= 25:
        return "degraded"
    return "ok"


def _worker_status(seconds: float | None) -> HealthStatus:
    if seconds is None:
        return "degraded"
    if seconds >= 300:
        return "down"
    if seconds >= 120:
        return "degraded"
    return "ok"


def _component(
    name: str,
    label: str,
    raw: dict[str, Any],
    *,
    status: HealthStatus | None = None,
) -> HealthComponent:
    return HealthComponent(
        name=name,
        label=label,
        status=status or ("ok" if _raw_ok(raw) else "down"),
        latency_ms=raw.get("latency_ms"),
        detail=raw.get("detail"),
    )


def _worst_status(statuses: list[HealthStatus]) -> HealthStatus:
    if "down" in statuses:
        return "down"
    if "degraded" in statuses:
        return "degraded"
    return "ok"


@router.get("", response_model=SystemHealthResponse)
async def get_system_health(
    _: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN.value)),
) -> SystemHealthResponse:
    db_r, redis_r = await asyncio.gather(health._check_db(), health._check_redis())
    minio_r, celery_r = await asyncio.gather(
        asyncio.to_thread(health._check_minio),
        asyncio.to_thread(health._check_celery),
    )

    queues = [
        CeleryQueueHealth(
            name=str(row.get("name") or "default"),
            length=int(row.get("length") or 0),
            status=_queue_status(int(row.get("length") or 0)),
        )
        for row in celery_r.get("queues", [])
        if isinstance(row, dict)
    ]
    workers = [
        CeleryWorkerHealth(
            name=str(row.get("name") or "worker"),
            last_heartbeat_seconds_ago=row.get("last_heartbeat_seconds_ago"),
            pool_max=row.get("pool_max"),
            status=_worker_status(row.get("last_heartbeat_seconds_ago")),
        )
        for row in celery_r.get("workers", [])
        if isinstance(row, dict)
    ]
    celery_status = "ok" if _raw_ok(celery_r) else "down"
    if celery_status == "ok":
        celery_status = _worst_status(
            [item.status for item in queues] + [item.status for item in workers]
        )

    components = [
        _component("db", "PostgreSQL", db_r),
        _component("redis", "Redis", redis_r),
        _component("minio", "MinIO", minio_r),
        _component("celery", "Celery", celery_r, status=celery_status),
    ]
    component_statuses = [item.status for item in components]
    status = (
        "down"
        if components[0].status == "down" or components[1].status == "down"
        else _worst_status(component_statuses)
    )

    return SystemHealthResponse(
        status=status,
        version=settings.app_version,
        components=components,
        celery=CeleryHealthSummary(
            active_count=int(celery_r.get("active_count") or 0),
            workers=workers,
            queues=queues,
        ),
    )

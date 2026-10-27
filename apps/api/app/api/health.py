from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db.base import AsyncSessionLocal
from app.observability.raster_mask import (
    refresh_raster_mask_active_geometries_safely,
)
from app.services.storage import storage_service
from app.schemas.health import HealthCheck, HealthReport
from app.workers.celery_app import celery_app

router = APIRouter()

CELERY_INSPECT_TIMEOUT_SECONDS = 0.75
# v0.19.5 · ml.cpu = 设备感知路由的 CPU 预标队列 (全 CPU pipeline 落此)。
CELERY_QUEUE_NAMES = (
    "default",
    "ml",
    "ml.cpu",
    "media",
    "gpu",
    "cleanup",
    "audit",
    "export",
)


async def _check_db() -> dict:
    start = time.monotonic()
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
            await refresh_raster_mask_active_geometries_safely(db)
        return {
            "status": "ok",
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }
    except Exception as e:
        return {"status": "error", "latency_ms": None, "detail": str(e)}


async def _check_redis() -> dict:
    import redis.asyncio as aioredis  # noqa: PLC0415

    start = time.monotonic()
    try:
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        return {
            "status": "ok",
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }
    except Exception as e:
        return {"status": "error", "latency_ms": None, "detail": str(e)}


def _check_minio() -> dict:
    start = time.monotonic()
    try:
        storage_service.client.head_bucket(Bucket=storage_service.bucket)
        return {
            "status": "ok",
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }
    except Exception as e:
        return {"status": "error", "latency_ms": None, "detail": str(e)}


def _check_celery() -> dict:
    """v0.8.7 F2 · 扩展返回 queues + workers 心跳明细，并填 Prometheus Gauge。

    queues: list[{name, length}]  — active + reserved 数量之和（按 broker 实际为准）
    workers: list[{name, last_heartbeat_seconds_ago}]  — 心跳新鲜度，None=未知
    """
    from app.observability.metrics import (
        CELERY_QUEUE_LENGTH,
        CELERY_WORKER_HEARTBEAT_SECONDS,
    )

    start = time.monotonic()
    try:
        inspect = celery_app.control.inspect(timeout=CELERY_INSPECT_TIMEOUT_SECONDS)
        try:
            stats = inspect.stats() or {}
        except Exception:
            stats = {}
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        if not stats:
            return {
                "status": "error",
                "latency_ms": latency_ms,
                "active_count": 0,
                "workers": [],
                "queues": [],
                "detail": "no workers responded",
            }

        try:
            queue_counts = _read_celery_queue_lengths()
        except Exception:
            queue_counts = {}

        # Prometheus Gauge：覆盖式更新
        for qname, count in queue_counts.items():
            CELERY_QUEUE_LENGTH.labels(queue=qname).set(count)

        # v0.10.25 · 心跳新鲜度：worker 心跳 bootstep（v0.11.18，见 workers/heartbeat.py）周期把
        # unix 时间戳写进 Redis（key celery:hb:{worker}，worker 名与 ping().keys() 同源）。这里同步读
        # 同名 key 算 now - ts。读 Redis 失败整体降级为 None，不影响 /health 其它统计。
        import redis  # noqa: PLC0415

        from app.workers.heartbeat import HEARTBEAT_KEY_PREFIX  # noqa: PLC0415

        heartbeats: dict[str, float | None] = {}
        try:
            r = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
            now = time.time()
            for name in stats.keys():
                raw = r.get(f"{HEARTBEAT_KEY_PREFIX}{name}")
                heartbeats[name] = (now - float(raw)) if raw is not None else None
            r.close()
        except Exception:
            heartbeats = {}

        workers_payload = []
        for name in sorted(stats.keys()):
            last = heartbeats.get(name)
            # 仅在有真实心跳值时填 Gauge，无值不 set 避免误报 0。
            if last is not None:
                CELERY_WORKER_HEARTBEAT_SECONDS.labels(worker=name).set(last)
            workers_payload.append(
                {
                    "name": name,
                    "last_heartbeat_seconds_ago": last,
                    "pool_max": (stats.get(name, {}).get("pool", {}) or {}).get(
                        "max-concurrency"
                    ),
                }
            )

        return {
            "status": "ok",
            "latency_ms": latency_ms,
            "active_count": len(workers_payload),
            "workers": workers_payload,
            "queues": [
                {"name": qname, "length": count}
                for qname, count in sorted(queue_counts.items())
                if count > 0
            ],
        }
    except Exception as e:
        return {
            "status": "error",
            "latency_ms": None,
            "active_count": 0,
            "workers": [],
            "queues": [],
            "detail": str(e),
        }


def _read_celery_queue_lengths() -> dict[str, int]:
    """Read pending broker queue lengths without Celery inspect fan-out."""
    import redis  # noqa: PLC0415

    counts = {name: 0 for name in CELERY_QUEUE_NAMES}
    r = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
    try:
        for name in CELERY_QUEUE_NAMES:
            counts[name] = int(r.llen(name))
    finally:
        r.close()
    return counts


def _public_check(result: dict) -> dict:
    if result.get("status") != "ok" and result.get("detail"):
        return {
            **result,
            "detail": "Health check failed; inspect authenticated system diagnostics.",
        }
    return result


@router.get(
    "/db",
    response_model=HealthCheck,
    responses={503: {"model": HealthCheck, "description": "Component unavailable"}},
)
async def health_db():
    result = await _check_db()
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=_public_check(result))


@router.get(
    "/redis",
    response_model=HealthCheck,
    responses={503: {"model": HealthCheck, "description": "Component unavailable"}},
)
async def health_redis():
    result = await _check_redis()
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=_public_check(result))


@router.get(
    "/minio",
    response_model=HealthCheck,
    responses={503: {"model": HealthCheck, "description": "Component unavailable"}},
)
async def health_minio():
    result = await asyncio.to_thread(_check_minio)
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=_public_check(result))


@router.get(
    "/celery",
    response_model=HealthCheck,
    responses={503: {"model": HealthCheck, "description": "Component unavailable"}},
)
async def health_celery():
    result = await asyncio.to_thread(_check_celery)
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(status_code=code, content=_public_check(result))


@router.get(
    "",
    response_model=HealthReport,
    responses={
        503: {
            "model": HealthReport,
            "description": "One or more components unavailable",
        }
    },
)
async def health_all():
    db_r, redis_r = await asyncio.gather(_check_db(), _check_redis())
    minio_r, celery_r = await asyncio.gather(
        asyncio.to_thread(_check_minio),
        asyncio.to_thread(_check_celery),
    )
    checks = {"db": db_r, "redis": redis_r, "minio": minio_r, "celery": celery_r}
    overall = "ok" if all(v["status"] == "ok" for v in checks.values()) else "degraded"
    code = 200 if overall == "ok" else 503
    return JSONResponse(
        status_code=code,
        content={
            "status": overall,
            "version": settings.app_version,
            "checks": {name: _public_check(result) for name, result in checks.items()},
        },
    )

"""Settings-bound edge access to the Identity-owned login failure counter."""

from __future__ import annotations

from aap_identity.login_counter import RedisLoginCounter
from app.config import settings


def _counter() -> RedisLoginCounter:
    return RedisLoginCounter(
        settings.redis_url, window_seconds=settings.login_failed_window_seconds
    )


async def get_count(ip: str) -> int:
    return await _counter().get_count(ip)


async def increment(ip: str) -> int:
    return await _counter().increment(ip)


async def reset(ip: str) -> None:
    await _counter().reset(ip)

"""The existing immediate Notifications Redis Pub/Sub transport."""

from __future__ import annotations

import json
import uuid


def channel_for(user_id: uuid.UUID | str) -> str:
    return f"notify:{user_id}"


def _redis_from_url(redis_url: str):
    from redis.asyncio import from_url

    return from_url(redis_url)


async def publish_json(redis_url: str, *, channel: str, message: dict) -> None:
    redis = _redis_from_url(redis_url)
    try:
        await redis.publish(channel, json.dumps(message))
    finally:
        await redis.close()

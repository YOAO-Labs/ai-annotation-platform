"""Labeling-owned fixed queries for scene and track read paths."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.scene_read_contract import (
    SCENE_READ_FUNCTION,
    SceneReadData,
    SceneReadUnavailable,
    scene_read_request,
)
from aap_labeling.scene_read_data import SceneReadDataClient


_SCENE_READ_STEP = text(
    f"SELECT {SCENE_READ_FUNCTION.partition('(')[0]}(CAST(:request AS jsonb))"
)


async def read_scene_data(
    db: AsyncSession, action: str, **values: object
) -> SceneReadData:
    request = scene_read_request(action, **values)
    if db.autoflush:
        await db.flush()

    async def execute(request_json: str):
        return await db.scalar(_SCENE_READ_STEP, {"request": request_json})

    try:
        return await SceneReadDataClient(execute).query(request)
    except SceneReadUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"reason": "scene_read_unavailable"},
        ) from None


__all__ = ["read_scene_data"]

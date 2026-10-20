"""Assets-backed project kind facts retained at the API I/O boundary."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_spatial import AssetsSpatialGateway


async def dataset_has_scenes(db: AsyncSession, dataset_id: uuid.UUID) -> bool:
    return await AssetsSpatialGateway(db).has_scenes(dataset_id)

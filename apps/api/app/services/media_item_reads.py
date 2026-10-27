"""Media reads of Assets DatasetItem facts through its fixed public relation.

Media never selects the core ``dataset_items`` table directly. Background work
and request contexts read the same owner projection that catalog clients see;
only the media-owned output lifecycle may touch its own tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog.spatial_contract import ReadItemsRequest, SpatialItem


async def read_items(
    db: AsyncSession, item_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, SpatialItem]:
    """Read requested item facts; unknown IDs are simply absent."""
    wanted = list(dict.fromkeys(item_ids))
    found: dict[uuid.UUID, SpatialItem] = {}
    for start in range(0, len(wanted), 5000):
        batch = wanted[start : start + 5000]
        if not batch:
            continue
        from app.services.assets_spatial import AssetsSpatialGateway

        page = await AssetsSpatialGateway(db).read_spatial_items(
            ReadItemsRequest(item_ids=batch)
        )
        found.update({row.id: row for row in page.items})
    return found


async def read_item(db: AsyncSession, item_id: uuid.UUID) -> SpatialItem | None:
    return (await read_items(db, [item_id])).get(item_id)


@dataclass(frozen=True)
class VideoDerivativeLinks:
    video_output_id: uuid.UUID | None
    video_building_output_id: uuid.UUID | None


async def read_video_derivative_links(
    db: AsyncSession, item_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, VideoDerivativeLinks]:
    """Read the published video output pointers without touching core rows."""
    from sqlalchemy import text

    wanted = list(dict.fromkeys(item_ids))
    found: dict[uuid.UUID, VideoDerivativeLinks] = {}
    for start in range(0, len(wanted), 5000):
        batch = wanted[start : start + 5000]
        if not batch:
            continue
        raw = (
            await db.execute(
                text(
                    "SELECT aap_assets_v1.read_video_derivative_links("
                    "CAST(:item_ids AS uuid[]))"
                ),
                {"item_ids": batch},
            )
        ).scalar_one()
        if not isinstance(raw, list):
            raise RuntimeError("Assets derivative link reply is not a JSON array")
        for row in raw:
            item_id = uuid.UUID(str(row["dataset_item_id"]))
            found[item_id] = VideoDerivativeLinks(
                video_output_id=(
                    uuid.UUID(str(row["video_output_id"]))
                    if row.get("video_output_id") is not None
                    else None
                ),
                video_building_output_id=(
                    uuid.UUID(str(row["video_building_output_id"]))
                    if row.get("video_building_output_id") is not None
                    else None
                ),
            )
    return found


async def read_dataset_items(
    db: AsyncSession, dataset_id: uuid.UUID
) -> list[SpatialItem]:
    """Page the whole dataset projection; callers keep their bounded filters."""
    from aap_assets_catalog.spatial_contract import ReadItemsRequest
    from app.services.assets_spatial import AssetsSpatialGateway

    gateway = AssetsSpatialGateway(db)
    rows: list[SpatialItem] = []
    cursor: uuid.UUID | None = None
    while True:
        page = await gateway.read_spatial_items(
            ReadItemsRequest(
                dataset_id=dataset_id,
                after_item_id=cursor,
                page_size=500,
            )
        )
        rows.extend(page.items)
        if page.complete:
            return rows
        cursor = page.next_item_id
        if cursor is None:
            raise RuntimeError("Assets item page did not provide a cursor")

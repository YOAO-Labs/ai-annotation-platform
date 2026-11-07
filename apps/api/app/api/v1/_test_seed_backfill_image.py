"""Private owned image birth for a real media-backfill cutoff scenario."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._test_seed import (
    SeedOwnedRequest,
    _raster_image_bytes,
    _require_e2e_seed_database,
    _resolve_fixture_scope,
    _run_owned_seed,
)
from app.db.models.dataset import Dataset, DatasetItem
from app.deps import get_db
from app.services import fixture_run_ownership as ownership

router = APIRouter(dependencies=[Depends(_require_e2e_seed_database)])


class BackfillImageRequest(SeedOwnedRequest):
    dataset_id: UUID


class BackfillImageResponse(BaseModel):
    item_id: UUID


@router.post(
    "/seed/backfill-image",
    response_model=BackfillImageResponse,
    include_in_schema=False,
)
async def seed_backfill_image(
    payload: BackfillImageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BackfillImageResponse:
    async def build(scope, admission_id):
        resolved = await _resolve_fixture_scope(db, scope)
        published = await db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_run_resources "
                "WHERE run_id=:run AND kind='dataset' AND resource_id=:dataset "
                "AND state='published')"
            ),
            {"run": payload.run_id, "dataset": payload.dataset_id},
        )
        dataset = await db.get(Dataset, payload.dataset_id)
        if (
            published is not True
            or payload.dataset_id not in resolved.dataset_ids
            or dataset is None
            or dataset.created_by not in resolved.user_ids
        ):
            raise HTTPException(409, detail="fixture_dataset_changed")

        item_id = uuid4()
        filename = f"backfill-late-{item_id.hex}.png"
        image = _raster_image_bytes(0)
        await ownership.reserve(
            db, payload.run_id, "item", item_id, logical_key=f"backfill-{item_id.hex}"
        )
        await db.commit()
        item = DatasetItem(
            id=item_id,
            dataset_id=dataset.id,
            file_name=filename,
            file_path="",
            file_type="image",
            file_size=len(image),
            width=None,
            height=None,
        )
        allocation, original = await ownership.create_original(
            db,
            run_id=payload.run_id,
            admission_id=admission_id,
            item=item,
            logical_path=filename,
            payload=image,
            content_type="image/png",
            requested_by=dataset.created_by,
        )
        await ownership.attach_original(db, item, allocation, original)
        await ownership.publish_resource(db, payload.run_id, "item", item_id)
        await db.commit()
        return BackfillImageResponse(item_id=item_id)

    return await _run_owned_seed(db, payload, "backfill_image_seed", build, request)

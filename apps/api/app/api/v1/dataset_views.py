"""Personal browsing preferences use read authority, independent of dataset writes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset
from app.db.models.dataset_saved_view import DatasetSavedView
from app.db.models.user import User
from app.deps import get_current_user, get_db, require_scopes
from app.schemas.dataset_view import (
    DatasetSavedViewList,
    DatasetSavedViewOut,
    DatasetSavedViewWrite,
)
from app.services.dataset_workbench import (
    require_dataset,
    require_filter_source,
    require_filter_scene,
)

router = APIRouter(
    prefix="/datasets/{dataset_id}/saved-views",
    dependencies=[Depends(require_scopes("datasets:read"))],
)


async def owned_view(db, dataset_id, view_id, actor):
    await require_dataset(db, dataset_id)
    row = await db.scalar(
        select(DatasetSavedView).where(
            DatasetSavedView.id == view_id,
            DatasetSavedView.dataset_id == dataset_id,
            DatasetSavedView.owner_id == actor.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Personal dataset view not found")
    return row


async def save(db, row, payload, actor):
    if payload.query.source_id:
        await require_filter_source(db, row.dataset_id, payload.query.source_id, actor)
    if payload.query.scene_id is not None:
        await require_filter_scene(db, row.dataset_id, payload.query.scene_id)
    row.name = payload.name
    row.query = payload.query.model_dump(mode="json", exclude_none=True)
    row.query_schema_version = payload.query_schema_version
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_dataset_saved_views_owner_name" in str(exc.orig):
            raise HTTPException(
                409, "A personal view with this name already exists"
            ) from None
        raise
    await db.refresh(row)
    return row


@router.get("", response_model=DatasetSavedViewList)
async def list_views(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    await require_dataset(db, dataset_id)
    rows = await db.scalars(
        select(DatasetSavedView)
        .where(
            DatasetSavedView.dataset_id == dataset_id,
            DatasetSavedView.owner_id == actor.id,
        )
        .order_by(DatasetSavedView.name, DatasetSavedView.id)
        .limit(100)
    )
    return DatasetSavedViewList(items=list(rows))


@router.post("", response_model=DatasetSavedViewOut, status_code=201)
async def create_view(
    dataset_id: uuid.UUID,
    payload: DatasetSavedViewWrite,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    await require_dataset(db, dataset_id)
    # Serialize the bounded per-dataset creation check, including concurrent requests.
    await db.scalar(
        select(Dataset.id).where(Dataset.id == dataset_id).with_for_update()
    )
    count = await db.scalar(
        select(func.count())
        .select_from(DatasetSavedView)
        .where(
            DatasetSavedView.dataset_id == dataset_id,
            DatasetSavedView.owner_id == actor.id,
        )
    )
    if count >= 100:
        raise HTTPException(409, "At most 100 personal views per dataset")
    return await save(
        db, DatasetSavedView(dataset_id=dataset_id, owner_id=actor.id), payload, actor
    )


@router.put("/{view_id}", response_model=DatasetSavedViewOut)
async def update_view(
    dataset_id: uuid.UUID,
    view_id: uuid.UUID,
    payload: DatasetSavedViewWrite,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return await save(
        db, await owned_view(db, dataset_id, view_id, actor), payload, actor
    )


@router.delete("/{view_id}", status_code=204)
async def delete_view(
    dataset_id: uuid.UUID,
    view_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    await db.delete(await owned_view(db, dataset_id, view_id, actor))
    await db.commit()

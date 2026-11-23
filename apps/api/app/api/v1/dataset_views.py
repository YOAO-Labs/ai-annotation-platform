"""Personal browsing preferences use read authority, independent of dataset writes."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services.identity_contracts import IdentityActorFacts
from app.services.insights_saved_views import PersonalDatasetViews

router = APIRouter(
    prefix="/datasets/{dataset_id}/saved-views",
    dependencies=[Depends(require_scopes("datasets:read"))],
)


async def _validate_query(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    payload: DatasetSavedViewWrite,
    actor: IdentityActorFacts,
) -> None:
    if payload.query.source_id:
        await require_filter_source(db, dataset_id, payload.query.source_id, actor)
    if payload.query.scene_id is not None:
        await require_filter_scene(db, dataset_id, payload.query.scene_id)


@router.get("", response_model=DatasetSavedViewList)
async def list_views(
    dataset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(get_current_user),
):
    await require_dataset(db, dataset_id)
    views = PersonalDatasetViews(db, dataset_id, actor.id)
    return DatasetSavedViewList(items=await views.list())


@router.post("", response_model=DatasetSavedViewOut, status_code=201)
async def create_view(
    dataset_id: uuid.UUID,
    payload: DatasetSavedViewWrite,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(get_current_user),
):
    views = PersonalDatasetViews(db, dataset_id, actor.id)
    await views.prepare_create()
    await _validate_query(db, dataset_id, payload, actor)
    view = await views.create(payload)
    await db.commit()
    return view


@router.put("/{view_id}", response_model=DatasetSavedViewOut)
async def update_view(
    dataset_id: uuid.UUID,
    view_id: uuid.UUID,
    payload: DatasetSavedViewWrite,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(get_current_user),
):
    await require_dataset(db, dataset_id)
    views = PersonalDatasetViews(db, dataset_id, actor.id)
    await views.require_view(view_id)
    await _validate_query(db, dataset_id, payload, actor)
    view = await views.update(view_id, payload)
    await db.commit()
    return view


@router.delete("/{view_id}", status_code=204)
async def delete_view(
    dataset_id: uuid.UUID,
    view_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: IdentityActorFacts = Depends(get_current_user),
):
    await require_dataset(db, dataset_id)
    await PersonalDatasetViews(db, dataset_id, actor.id).delete(view_id)
    await db.commit()

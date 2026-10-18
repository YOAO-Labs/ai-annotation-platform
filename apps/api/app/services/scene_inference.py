"""ORM adapter for the portable scene-layout planner."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.frame_layout import (
    PreparedDatasetItem,
    source_ingest_logical_path,
)
from aap_assets_source.scene_layout import (
    IngestScenePlan,
    SceneInferenceMode,
    SceneLayoutInput,
    plan_scenes,
)
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.schemas.scene import InferenceResult
from app.services import scene as scene_svc


async def infer_and_apply(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    mode: SceneInferenceMode = "auto",
    dry_run: bool = False,
) -> InferenceResult:
    """Read ORM facts, plan once in the portable module, and apply the plan."""
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        return InferenceResult(
            dataset_id=dataset_id,
            created_scenes=0,
            assigned_items=0,
            skipped_items=0,
            dry_run=dry_run,
            notes=[f"dataset {dataset_id} not found"],
        )

    scene_count = await db.scalar(
        select(func.count(Scene.id)).where(Scene.dataset_id == dataset_id)
    )
    items: list[DatasetItem] = []
    facts: tuple[PreparedDatasetItem, ...] = ()
    item_count = 0
    assigned_count = 0
    if not scene_count:
        counts = await db.execute(
            select(
                func.count(DatasetItem.id),
                func.count(DatasetItem.id).filter(DatasetItem.scene_id.is_not(None)),
            ).where(DatasetItem.dataset_id == dataset_id)
        )
        item_count, assigned_count = counts.one()
        if item_count and not assigned_count:
            rows = await db.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
            )
            items = list(rows.scalars().all())
            facts = tuple(
                PreparedDatasetItem(
                    id=item.id,
                    file_name=item.file_name,
                    file_path=item.file_path,
                    file_type=item.file_type,
                    scene_id=item.scene_id,
                    frame_index=item.frame_index,
                    logical_path=getattr(item, "logical_path", None)
                    or source_ingest_logical_path(item.metadata_),
                )
                for item in items
            )
    plan = plan_scenes(
        SceneLayoutInput(
            dataset_name=dataset.name,
            items=facts,
            scene_count=scene_count,
            assigned_count=assigned_count,
            item_count=item_count,
            mode=mode,
            dry_run=dry_run,
        )
    )
    if not dry_run:
        await _apply_plan(db, dataset_id, items, plan)

    return InferenceResult(
        dataset_id=dataset_id,
        created_scenes=plan.created_scenes,
        assigned_items=plan.assigned_items,
        skipped_items=plan.skipped_items,
        dry_run=plan.dry_run,
        notes=list(plan.notes),
    )


async def _apply_plan(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    items: list[DatasetItem],
    plan: IngestScenePlan,
) -> None:
    for scene in plan.scenes:
        await scene_svc.create_scene(
            db,
            scene_id=scene.id,
            dataset_id=dataset_id,
            name=scene.name,
            source_format=scene.source_format,
            source_metadata=scene.source_metadata,
        )
    items_by_id = {item.id: item for item in items}
    for assignment in plan.assignments:
        item = items_by_id.get(assignment.item_id)
        if item is None:
            continue
        item.scene_id = assignment.scene_id
        item.frame_index = assignment.frame_index
    await db.flush()

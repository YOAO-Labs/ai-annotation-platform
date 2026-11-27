"""Early reference gates for supported manual task operations."""

from fastapi import HTTPException
from sqlalchemy import select

from app.services.labeling_core_read_relations import task_row
from app.services.reference_keys import PREFIX


async def reject_reference_scope(
    db, *, project_id=None, task_ids=None, item_ids=None, dataset_id=None
):
    from app.services.assets_spatial_views import labeling_dataset_item_facts

    if item_ids is not None or dataset_id is not None:
        item = labeling_dataset_item_facts
        stmt = select(item.id).where(item.file_path.startswith(PREFIX, autoescape=True))
        if item_ids is not None:
            stmt = stmt.where(item.id.in_(item_ids))
        if dataset_id is not None:
            stmt = stmt.where(item.dataset_id == dataset_id)
    else:
        stmt = select(task_row.id).where(
            task_row.file_path.startswith(PREFIX, autoescape=True)
        )
        if project_id is not None:
            stmt = stmt.where(task_row.project_id == project_id)
        if task_ids is not None:
            stmt = stmt.where(task_row.id.in_(task_ids))
    if await db.scalar(stmt.limit(1)):
        raise HTTPException(
            409,
            detail={
                "code": "reference_operation_not_supported",
                "message": "此操作尚未接入原地引用图片，请使用手动标注与严格原图读取",
            },
        )

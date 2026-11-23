"""Read the Labeling projection within the authorized caller's transaction."""

from __future__ import annotations

from fastapi import HTTPException

from yoao_labeling.class_queries import (
    LabelingClassQueryManager,
    materialize_labeling_class_query_data,
)
from yoao_labeling.class_query_contract import (
    LabelingClassQueryUnavailable,
    labeling_class_query_request,
)

from app.services.labeling_class_query_data import LabelingClassQueryData


async def execute_labeling_class_query(db, *, project_id):
    request = labeling_class_query_request(project_id)
    try:
        reply = await LabelingClassQueryManager(LabelingClassQueryData(db)).execute(
            request
        )
        return materialize_labeling_class_query_data(reply)
    except (LabelingClassQueryUnavailable, OSError, TimeoutError):
        raise HTTPException(status_code=503, detail="标注统计服务暂不可用") from None

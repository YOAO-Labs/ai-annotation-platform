from fastapi import APIRouter, Depends

from app.deps import require_active_task_actor

from app.services.reference_access import reference_task_guard

from app.api.v1.tasks import (
    annotation_slices,
    annotations,
    ai_masks,
    cross_frame_jobs,
    lifecycle,
    image_pyramid,
    locks,
    mask_capabilities,
    mask_mutations,
    multicamera_annotations,
    predictions,
    review,
    scene_timeline,
    track_operations,
    video,
)
from app.api.v1.tasks import list as task_list
from app.api.v1.tasks._shared import (
    _attach_dimensions_batch,
    _attach_image_pyramids_batch,
    _linked_source_references,
    _task_with_url,
)

# 各子 router 在此聚合并统一加 /tasks 前缀(下放自原 router.py 的 include 前缀)。
# 前缀必须在此层施加:list_tasks 的 `GET ""` 空路径若以空前缀做嵌套 include,
# FastAPI 会报 "Prefix and path cannot be both empty";加上 /tasks 即合法,
# 且对外路径与拆分前完全一致(/tasks 根列表端点)。
router = APIRouter(
    dependencies=[
        Depends(require_active_task_actor),
        Depends(reference_task_guard),
    ]
)
router.include_router(task_list.router, prefix="/tasks")
router.include_router(scene_timeline.router, prefix="/tasks")
router.include_router(cross_frame_jobs.router, prefix="/tasks")
router.include_router(track_operations.router, prefix="/tasks")
router.include_router(video.router, prefix="/tasks")
router.include_router(image_pyramid.router, prefix="/tasks")
router.include_router(mask_capabilities.router, prefix="/tasks")
router.include_router(mask_mutations.router, prefix="/tasks")
router.include_router(annotation_slices.router, prefix="/tasks")
router.include_router(multicamera_annotations.router, prefix="/tasks")
router.include_router(annotations.router, prefix="/tasks")
router.include_router(ai_masks.router, prefix="/tasks")
router.include_router(predictions.router, prefix="/tasks")
router.include_router(lifecycle.router, prefix="/tasks")
router.include_router(review.router, prefix="/tasks")
router.include_router(locks.router, prefix="/tasks")

__all__ = [
    "router",
    "_attach_dimensions_batch",
    "_attach_image_pyramids_batch",
    "_linked_source_references",
    "_task_with_url",
]

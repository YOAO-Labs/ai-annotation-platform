"""Bridge tracker-produced Mask bytes into the Labeling GC root authority."""

from __future__ import annotations

from app.services.mask_gc import register_tracker_running_mask_root
from app.services.video_tracking.execution import (
    TrackerExecutionCancelled,
    fence_tracker_write,
)


class TrackerMaskRootOwnerChanged(Exception):
    """The tracker Job stopped owning output before its Mask root was recorded."""


async def retain_tracker_output_mask(db, *, job_id, task_id, object_key: str) -> None:
    try:
        await fence_tracker_write(db, job_id, "mask_root")
    except TrackerExecutionCancelled:
        raise TrackerMaskRootOwnerChanged from None
    if not await register_tracker_running_mask_root(
        db, job_id=job_id, task_id=task_id, object_key=object_key
    ):
        raise TrackerMaskRootOwnerChanged

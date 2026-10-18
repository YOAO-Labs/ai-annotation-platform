"""Explicit operator-side admission for accepted Assets image backfills."""

from __future__ import annotations

from uuid import UUID

from aap_assets_images.contract import (
    AcceptedImageWork,
    ImageBackfillSelection,
    ImageBackfillSpec,
    ScheduleImageMediaRequest,
)
from aap_assets_images.policy import ImagePolicy


async def accept_image_backfill(
    selection: ImageBackfillSelection,
    *,
    operation_id: UUID,
    policy: ImagePolicy | None = None,
) -> AcceptedImageWork:
    """Publish explicit, cutoff-frozen operator intent through the image owner."""
    from app.services.assets_images import image_gateway, image_policy

    if selection.accepted_before is None:
        raise ValueError("operator backfill requires a frozen accepted_before cutoff")
    request = ScheduleImageMediaRequest(
        operation_id=operation_id,
        owner_kind=None,
        owner_id=None,
        spec=ImageBackfillSpec(
            selection=selection,
            policy=policy or image_policy(),
        ),
    )
    async with image_gateway() as owner:
        return await owner.call("schedule_image_media", request)

"""Rebuild persisted video frame timetables through current asset owners."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.services.dataset import DatasetService
from app.services.media_probe import probe_video_frame_timetable
from app.services.storage import StorageService
from app.services.video_frame_service import (
    select_sparse_anchor_rows,
    source_key_for_item,
)


class VideoTimetableRebuildWorkflow:
    def __init__(self, db: AsyncSession, storage: StorageService | None = None) -> None:
        self.db = db
        self.storage = storage
        self.datasets = DatasetService(db)

    async def select_items(
        self,
        *,
        dataset_item_ids: Sequence[uuid.UUID],
        dataset_id: uuid.UUID | None,
        all_items: bool,
        limit: int | None,
    ) -> list[DatasetItem]:
        selectors = sum([bool(dataset_item_ids), dataset_id is not None, all_items])
        if selectors != 1:
            raise SystemExit(
                "Pass exactly one selector: --dataset-item-id, --dataset-id, or --all"
            )

        statement = select(DatasetItem).where(DatasetItem.file_type == "video")
        if dataset_item_ids:
            statement = statement.where(DatasetItem.id.in_(dataset_item_ids))
        elif dataset_id is not None:
            statement = statement.where(DatasetItem.dataset_id == dataset_id)
        statement = statement.order_by(DatasetItem.created_at.asc())
        if limit is not None:
            statement = statement.limit(max(0, limit))
        return (await self.db.execute(statement)).scalars().all()

    async def rebuild_item(
        self,
        item: DatasetItem,
        *,
        dry_run: bool = False,
        sparse_stride: int = 1,
    ) -> int:
        storage = self.storage or StorageService()
        source_key = source_key_for_item(item)
        with tempfile.TemporaryDirectory(prefix="anno-video-timetable-") as tmp:
            suffix = Path(item.file_name).suffix or ".mp4"
            input_path = Path(tmp) / f"source{suffix}"
            with input_path.open("wb") as file:
                storage.client.download_fileobj(
                    Bucket=storage.bucket_for_cache_key(source_key),
                    Key=source_key,
                    Fileobj=file,
                )
            rows = probe_video_frame_timetable(input_path)

        # Sparse persistence keeps grid anchors and keyframes, while the recorded
        # frame count remains the total number of source frames.
        persisted_rows = select_sparse_anchor_rows(rows, sparse_stride)
        if not dry_run:
            await self.datasets.replace_video_frame_timetable(
                item, persisted_rows, total_frames=len(rows)
            )
        return len(persisted_rows)

    async def record_item_error(self, item_id: uuid.UUID, error: str) -> None:
        await self.datasets.record_video_frame_timetable_error(item_id, error)

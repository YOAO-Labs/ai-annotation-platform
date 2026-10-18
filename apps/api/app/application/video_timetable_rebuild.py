"""Rebuild persisted video frame timetables through the Assets media owner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import uuid
from typing import Sequence

from aap_assets_media.contract import (
    BATCH_SIZE,
    BackfillRequest,
    BackfillSelection,
    MediaFailure,
    MediaSource,
    ScheduleWorkRequest,
    TimetableSpec,
    WorkStatusRequest,
)
from app.services.assets_media import (
    media_gateway,
    probe_video_timetable,
    run_media_job,
)


class VideoTimetableRebuildWorkflow:
    """Own CLI selection, dry-run probing, and durable timetable rebuilds."""

    def __init__(self, *, gateway_factory=media_gateway) -> None:
        self.gateway_factory = gateway_factory

    async def select_items(
        self,
        *,
        dataset_item_ids: Sequence[uuid.UUID],
        dataset_id: uuid.UUID | None,
        all_items: bool,
        limit: int | None,
    ) -> list[MediaSource]:
        if sum((bool(dataset_item_ids), dataset_id is not None, all_items)) != 1:
            raise SystemExit(
                "Pass exactly one selector: --dataset-item-id, --dataset-id, or --all"
            )

        normalized_limit = None if limit is None else max(0, limit)
        if normalized_limit == 0:
            return []

        accepted_before = datetime.now(UTC)
        requested_ids = list(dict.fromkeys(dataset_item_ids))
        id_batches = (
            [
                requested_ids[index : index + BATCH_SIZE]
                for index in range(0, len(requested_ids), BATCH_SIZE)
            ]
            if requested_ids
            else [[]]
        )
        selected: list[MediaSource] = []
        seen: set[uuid.UUID] = set()

        async with self.gateway_factory() as gateway:
            for id_batch in id_batches:
                selection = BackfillSelection(
                    dataset_id=dataset_id,
                    dataset_item_ids=id_batch,
                    all_items=all_items,
                    only_missing=False,
                    accepted_before=accepted_before,
                )
                cursor: uuid.UUID | None = None
                snapshot_digest: str | None = None
                total: int | None = None
                page_items = 0
                while True:
                    request = BackfillRequest(
                        selection=selection,
                        after_item_id=cursor,
                        page_size=BATCH_SIZE,
                    )
                    page = await gateway.call("scan_video_backfill", request)
                    if (
                        page.after_item_id != cursor
                        or page.accepted_before != accepted_before
                    ):
                        raise MediaFailure("invalid_response")
                    if snapshot_digest is None:
                        snapshot_digest = page.snapshot_digest
                        total = page.total
                    elif (page.snapshot_digest, page.total) != (snapshot_digest, total):
                        raise MediaFailure("invalid_response")

                    for source in page.items:
                        if (
                            source.dataset_item_id in seen
                            or source.file_type != "video"
                            or (cursor is not None and source.dataset_item_id <= cursor)
                            or (
                                dataset_id is not None
                                and source.dataset_id != dataset_id
                            )
                            or (
                                id_batch and source.dataset_item_id not in set(id_batch)
                            )
                        ):
                            raise MediaFailure("invalid_response")
                        seen.add(source.dataset_item_id)
                        selected.append(source)
                        page_items += 1

                    if (
                        normalized_limit is not None
                        and len(selected) >= normalized_limit
                    ):
                        return selected[:normalized_limit]
                    if page.complete:
                        if page.next_item_id is not None or page_items != page.total:
                            raise MediaFailure("invalid_response")
                        break
                    if (
                        page.next_item_id is None
                        or not page.items
                        or page.next_item_id != page.items[-1].dataset_item_id
                        or (cursor is not None and page.next_item_id <= cursor)
                    ):
                        raise MediaFailure("invalid_response")
                    cursor = page.next_item_id
        return selected

    async def rebuild_item(
        self,
        source: MediaSource,
        *,
        dry_run: bool = False,
        sparse_stride: int = 1,
    ) -> int:
        if sparse_stride < 1:
            raise ValueError("sparse_stride must be at least 1")
        if dry_run:
            rows = await probe_video_timetable(source, sparse_stride=sparse_stride)
            return len(rows)

        async with self.gateway_factory() as gateway:
            accepted = await gateway.call(
                "schedule_video_media",
                ScheduleWorkRequest(
                    operation_id=uuid.uuid4(),
                    dataset_item_id=source.dataset_item_id,
                    spec=TimetableSpec(
                        sparse_stride=sparse_stride,
                        expected_source_snapshot=source.source_snapshot,
                    ),
                    force=True,
                ),
            )
            job_id = accepted.work.job_id

            while True:
                # Local mode runs one accepted job. In remote mode this returns False;
                # the independently deployed owner continues the durable work.
                await run_media_job(job_id)
                status = await gateway.call(
                    "read_video_work", WorkStatusRequest(job_id=job_id)
                )
                if status.work.status == "ready":
                    count = status.progress.get("timetable_rows")
                    if type(count) is not int or count < 0:
                        raise MediaFailure("invalid_response")
                    return count
                if status.work.status in {"failed", "canceled"}:
                    raise MediaFailure("storage_unavailable")
                await asyncio.sleep(0.5)

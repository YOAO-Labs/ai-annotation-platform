"""Cursor-based, opt-in backfill for eligible immutable image pyramids.

Run from ``apps/api``:

    PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py --dry-run
    PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py \
      --cursor <dataset-item-uuid> --limit 100

DatasetItem mode filters by persisted logical dimensions. Direct Task mode is
separate and opt-in because legacy tasks have no persisted dimensions; the worker
probes and applies hard admission before generating.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import select

from app.config import settings
from app.db.base import async_session
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.workers.image_pyramid import enqueue_image_pyramid


async def _dataset_items(cursor: uuid.UUID | None, limit: int) -> list[DatasetItem]:
    statement = (
        select(DatasetItem)
        .where(
            DatasetItem.file_type == "image",
            DatasetItem.width.is_not(None),
            DatasetItem.height.is_not(None),
            DatasetItem.width * DatasetItem.height
            >= settings.image_pyramid_optional_pixels,
        )
        .order_by(DatasetItem.id)
        .limit(limit)
    )
    if cursor is not None:
        statement = statement.where(DatasetItem.id > cursor)
    async with async_session() as db:
        return list((await db.execute(statement)).scalars().all())


async def _direct_tasks(cursor: uuid.UUID | None, limit: int) -> list[Task]:
    statement = (
        select(Task)
        .where(Task.file_type == "image", Task.dataset_item_id.is_(None))
        .order_by(Task.id)
        .limit(limit)
    )
    if cursor is not None:
        statement = statement.where(Task.id > cursor)
    async with async_session() as db:
        return list((await db.execute(statement)).scalars().all())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner-kind",
        choices=("dataset_item", "task"),
        default="dataset_item",
        help="task means legacy direct tasks only",
    )
    parser.add_argument("--cursor", type=uuid.UUID)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")

    rows = (
        await _dataset_items(args.cursor, args.limit)
        if args.owner_kind == "dataset_item"
        else await _direct_tasks(args.cursor, args.limit)
    )
    queued = 0
    if not args.dry_run:
        from app.services import image_pyramid_originals as originals

        async with async_session() as db:
            for row in rows:
                source = await originals.pin(db, args.owner_kind, row.id)
                enqueue_image_pyramid(
                    args.owner_kind,
                    row.id,
                    force=args.owner_kind == "task",
                    delegation=originals.system_authorization(
                        args.owner_kind, row.id, source
                    ),
                )
                queued += 1
    next_cursor = str(rows[-1].id) if len(rows) == args.limit else None
    print(
        json.dumps(
            {
                "owner_kind": args.owner_kind,
                "dry_run": args.dry_run,
                "eligible": len(rows),
                "queued": queued,
                "next_cursor": next_cursor,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import async_session, engine
from app.db.models.dataset import DatasetItem
from app.services.video_frame_service import select_sparse_anchor_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild video_frame_indices for existing video dataset items.",
    )
    parser.add_argument(
        "--dataset-item-id",
        action="append",
        default=[],
        help="Video DatasetItem UUID. Can be passed multiple times.",
    )
    parser.add_argument("--dataset-id", help="Dataset UUID containing video items.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Rebuild all video dataset items.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--sparse-stride",
        type=int,
        default=1,
        help=(
            "长视频 sparse timetable: 每隔 N 帧存一个 pts_ms 锚点 (+ 所有关键帧), "
            "中间帧由相邻锚点线性插值。默认 1 = 全帧 (不 sparse)。"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    return parser


def _parse_uuid(raw: str, flag: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise SystemExit(f"{flag} must be a UUID: {raw}") from exc


async def select_video_items(
    db: AsyncSession,
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

    stmt = select(DatasetItem).where(DatasetItem.file_type == "video")
    if dataset_item_ids:
        stmt = stmt.where(DatasetItem.id.in_(dataset_item_ids))
    elif dataset_id is not None:
        stmt = stmt.where(DatasetItem.dataset_id == dataset_id)
    stmt = stmt.order_by(DatasetItem.created_at.asc())
    if limit is not None:
        stmt = stmt.limit(max(0, limit))
    return (await db.execute(stmt)).scalars().all()


async def rebuild_item_timetable(
    db: AsyncSession,
    item: DatasetItem,
    *,
    storage: object | None = None,
    dry_run: bool = False,
    sparse_stride: int = 1,
) -> int:
    from app.db.models.video_derivative import VideoDerivativeOutput
    from app.services import video_derivative_originals as originals
    from app.services import video_derivatives as derivatives
    from app.services.content_snapshot import ContentError
    from app.workers.media_derivatives import run_outputs

    source = await originals.pin(db, "dataset_item", item.id)
    delegation = originals.system_authorization("dataset_item", item.id, source)
    authority = await originals.delegated(db, delegation)
    stride = max(1, int(sparse_stride))
    if dry_run:
        try:
            current = await derivatives.metadata(db, item, source)
            _, frames = derivatives.metadata_result(current)
        except ContentError as exc:
            if exc.code not in {
                "video_metadata_unpinned",
                "video_metadata_changed",
                "video_metadata_invalid",
            }:
                raise
            return 0
        return len(select_sparse_anchor_rows(frames, stride))

    output, _ = await derivatives.prepare(
        db,
        item.id,
        authority,
        "metadata",
        spec={"schema": 1, "timetable_stride": stride},
        force=True,
    )
    output_id, item_id = output.id, item.id
    await db.commit()
    await run_outputs([str(output_id)])
    current_item = await db.get(DatasetItem, item_id, populate_existing=True)
    current_output = await db.get(
        VideoDerivativeOutput, output_id, populate_existing=True
    )
    if (
        current_item is None
        or current_output is None
        or current_item.video_output_id != output_id
    ):
        raise ContentError("video_metadata_rebuild_failed", 409)
    # Recheck the exact pinned original before accepting the newly published owner.
    current_source = await originals.check(db, authority)
    published = await derivatives.metadata(db, current_item, current_source)
    info, frames = derivatives.metadata_result(published)
    if info.frame_timetable_error:
        raise ContentError("video_timetable_rebuild_failed", 409)
    return len(select_sparse_anchor_rows(frames, stride))


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_item_ids = [
        _parse_uuid(raw, "--dataset-item-id") for raw in args.dataset_item_id
    ]
    dataset_id = (
        _parse_uuid(args.dataset_id, "--dataset-id") if args.dataset_id else None
    )

    async with async_session() as db:
        items = await select_video_items(
            db,
            dataset_item_ids=dataset_item_ids,
            dataset_id=dataset_id,
            all_items=args.all,
            limit=args.limit,
        )
        failures = 0
        for item in items:
            try:
                count = await rebuild_item_timetable(
                    db, item, dry_run=args.dry_run, sparse_stride=args.sparse_stride
                )
                prefix = "would rebuild" if args.dry_run else "rebuilt"
                suffix = " (sparse)" if args.sparse_stride > 1 else ""
                print(f"{prefix} {item.id}: {count} anchor rows{suffix}")
            except Exception as exc:
                failures += 1
                await db.rollback()
                print(f"failed {item.id}: {exc}")
                if not args.keep_going:
                    break

    await engine.dispose()
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()

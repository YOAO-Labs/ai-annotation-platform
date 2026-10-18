from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Sequence

from app.application.video_timetable_rebuild import VideoTimetableRebuildWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild persisted video frame timetables for selected videos.",
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


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset_item_ids = [
        _parse_uuid(raw, "--dataset-item-id") for raw in args.dataset_item_id
    ]
    dataset_id = (
        _parse_uuid(args.dataset_id, "--dataset-id") if args.dataset_id else None
    )

    workflow = VideoTimetableRebuildWorkflow()
    items = await workflow.select_items(
        dataset_item_ids=dataset_item_ids,
        dataset_id=dataset_id,
        all_items=args.all,
        limit=args.limit,
    )
    failures = 0
    for source in items:
        try:
            count = await workflow.rebuild_item(
                source, dry_run=args.dry_run, sparse_stride=args.sparse_stride
            )
            prefix = "would rebuild" if args.dry_run else "rebuilt"
            suffix = " (sparse)" if args.sparse_stride > 1 else ""
            print(f"{prefix} {source.dataset_item_id}: {count} anchor rows{suffix}")
        except Exception as exc:
            failures += 1
            print(f"failed {source.dataset_item_id}: {exc}")
            if not args.keep_going:
                break
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()

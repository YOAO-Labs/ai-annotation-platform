"""Accept a bounded, cursor-based image-pyramid backfill through Assets.

Run from ``apps/api`` with the API environment loaded. The command publishes a
durable ``ImageBackfillSpec`` with an explicit operation ID and fixed cutoff;
the Assets worker performs source observation and child-job admission.

    PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py --dry-run
    PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py \
      --cursor <owner-uuid> --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import uuid

from aap_assets_images.contract import (
    ImageBackfillSelection,
    ScanImageBackfillRequest,
)
from app.services.assets_images import image_gateway, image_policy
from scripts.image_media_backfill import accept_image_backfill

_PAGE_SIZE = 500


async def _eligible_owner_ids(
    *,
    owner_kind: str,
    dataset_id: uuid.UUID | None,
    cursor: uuid.UUID | None,
    limit: int,
    accepted_before: datetime | None = None,
) -> tuple[list[uuid.UUID], datetime]:
    policy = image_policy()
    accepted_before = accepted_before or datetime.now(timezone.utc)
    selection = ImageBackfillSelection(
        owner_kind=owner_kind,
        dataset_id=dataset_id,
        all_owners=dataset_id is None,
        purpose="pyramid",
        only_missing=True,
        accepted_before=accepted_before,
    )
    selected: list[uuid.UUID] = []
    after_id = cursor
    async with image_gateway() as owner:
        while len(selected) < limit:
            page = await owner.call(
                "scan_image_backfill",
                ScanImageBackfillRequest(
                    selection=selection,
                    after_owner_kind=owner_kind if after_id is not None else None,
                    after_owner_id=after_id,
                    page_size=min(_PAGE_SIZE, max(1, limit - len(selected))),
                ),
            )
            for source in page.items:
                if owner_kind == "task" and source.item_id is not None:
                    continue
                if owner_kind == "dataset_item" and not policy.pyramid_eligible(
                    source.width, source.height
                ):
                    continue
                if source.source_drifted or not source.upload_ready:
                    continue
                selected.append(source.owner.id)
                if len(selected) == limit:
                    break
            if len(selected) == limit or page.complete:
                break
            if page.next_owner_id is None:
                break
            after_id = page.next_owner_id
    return selected, accepted_before


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner-kind",
        choices=("dataset_item", "task"),
        default="dataset_item",
        help="task means direct legacy Tasks only",
    )
    parser.add_argument("--dataset-id", type=uuid.UUID)
    parser.add_argument("--cursor", type=uuid.UUID)
    parser.add_argument("--operation-id", type=uuid.UUID)
    parser.add_argument(
        "--accepted-before",
        type=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
        help="reuse the exact cutoff when replaying --operation-id",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= _PAGE_SIZE:
        parser.error(f"--limit must be between 1 and {_PAGE_SIZE}")
    if args.owner_kind == "task" and args.dataset_id is not None:
        parser.error("--dataset-id is not valid for task mode")
    if args.accepted_before is not None and args.accepted_before.tzinfo is None:
        parser.error("--accepted-before must include a timezone")

    owner_ids, accepted_before = await _eligible_owner_ids(
        owner_kind=args.owner_kind,
        dataset_id=args.dataset_id,
        cursor=args.cursor,
        limit=args.limit,
        accepted_before=args.accepted_before,
    )
    operation_id = args.operation_id or uuid.uuid4()
    accepted = None
    if owner_ids and not args.dry_run:
        accepted = await accept_image_backfill(
            ImageBackfillSelection(
                owner_kind=args.owner_kind,
                owner_ids=owner_ids,
                purpose="pyramid",
                only_missing=True,
                accepted_before=accepted_before,
            ),
            operation_id=operation_id,
        )
    print(
        json.dumps(
            {
                "owner_kind": args.owner_kind,
                "dataset_id": str(args.dataset_id) if args.dataset_id else None,
                "dry_run": args.dry_run,
                "eligible": len(owner_ids),
                "accepted_before": accepted_before.isoformat(),
                "operation_id": str(operation_id),
                "job_id": str(accepted.work.job_id) if accepted else None,
                "next_cursor": (
                    str(owner_ids[-1]) if len(owner_ids) == args.limit else None
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

"""Map actual legacy keys without reading/moving objects or touching item rows.

Run from apps/api. Select --dataset-id/--all-datasets and one explicit mode.
Apply commits bounded batches. Re-running safely rescans; --after accepts a
reported item UUID cursor. Dry-run/verify use database-enforced READ ONLY.
Reports contain IDs/counts only. Unverified means mapped, never byte-verified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402

from app.db.models.dataset import Dataset, DatasetItem  # noqa: E402
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation  # noqa: E402
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    ensure_legacy_bindings,
    legacy_dataset_binding,
    register_item_asset,
    validate_legacy_bindings,
)  # noqa: E402
from app.services.storage import storage_service  # noqa: E402


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument("--dataset-id", type=uuid.UUID)
    target.add_argument("--all-datasets", action="store_true")
    mode = command.add_mutually_exclusive_group(required=True)
    for name in ("dry-run", "apply", "verify"):
        mode.add_argument(f"--{name}", dest="mode", action="store_const", const=name)
    command.add_argument(
        "--batch-size", type=int, default=100, choices=range(1, 501), metavar="1..500"
    )
    command.add_argument("--after", type=uuid.UUID)
    return command


async def backfill_batch(db, *, mode, dataset_id=None, after=None, batch_size=100):
    if mode not in {"apply", "verify", "dry-run"} or not 1 <= batch_size <= 500:
        raise ValueError("Invalid backfill mode or batch size")
    # Bootstrap bindings before item locks, matching online import lock order.
    if mode == "apply":
        await ensure_legacy_bindings(db)
    binding = legacy_dataset_binding() if mode == "apply" else None
    from app.services.reference_keys import PREFIX

    stmt = (
        select(DatasetItem)
        .where(~DatasetItem.file_path.startswith(PREFIX, autoescape=True))
        .order_by(DatasetItem.id)
        .limit(batch_size)
    )
    if dataset_id:
        stmt = stmt.where(DatasetItem.dataset_id == dataset_id)
    if after:
        stmt = stmt.where(DatasetItem.id > after)
    if mode == "apply":
        stmt = stmt.with_for_update()
    items = list(await db.scalars(stmt))
    report = dict(scanned=len(items), created=0, already_mapped=0, pending=0, invalid=0)
    for item in items:
        asset = await db.scalar(
            select(DatasetAsset).where(DatasetAsset.dataset_item_id == item.id)
        )
        replicas = (
            list(
                await db.scalars(
                    select(DatasetAssetLocation).where(
                        DatasetAssetLocation.asset_id == asset.id
                    )
                )
            )
            if asset
            else []
        )
        if not item.file_path:
            report["invalid"] += 1
            continue
        if asset and replicas:
            valid = any(
                replica.location_id == LEGACY_LOCATION_ID
                and replica.bucket == storage_service.datasets_bucket
                and replica.object_key == item.file_path
                for replica in replicas
            )
            report["already_mapped" if valid else "invalid"] += 1
        elif mode == "apply":
            await register_item_asset(db, item, binding=binding)
            report["created"] += 1
        else:
            report["pending"] += 1
    cursor = items[-1].id if items else after
    return report, cursor


async def run(args, session_factory, emit):
    configuration_findings = []
    if args.mode != "apply":
        async with session_factory() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            configuration_findings = await validate_legacy_bindings(db)
            if configuration_findings:
                emit({"type": "configuration", "findings": configuration_findings})
    if args.dataset_id:
        async with session_factory() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            if await db.get(Dataset, args.dataset_id) is None:
                raise ValueError("Selected dataset does not exist")
    totals = dict(scanned=0, created=0, already_mapped=0, pending=0, invalid=0)
    after = args.after
    while True:
        async with session_factory() as db:
            if args.mode != "apply":
                await db.execute(text("SET TRANSACTION READ ONLY"))
            report, cursor = await backfill_batch(
                db,
                mode=args.mode,
                dataset_id=args.dataset_id,
                after=after,
                batch_size=args.batch_size,
            )
            if args.mode == "apply":
                await db.commit()
            for key in totals:
                totals[key] += report[key]
            emit(
                {
                    "type": "batch",
                    "mode": args.mode,
                    "after": str(cursor) if cursor else None,
                    **report,
                }
            )
        after = cursor
        if report["scanned"] < args.batch_size:
            break
    emit(
        {
            "type": "summary",
            "mode": args.mode,
            "configuration_errors": len(configuration_findings),
            **totals,
        }
    )
    return (
        1
        if configuration_findings
        or totals["invalid"]
        or (args.mode == "verify" and totals["pending"])
        else 0
    )


async def main(args):
    from app.db.base import async_session, engine

    try:
        return await run(
            args, async_session, lambda event: print(json.dumps(event, sort_keys=True))
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        result = asyncio.run(main(parser().parse_args()))
    except Exception as error:
        print(
            json.dumps(
                {
                    "type": "error",
                    "error_type": type(error).__name__,
                    "reason": "Backfill stopped; committed apply batches can be resumed.",
                }
            ),
            file=sys.stderr,
        )
        result = 2
    raise SystemExit(result)

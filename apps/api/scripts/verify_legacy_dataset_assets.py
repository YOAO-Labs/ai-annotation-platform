"""Preview current legacy bytes, then apply only a reviewed, unchanged plan.

This complements locator backfill; it neither moves originals nor changes items.
Run from apps/api with the intended database and platform storage configuration.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models.dataset import Dataset, DatasetItem  # noqa: E402
from app.db.models.dataset_asset import DatasetAsset  # noqa: E402
from app.services import legacy_asset_verification as service  # noqa: E402
from app.services.content_snapshot import ContentError  # noqa: E402
from app.services.reference_keys import PREFIX  # noqa: E402


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply-plan", type=Path)
    target = command.add_mutually_exclusive_group()
    target.add_argument("--item-id", type=uuid.UUID)
    target.add_argument("--dataset-id", type=uuid.UUID)
    target.add_argument("--all-datasets", action="store_true")
    command.add_argument("--output", type=Path)
    command.add_argument("--after", type=uuid.UUID)
    command.add_argument(
        "--batch-size", type=int, default=100, choices=range(1, 501), metavar="1..500"
    )
    return command


def line(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


@contextmanager
def validated_plan(path):
    """Freeze and validate the complete plan before the first business write."""
    with tempfile.TemporaryFile(mode="w+b", prefix="aap-legacy-plan-") as frozen:
        digest, count, complete = hashlib.sha256(), 0, None
        with path.open("rb") as source:
            header = source.readline(65537)
            if len(header) > 65536 or json.loads(header) != {
                "type": "plan",
                "schema": 1,
            }:
                raise ValueError("Invalid legacy verification plan header")
            frozen.write(header)
            digest.update(header)
            while raw := source.readline(65537):
                if len(raw) > 65536:
                    raise ValueError("Legacy verification plan line is too large")
                record = json.loads(raw)
                if record.get("type") == "complete":
                    if (
                        set(record) != {"type", "count", "failed", "sha256"}
                        or type(record["count"]) is not int
                        or record["count"] != count
                        or type(record["failed"]) is not int
                        or record["failed"] < 0
                        or record["sha256"] != digest.hexdigest()
                        or source.read(1)
                    ):
                        raise ValueError(
                            "Legacy verification plan is incomplete or changed"
                        )
                    complete = record
                    break
                if set(record) != {"type", "preview"} or record["type"] != "asset":
                    raise ValueError("Invalid legacy verification plan entry")
                service.Preview.parse(record["preview"])
                digest.update(raw)
                frozen.write(raw)
                count += 1
        if complete is None:
            raise ValueError("Legacy verification plan has no completion record")
        frozen.seek(0)
        frozen.readline()
        yield frozen, complete


async def _ids(args, factory, after):
    statement = select(DatasetItem.id).order_by(DatasetItem.id).limit(args.batch_size)
    if args.item_id:
        statement = statement.where(DatasetItem.id == args.item_id)
    else:
        statement = statement.outerjoin(
            DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id
        ).where(
            DatasetAsset.id.is_(None) | (DatasetAsset.verification_state != "verified")
        )
        statement = statement.where(
            ~DatasetItem.file_path.startswith(PREFIX, autoescape=True)
        )
    if args.dataset_id:
        statement = statement.where(DatasetItem.dataset_id == args.dataset_id)
    if after:
        statement = statement.where(DatasetItem.id > after)
    async with factory() as db:
        return list(await db.scalars(statement))


async def run(args, factory, emit):
    totals = {"previewed": 0, "verified": 0, "already_verified": 0, "failed": 0}

    async def execute(item_id, expected=None):
        try:
            result = await service.verify(factory, item_id, expected=expected)
        except ContentError as exc:
            totals["failed"] += 1
            emit(
                {
                    "type": "item",
                    "item_id": str(item_id),
                    "status": "failed",
                    "code": exc.code,
                }
            )
            return None
        totals["previewed" if result["status"] == "preview" else result["status"]] += 1
        emit({"type": "item", "item_id": str(item_id), "status": result["status"]})
        return result

    if args.preview:
        if not args.output or not (
            args.item_id or args.dataset_id or args.all_datasets
        ):
            raise ValueError("Preview requires an explicit target and output path")
        if args.dataset_id:
            async with factory() as db:
                if await db.get(Dataset, args.dataset_id) is None:
                    raise ValueError("Selected dataset does not exist")
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            digest = hashlib.sha256()

            def write(value):
                raw = line(value)
                output.write(raw)
                digest.update(raw)

            write({"type": "plan", "schema": 1})
            after = args.after
            while items := await _ids(args, factory, after):
                for item_id in items:
                    result = await execute(item_id)
                    if result and result["status"] == "preview":
                        write(
                            {
                                "type": "asset",
                                "preview": {
                                    key: value
                                    for key, value in result.items()
                                    if key != "status"
                                },
                            }
                        )
                after = items[-1]
                output.flush()
                emit({"type": "batch", "after": str(after)})
            if args.item_id and sum(totals.values()) == 0:
                raise ValueError("Selected item does not exist")
            output.write(
                line(
                    {
                        "type": "complete",
                        "count": totals["previewed"],
                        "failed": totals["failed"],
                        "sha256": digest.hexdigest(),
                    }
                )
            )
            output.flush()
            os.fsync(output.fileno())
    else:
        if (
            args.output
            or args.item_id
            or args.dataset_id
            or args.all_datasets
            or args.after
        ):
            raise ValueError("Apply uses only the explicit identities in its plan")
        with validated_plan(args.apply_plan) as (frozen, completion):
            for raw in frozen:
                expected = service.Preview.parse(json.loads(raw)["preview"])
                await execute(expected.item_id, expected)
            totals["failed"] += completion["failed"]
    emit({"type": "summary", **totals})
    return 1 if totals["failed"] else 0


async def main(args):
    from app.db.base import async_session, engine

    try:
        return await run(
            args, async_session, lambda value: print(json.dumps(value, sort_keys=True))
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
                    "reason": "Legacy verification stopped; completed item publications can be retried.",
                }
            ),
            file=sys.stderr,
        )
        result = 2
    raise SystemExit(result)

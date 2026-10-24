"""Inspect exact saved temporary versions and keep real retention deadlines."""

import asyncio
from datetime import datetime, timezone
import time

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.db.models.prediction_input import PredictionInputLease
from app.db.models.storage_maintenance import StorageOperation
from app.services import prediction_inputs


async def _records(factory, task_id):
    async with factory() as db:
        rows = list(
            await db.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.task_id == task_id,
                )
            )
        )
        purposes = [prediction_inputs.purpose(row.id) for row in rows]
        assert not await db.scalar(
            select(StorageOperation.id).where(
                StorageOperation.purpose.in_(purposes),
            )
        ), "A test-owned temporary input still has unresolved physical operations"
        return rows


async def retire_known_inputs(factory, task_id, *, wait_seconds=90):
    """Retry known cleanup at its actual due time, without rewriting any TTL.

    Uncertain writes retain their original responsibility. A failing acceptance
    run must report that state instead of deleting its recovery record or making
    its seven-day retention appear to have elapsed.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        rows = await _records(factory, task_id)
        pending = [row for row in rows if row.state != "deleted"]
        if not pending:
            return rows
        now = datetime.now(timezone.utc)
        for row in pending:
            assert not (row.uncertain_write and row.retain_until > now), (
                "Uncertain input responsibility is retained until its real deadline"
            )
            assert row.state != "active" or row.expires_at <= now, (
                "The request/worker did not close its active temporary input"
            )
            if row.next_cleanup_at <= now:
                await prediction_inputs.purge_input(factory, row.id)
        assert time.monotonic() < deadline, "Known temporary cleanup did not settle"
        await asyncio.sleep(0.25)


async def assert_retired_input_versions(factory, task_id, storage, *, observed=None):
    """Require tombstones, their exact version absence and an empty saved prefix."""
    rows = await _records(factory, task_id)
    assert rows and all(row.state == "deleted" for row in rows)
    expected = set()

    def inspect():
        for row in rows:
            prefix = prediction_inputs.input_prefix(row.id)
            ref = prediction_inputs.resolve_ref(row.storage_ref, expected_prefix=prefix)
            assert row.bucket == ref["bucket"]
            assert storage.client.meta.endpoint_url.rstrip("/") == ref["endpoint_url"]
            for relative, entry in row.inventory["objects"].items():
                version = entry["version_id"]
                assert (
                    version not in {None, "", "null"} and entry["state"] == "verified"
                )
                key = prefix + relative
                expected.add((row.bucket, key, version))
                try:
                    storage.client.head_object(
                        Bucket=row.bucket, Key=key, VersionId=version
                    )
                except ClientError as error:
                    assert error.response.get("Error", {}).get("Code") in {
                        "404",
                        "NoSuchKey",
                        "NoSuchVersion",
                        "NotFound",
                    }
                else:
                    raise AssertionError("A deleted input's exact version still exists")
            current = storage.client.list_objects_v2(Bucket=row.bucket, Prefix=prefix)
            versions = storage.client.list_object_versions(
                Bucket=row.bucket, Prefix=prefix
            )
            assert not current.get("IsTruncated") and not current.get("Contents")
            assert not versions.get("IsTruncated")
            assert not versions.get("Versions") and not versions.get("DeleteMarkers")

    await asyncio.to_thread(inspect)
    if observed is not None:
        assert set(observed) == expected
    return rows

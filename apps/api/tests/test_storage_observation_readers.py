"""Actual strict read owners count completed attempts without changing authority."""

import asyncio
from contextlib import contextmanager
import hashlib
import threading
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.db.models.storage_observation_rate import StorageObservationBucket as Bucket
from app.services import dataset_content as content, dataset_references as references
from app.services import reference_storage, storage_fault_events as faults
from app.services import storage_observation_rates as rates
from app.services.content_snapshot import ContentError
from tests.test_dataset_content import content_world as content_world
from tests.test_storage_fault_events import world as world


@pytest.fixture
async def rate_world(world, test_engine):
    source_id = world[5].id
    yield world
    async with test_engine.begin() as db:
        await db.execute(delete(Bucket).where(Bucket.source_id == source_id))


async def totals(engine, source_id, metric="original_read"):
    async with engine.connect() as db:
        rows = (
            await db.execute(
                select(Bucket.success_count, Bucket.failure_count).where(
                    Bucket.source_id == source_id, Bucket.metric == metric
                )
            )
        ).all()
    return tuple(sum(row[index] for row in rows) for index in (0, 1))


async def scan(db, world, **kwargs):
    snapshot = await content.scan_snapshot(
        db, world[6].id, world[7].path_digest, world[2], verify=True, **kwargs
    )
    snapshot.close()


async def test_actual_scan_success_and_missing_file_have_independent_counts(
    rate_world, db_session, test_engine
):
    w = rate_world
    source_id = w[5].id
    await scan(db_session, w)
    await scan(db_session, w)
    (w[0] / w[7].relative_path).unlink()
    with pytest.raises(Exception, match="path_unavailable"):
        await scan(db_session, w)
    await db_session.rollback()
    assert await totals(test_engine, source_id) == (2, 1)


async def test_registration_two_safety_reads_and_reference_read_each_count(
    rate_world, db_session, test_engine
):
    w = rate_world
    result = await references.register_entry(
        db_session, w[6].id, w[7].path_digest, w[2]
    )
    assert await totals(test_engine, w[5].id) == (2, 0)
    snapshot = await references.reference_snapshot(
        db_session, result["item_id"], w[2], expected_sha256=result["sha256"]
    )
    snapshot.close()
    assert await totals(test_engine, w[5].id) == (3, 0)
    (w[0] / w[7].relative_path).unlink()
    with pytest.raises(Exception, match="path_unavailable"):
        await references.reference_snapshot(db_session, result["item_id"], w[2])
    assert await totals(test_engine, w[5].id) == (3, 1)


async def test_wrong_client_sha_is_excluded_before_attempt(
    rate_world, db_session, test_engine
):
    w = rate_world
    await scan(db_session, w)
    with pytest.raises(ContentError, match="content_unverified"):
        await content.scan_snapshot(
            db_session, w[6].id, w[7].path_digest, w[2], expected_sha256="0" * 64
        )
    result = await references.register_entry(
        db_session, w[6].id, w[7].path_digest, w[2]
    )
    with pytest.raises(ContentError, match="content_mismatch"):
        await references.reference_snapshot(
            db_session, result["item_id"], w[2], expected_sha256="0" * 64
        )
    assert await totals(test_engine, w[5].id) == (3, 0)


async def test_final_context_rejection_closes_snapshot_without_counting(
    rate_world, db_session, test_engine, monkeypatch
):
    w = rate_world
    captured = []
    original = content.snapshots.capture_from_location

    async def capture(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        captured.append(snapshot)
        return snapshot

    async def reject(_db):
        raise ContentError("configuration_changed")

    monkeypatch.setattr(content.snapshots, "capture_from_location", capture)
    with pytest.raises(ContentError, match="configuration_changed"):
        await scan(db_session, w, before_commit=reject)
    assert captured and captured[0].file.closed
    assert await totals(test_engine, w[5].id) == (0, 0)


@pytest.mark.parametrize("kind", ["unknown", "unsettled", "capacity", "guard"])
async def test_non_source_failures_are_excluded(
    rate_world, db_session, test_engine, monkeypatch, kind
):
    w = rate_world
    failure = {
        "unknown": RuntimeError("private path and provider response"),
        "unsettled": ContentError("source_changed"),
        "capacity": ContentError("snapshot_capacity_exceeded", 429),
        "guard": ContentError("storage_location_stopped"),
    }[kind]
    if kind == "unsettled":
        failure.storage_operation_unsettled = True
    monkeypatch.setattr(
        content.snapshots, "capture_from_location", AsyncMock(side_effect=failure)
    )
    with pytest.raises(type(failure)) as caught:
        await scan(db_session, w)
    assert caught.value is failure
    assert await totals(test_engine, w[5].id) == (0, 0)


async def test_fault_history_failure_does_not_drop_independent_read_failure(
    rate_world, db_session, test_engine, monkeypatch
):
    w = rate_world
    monkeypatch.setattr(faults, "report", AsyncMock(return_value=None))
    (w[0] / w[7].relative_path).unlink()
    with pytest.raises(Exception, match="path_unavailable"):
        await scan(db_session, w)
    assert await totals(test_engine, w[5].id) == (0, 1)


async def test_cancelled_actual_read_drains_and_never_counts(
    rate_world, db_session, test_engine, monkeypatch
):
    w = rate_world
    opened, release = threading.Event(), threading.Event()
    original = reference_storage.content_file

    @contextmanager
    def blocked(*args, **kwargs):
        with original(*args, **kwargs) as result:
            opened.set()
            assert release.wait(10)
            yield result

    monkeypatch.setattr(reference_storage, "content_file", blocked)
    pending = asyncio.create_task(scan(db_session, w))
    try:
        assert await asyncio.to_thread(opened.wait, 10)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await totals(test_engine, w[5].id) == (0, 0)
    assert (w[0] / w[7].relative_path).read_bytes() == w[1]


async def test_rate_write_failure_preserves_successful_snapshot_and_caller_state(
    rate_world, db_session, test_engine, monkeypatch
):
    w = rate_world
    monkeypatch.setattr(
        rates, "_persist", AsyncMock(side_effect=RuntimeError("private"))
    )
    await scan(db_session, w)
    assert await totals(test_engine, w[5].id) == (0, 0)
    assert w[7].verified_content["sha256"] == hashlib.sha256(w[1]).hexdigest()

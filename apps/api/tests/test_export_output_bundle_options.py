import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading
import uuid

from billiard.exceptions import SoftTimeLimitExceeded
import pytest
from sqlalchemy import func, select

from app.db.models.export_output import ExportOutput
from app.services import export_delivery_data as data
from app.services import export_media_bundles as bundles
from app.services import export_outputs as outputs
from app.services import export_storage
from tests.test_export_media_bundle_images import allocate as allocate_image
from tests.test_export_media_bundles import allocated as allocate_lidar
from tests.test_video_export_media_domain import allocate as allocate_video


async def bundle(db, *, schema=1, leased=True):
    if schema == 4:
        _, owner, _, _ = await allocate_video(db, tasks=0)
    elif schema == 3:
        _, owner, _, _ = await allocate_image(db, managed=0, references=0)
    else:
        _, owner, _, _ = await allocate_lidar(db, tasks=1, targets=("pointmask",))
    if leased:
        owner = await bundles.claim(db, owner.id)
    await db.commit()
    return owner


@pytest.fixture
async def creation(db_session, monkeypatch, tmp_path):
    clock = [await db_session.scalar(select(func.clock_timestamp()))]
    monkeypatch.setattr(outputs, "now", lambda: clock[0])
    allocated_at = clock[0] - timedelta(minutes=20)
    monkeypatch.setattr(bundles, "now", lambda: allocated_at)
    path = tmp_path / "candidate.zip"
    path.write_bytes(b"exact local zip bytes")
    args = dict(
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media="image",
        format="pointmask",
        file_count=1,
        path=str(path),
        expected_size=path.stat().st_size,
    )
    return db_session, args, clock


def linked(args, owner, **changes):
    return {
        **args,
        "project_id": owner.project_id,
        "job_id": owner.job_id,
        "media_bundle_id": owner.id,
        **changes,
    }


async def test_old_defaults_still_allocate_distinct_seven_day_candidates(creation):
    db, args, clock = creation
    first = await outputs.create(db, **args)
    await db.rollback()
    second = await outputs.create(db, **args)
    await db.rollback()
    assert first.id != second.id
    for receipt in (first, second):
        assert receipt.created_at == clock[0]
        assert receipt.expires_at == clock[0] + timedelta(days=7)
        assert receipt.media_bundle_id is None
        saved = await data.zip_receipt(db, receipt.id)
        assert saved.state == "pending" and outputs.same(saved, receipt)


async def test_preallocated_id_and_shorter_deadline_are_saved_exactly(creation):
    db, args, clock = creation
    identifier = uuid.uuid4()
    created = clock[0] - timedelta(hours=1)
    expires = clock[0] + timedelta(days=1)
    receipt = await outputs.create(
        db,
        **args,
        output_id=identifier,
        created_at=created,
        expires_at=expires,
    )
    assert (receipt.id, receipt.created_at, receipt.expires_at) == (
        identifier,
        created,
        expires,
    )
    assert receipt.storage_ref["object_key"].endswith(f"/{identifier}.zip")
    assert outputs.same(await data.zip_receipt(db, identifier), receipt)


async def test_bundle_supplies_its_preallocated_id_and_common_deadline(creation):
    db, args, _ = creation
    owner = await bundle(db)
    receipt = await outputs.create(db, **linked(args, owner))
    assert receipt.id == owner.zip_output_id and receipt.media_bundle_id == owner.id
    assert (
        receipt.created_at == owner.created_at
        and receipt.expires_at == owner.expires_at
    )
    await db.rollback()
    saved = await data.zip_receipt(db, owner.zip_output_id)
    assert saved.state == "pending" and outputs.same(saved, receipt)


async def test_explicit_matching_bundle_options_are_accepted(creation):
    db, args, _ = creation
    owner = await bundle(db)
    receipt = await outputs.create(
        db,
        **linked(
            args,
            owner,
            output_id=owner.zip_output_id,
            created_at=owner.created_at,
            expires_at=owner.expires_at,
        ),
    )
    assert receipt.media_bundle_id == owner.id


@pytest.mark.parametrize("schema,media", [(1, "image"), (3, "image"), (4, "video")])
async def test_bundle_schema_selects_its_exact_zip_media(creation, schema, media):
    db, args, _ = creation
    owner = await bundle(db, schema=schema)
    receipt = await outputs.create(db, **linked(args, owner, media=media))
    assert receipt.media == media and receipt.media_bundle_id == owner.id
    assert receipt.id == owner.zip_output_id
    assert receipt.expires_at == owner.expires_at
    assert receipt.storage_ref["object_key"].startswith(media + "/")


@pytest.mark.parametrize("schema,media", [(1, "video"), (3, "video"), (4, "image")])
async def test_bundle_schema_rejects_cross_media_pair(creation, schema, media):
    db, args, _ = creation
    owner = await bundle(db, schema=schema)
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await outputs.create(db, **linked(args, owner, media=media))
    assert caught.value.code == "export_output_changed"
    assert await data.zip_receipt(db, owner.zip_output_id) is None


@pytest.mark.parametrize(
    "changed", ["id", "project", "job", "created", "expires", "media"]
)
async def test_bundle_identity_and_common_times_cannot_be_redefined(creation, changed):
    db, args, _ = creation
    owner = await bundle(db)
    values = linked(args, owner)
    if changed == "id":
        values["output_id"] = uuid.uuid4()
    elif changed == "project":
        values["project_id"] = uuid.uuid4()
    elif changed == "job":
        values["job_id"] = uuid.uuid4()
    elif changed == "created":
        values["created_at"] = owner.created_at + timedelta(seconds=1)
    elif changed == "expires":
        values["expires_at"] = owner.expires_at - timedelta(seconds=1)
    else:
        values["media"] = "video"
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await outputs.create(db, **values)
    assert caught.value.code == "export_output_changed"
    identifier = values.get("output_id", owner.zip_output_id)
    assert await data.zip_receipt(db, identifier) is None


@pytest.mark.parametrize(
    "changed",
    [
        "absent",
        "foreign",
        "ready",
        "retiring",
        "no_lease",
        "lease_only_in_projection",
        "expired_lease",
        "published",
    ],
)
async def test_bundle_must_be_the_current_building_leased_owner(
    creation, monkeypatch, changed
):
    db, args, clock = creation
    leased = changed not in {"no_lease", "lease_only_in_projection"}
    owner = await bundle(db, leased=leased)
    values = linked(args, owner)
    if changed == "absent":
        values["media_bundle_id"] = uuid.uuid4()
    elif changed == "expired_lease":
        clock[0] = owner.lease_expires_at
    elif changed != "no_lease":
        if changed == "foreign":
            reported = replace(owner, id=uuid.uuid4())
        elif changed == "published":
            reported = replace(owner, published_at=clock[0])
        elif changed == "lease_only_in_projection":
            reported = replace(
                owner,
                lease_token=uuid.uuid4(),
                lease_expires_at=clock[0] + timedelta(minutes=10),
            )
        else:
            reported = replace(owner, state=changed)
        monkeypatch.setattr(data, "media_fence", AsyncMock(return_value=reported))
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await outputs.create(db, **values)
    assert caught.value.code == (
        "export_storage_expired"
        if changed == "expired_lease"
        else "export_output_changed"
    )
    assert await data.zip_receipt(db, owner.zip_output_id) is None


@pytest.mark.parametrize(
    "changed", ["naive", "future", "expired", "over_seven_days", "equal"]
)
async def test_custom_delivery_window_is_bounded_and_timezone_aware(
    creation, monkeypatch, changed
):
    db, args, clock = creation
    now = clock[0]
    options = {}
    if changed == "naive":
        options["created_at"] = now.replace(tzinfo=None)
    elif changed == "future":
        options["created_at"] = now + timedelta(seconds=1)
    elif changed == "expired":
        options.update(
            created_at=now - timedelta(days=8), expires_at=now - timedelta(days=1)
        )
    elif changed == "over_seven_days":
        options["expires_at"] = now + timedelta(days=8)
    else:
        options["expires_at"] = now
    allocate = AsyncMock(wraps=export_storage.allocate)
    monkeypatch.setattr(export_storage, "allocate", allocate)
    identifier = uuid.uuid4()
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await outputs.create(db, **args, output_id=identifier, **options)
    assert caught.value.code == (
        "export_storage_expired"
        if changed == "expired"
        else "export_storage_invalid_receipt"
    )
    allocate.assert_not_awaited()
    assert await data.zip_receipt(db, identifier) is None


@pytest.mark.parametrize("delay", ["saved_owner", "reservation"])
async def test_lease_expiry_during_creation_prevents_commit(
    creation, monkeypatch, delay
):
    db, args, clock = creation
    owner = await bundle(db)

    def expire():
        clock[0] = owner.lease_expires_at

    if delay == "reservation":
        reserve = data.reserve_zip

        async def reserve_then_expire(db, row):
            accepted = await reserve(db, row)
            assert accepted
            expire()
            return accepted

        monkeypatch.setattr(data, "reserve_zip", reserve_then_expire)
    else:
        resolve = export_storage.resolve_ref

        def saved_owner(*args, **kwargs):
            result = resolve(*args, **kwargs)
            expire()
            return result

        monkeypatch.setattr(export_storage, "resolve_ref", saved_owner)
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await outputs.create(db, **linked(args, owner))
    assert caught.value.code == "export_storage_expired"
    if delay == "saved_owner":
        assert await data.zip_receipt(db, owner.zip_output_id) is None
    await db.rollback()
    assert await data.zip_receipt(db, owner.zip_output_id) is None


async def test_snapshot_compares_real_bundle_identity_and_preserves_old_simple_fakes(
    creation,
):
    db, args, _ = creation
    receipt = await outputs.create(db, **args)
    persisted = await data.zip_receipt(db, receipt.id)
    assert outputs.same(persisted, receipt)
    row = ExportOutput(**persisted._mapping)
    old_fake = SimpleNamespace(
        **{
            name: getattr(receipt, name)
            for name in outputs._FACTS
            if name != "media_bundle_id"
        }
    )
    assert outputs.same(row, old_fake)
    row.media_bundle_id = uuid.uuid4()
    assert not outputs.same(row, old_fake) and not outputs.same(row, receipt)
    saved = outputs.snapshot(row)
    assert saved.media_bundle_id == row.media_bundle_id
    assert outputs.same(row, saved)
    assert not outputs.same(row, replace(saved, media_bundle_id=uuid.uuid4()))
    saved.storage_ref["bucket"] = "foreign"
    assert outputs.same(row, saved)


async def test_hash_finishes_before_any_bundle_row_lock(creation, monkeypatch):
    db, args, _ = creation
    owner = await bundle(db)
    started, released, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = export_storage.file_receipt

    def hashing(path):
        started.set()
        assert released.wait(5)
        result = original(path)
        finished.set()
        return result

    monkeypatch.setattr(export_storage, "file_receipt", hashing)
    fence = data.media_fence

    async def fence_after_hash(db, identifier, *, mode):
        assert finished.is_set()
        return await fence(db, identifier, mode=mode)

    lock = AsyncMock(side_effect=fence_after_hash)
    monkeypatch.setattr(data, "media_fence", lock)
    pending = asyncio.create_task(outputs.create(db, **linked(args, owner)))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        lock.assert_not_awaited()
        assert not pending.done()
    finally:
        released.set()
        receipt = await pending
    lock.assert_awaited_with(db, owner.id, mode="update_nowait")
    assert finished.is_set() and receipt.media_bundle_id == owner.id


@pytest.mark.parametrize(
    "first,second",
    [
        (asyncio.CancelledError, SoftTimeLimitExceeded),
        (SoftTimeLimitExceeded, KeyboardInterrupt),
    ],
)
async def test_hash_wait_drains_through_all_baseexceptions(monkeypatch, first, second):
    started, released, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = asyncio.shield
    calls = []

    def hashing():
        started.set()
        assert released.wait(5)
        finished.set()
        return "finished"

    async def interrupted(kind):
        raise kind()

    def shield(future):
        calls.append(future)
        if first is SoftTimeLimitExceeded and len(calls) == 1:
            return interrupted(first)
        if len(calls) == 2:
            return interrupted(second)
        return original(future)

    monkeypatch.setattr(asyncio, "shield", shield)

    async def owned():
        try:
            return await outputs._settled_thread(hashing)
        except BaseException as exc:
            return exc

    pending = asyncio.create_task(owned())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        if first is asyncio.CancelledError:
            pending.cancel()
        for _ in range(30):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.001)
        assert len(calls) >= 3 and not pending.done() and not finished.is_set()
    finally:
        released.set()
    result = await pending
    assert isinstance(result, first) and finished.is_set()


async def test_late_uncertain_failure_is_preserved_on_the_original_cancellation():
    started, released = threading.Event(), threading.Event()

    def hashing():
        started.set()
        assert released.wait(5)
        exc = OSError("uncertain physical close")
        exc.storage_operation_unsettled = True
        raise exc

    async def owned():
        try:
            await outputs._settled_thread(hashing)
        except BaseException as exc:
            return exc

    pending = asyncio.create_task(owned())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        released.set()
    result = await pending
    assert (
        isinstance(result, asyncio.CancelledError)
        and result.storage_operation_unsettled
    )


async def test_hash_thread_preserves_context_variables():
    scope = ContextVar("export-create-scope", default=None)
    token = scope.set("owned-attempt")
    try:
        assert await outputs._settled_thread(scope.get) == "owned-attempt"
    finally:
        scope.reset(token)


async def test_executor_submission_uncertainty_is_not_reported_settled(monkeypatch):
    def uncertain(*args):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", uncertain)
    with pytest.raises(SoftTimeLimitExceeded) as caught:
        await outputs._settled_thread(lambda: None)
    assert caught.value.storage_operation_unsettled


async def test_bundle_candidate_uses_saved_owner_after_current_defaults_change(
    creation, monkeypatch
):
    db, args, _ = creation
    owner = await bundle(db)
    expected = dict(owner.storage_ref)
    prefix = expected.pop("key_prefix").removesuffix(
        f"lidar-export-media/objects/{owner.id}/"
    )
    expected["object_key"] = (
        f"{prefix}image/{owner.project_id}/{owner.job_id}/{owner.zip_output_id}.zip"
    )
    assert expected["bucket"] != "same-key-trap"

    async def forbidden(*args, **kwargs):
        pytest.fail("a preallocated bundle must not allocate against today's default")

    monkeypatch.setattr(export_storage, "allocate", forbidden)
    monkeypatch.setattr(
        export_storage.storage_service, "export_bucket", "same-key-trap"
    )
    candidate = await outputs.create(db, **linked(args, owner))
    assert candidate.storage_ref == expected
    assert (await data.zip_receipt(db, candidate.id)).storage_ref == expected

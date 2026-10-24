"""Pure creation-option and real-thread draining regressions, without a database."""

import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import uuid

from billiard.exceptions import SoftTimeLimitExceeded
import pytest

from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services import export_outputs as outputs
from app.services import export_storage

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
ENDPOINT = "http://saved-export.test:9000"


class CreationDB:
    def __init__(self, bundle=None):
        self.bundle = bundle
        self.events, self.added = [], []
        self.after_hash = None
        self.on_flush = None

    async def scalar(self, statement):
        assert statement._for_update_arg.nowait
        if self.after_hash is not None:
            assert self.after_hash.is_set(), (
                "business lock preceded physical hash completion"
            )
        self.events.append("lock")
        return self.bundle

    def add(self, row):
        self.added.append(row)
        self.events.append("add")

    async def flush(self):
        self.events.append("flush")
        if self.on_flush:
            self.on_flush()

    async def commit(self):
        self.events.append("commit")


def bundle(**changes):
    created = NOW - timedelta(minutes=20)
    return ExportMediaBundle(
        **{
            "id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
            "job_id": uuid.uuid4(),
            "zip_output_id": uuid.uuid4(),
            "state": "building",
            "created_at": created,
            "expires_at": created + timedelta(hours=168),
            "lease_token": uuid.uuid4(),
            "lease_expires_at": NOW + timedelta(seconds=3600),
            **changes,
        }
    )


def ref(row):
    return {
        "schema": 1,
        "purpose": "export",
        "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": ENDPOINT,
        "bucket": "saved-export-zip",
        "object_key": export_storage.key_for(
            row.media, row.project_id, row.job_id, row.id
        ),
        "storage_identity": export_storage.managed_storage_identity(
            ENDPOINT, "saved-export-zip"
        ),
    }


@pytest.fixture
def creation(monkeypatch, tmp_path):
    clock = [NOW]
    monkeypatch.setattr(outputs, "now", lambda: clock[0])
    monkeypatch.setattr(export_storage, "_runtime_endpoint", lambda: ENDPOINT)

    async def allocate(db, row):
        db.events.append("allocate")
        return ref(row)

    monkeypatch.setattr(export_storage, "allocate", allocate)
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
    return args, clock


def linked(args, owner, **changes):
    return {
        **args,
        "project_id": owner.project_id,
        "job_id": owner.job_id,
        "media_bundle_id": owner.id,
        **changes,
    }


async def test_old_defaults_still_allocate_distinct_seven_day_candidates(creation):
    args, _ = creation
    db = CreationDB()
    first = await outputs.create(db, **args)
    second = await outputs.create(db, **args)
    assert first.id != second.id
    assert first.created_at == NOW and first.expires_at == NOW + timedelta(days=7)
    assert first.media_bundle_id is None
    assert "lock" not in db.events and db.events.count("commit") == 2


async def test_preallocated_id_and_shorter_deadline_are_saved_exactly(creation):
    args, _ = creation
    identifier = uuid.uuid4()
    created = NOW - timedelta(hours=1)
    expires = NOW + timedelta(days=1)
    receipt = await outputs.create(
        CreationDB(),
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


async def test_bundle_supplies_its_preallocated_id_and_common_deadline(creation):
    args, _ = creation
    owner = bundle()
    db = CreationDB(owner)
    receipt = await outputs.create(db, **linked(args, owner))
    assert receipt.id == owner.zip_output_id and receipt.media_bundle_id == owner.id
    assert (
        receipt.created_at == owner.created_at
        and receipt.expires_at == owner.expires_at
    )
    assert db.events == ["lock", "allocate", "add", "flush", "commit"]


async def test_explicit_matching_bundle_options_are_accepted(creation):
    args, _ = creation
    owner = bundle()
    receipt = await outputs.create(
        CreationDB(owner),
        **linked(
            args,
            owner,
            output_id=owner.zip_output_id,
            created_at=owner.created_at,
            expires_at=owner.expires_at,
        ),
    )
    assert receipt.media_bundle_id == owner.id


@pytest.mark.parametrize(
    "changed", ["id", "project", "job", "created", "expires", "media"]
)
async def test_bundle_identity_and_common_times_cannot_be_redefined(creation, changed):
    args, _ = creation
    owner = bundle()
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
    db = CreationDB(owner)
    with pytest.raises(export_storage.ExportStorageError):
        await outputs.create(db, **values)
    assert db.events == ["lock"] and not db.added


@pytest.mark.parametrize(
    "changed",
    [
        "absent",
        "foreign",
        "ready",
        "retiring",
        "no_lease",
        "expired_lease",
        "published",
    ],
)
async def test_bundle_must_be_the_current_building_leased_owner(creation, changed):
    args, _ = creation
    owner = bundle()
    values = linked(args, owner)
    if changed == "foreign":
        owner.id = uuid.uuid4()
    elif changed in {"ready", "retiring"}:
        owner.state = changed
    elif changed == "no_lease":
        owner.lease_token = owner.lease_expires_at = None
    elif changed == "expired_lease":
        owner.lease_expires_at = NOW
    elif changed == "published":
        owner.published_at = NOW - timedelta(minutes=1)
    db = CreationDB(None if changed == "absent" else owner)
    with pytest.raises(export_storage.ExportStorageError):
        await outputs.create(db, **values)
    assert db.events == ["lock"] and not db.added


@pytest.mark.parametrize(
    "changed", ["naive", "future", "expired", "over_seven_days", "equal"]
)
async def test_custom_delivery_window_is_bounded_and_timezone_aware(creation, changed):
    args, _ = creation
    options = {}
    if changed == "naive":
        options["created_at"] = NOW.replace(tzinfo=None)
    elif changed == "future":
        options["created_at"] = NOW + timedelta(seconds=1)
    elif changed == "expired":
        options.update(
            created_at=NOW - timedelta(days=8), expires_at=NOW - timedelta(days=1)
        )
    elif changed == "over_seven_days":
        options["expires_at"] = NOW + timedelta(days=8)
    else:
        options["expires_at"] = NOW
    db = CreationDB()
    with pytest.raises(export_storage.ExportStorageError):
        await outputs.create(db, **args, **options)
    assert not db.events


@pytest.mark.parametrize("delay", ["allocate", "flush"])
async def test_time_is_rechecked_after_allocation_and_flush(
    creation, monkeypatch, delay
):
    args, clock = creation
    owner = bundle()
    db = CreationDB(owner)

    def expire():
        clock[0] = owner.lease_expires_at + timedelta(seconds=1)

    if delay == "flush":
        db.on_flush = expire
    else:

        async def allocate(db, row):
            db.events.append("allocate")
            expire()
            return ref(row)

        monkeypatch.setattr(export_storage, "allocate", allocate)
    with pytest.raises(export_storage.ExportStorageError) as caught:
        await outputs.create(db, **linked(args, owner))
    assert caught.value.code == "export_storage_expired" and "commit" not in db.events


async def test_snapshot_compares_real_bundle_identity_and_preserves_old_simple_fakes(
    creation,
):
    args, _ = creation
    db = CreationDB()
    receipt = await outputs.create(db, **args)
    row = db.added[0]
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
    assert isinstance(row, ExportOutput)


async def test_hash_finishes_before_any_bundle_row_lock(creation, monkeypatch):
    args, _ = creation
    owner = bundle()
    db = CreationDB(owner)
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
    db.after_hash = finished
    pending = asyncio.create_task(outputs.create(db, **linked(args, owner)))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        assert not db.events and not pending.done()
    finally:
        released.set()
    receipt = await pending
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

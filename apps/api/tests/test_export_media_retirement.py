"""Pure retirement fences and isolated-database recovery without live providers."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.db.models.export_output import ExportOutput
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation
from app.services import export_media_retirement as retirement
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services import export_outputs, export_storage
from app.services.content_snapshot import ContentError
from tests import test_export_media_bundle_models as model_tests
from tests.test_export_media_storage import Client

owned_db = model_tests.owned_db


def owner(**changes):
    values = model_tests._values()
    values.update(
        lease_token=None,
        lease_expires_at=None,
        published_at=None,
        retired_at=None,
        last_attempted_at=None,
    )
    values.update(changes)
    values["storage_ref"] = physical.make_ref(values["id"], bucket="saved-media")
    return SimpleNamespace(**values)


class PolicyDB:
    def __init__(self, job=None, replacement=None):
        self.job, self.replacement = job, replacement
        self.statements, self.commits = [], []

    async def get(self, *args, **kwargs):
        return self.job

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: self.replacement)

    async def commit(self):
        self.commits.append(True)


@pytest.mark.parametrize("state", ["building", "ready", "retiring"])
async def test_published_latch_survives_missing_job_until_exact_expiry(state):
    row = owner(state=state, published_at=datetime.now(timezone.utc))
    assert not await retirement.attempt_ended(PolicyDB(), row)
    assert await retirement.attempt_ended(PolicyDB(), row, current=row.expires_at)


@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "failed", "cancelled", None]
)
async def test_unpublished_attempt_end_is_explicit_and_never_a_job_age_timeout(status):
    row = owner()
    job = (
        None
        if status is None
        else SimpleNamespace(
            id=row.job_id,
            project_id=row.project_id,
            kind="export",
            status=status,
            payload={},
        )
    )
    expected = status not in {"pending", "running"}
    assert await retirement.attempt_ended(PolicyDB(job), row) is expected
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await retirement.attempt_ended(PolicyDB(job), row)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign_project",
        "foreign_job",
        "digest",
        "schema",
        "extra",
        "missing",
        "same",
        "input_schema",
        "proof_kind",
        "both",
    ],
)
@pytest.mark.parametrize("schema", [1, 2, 3])
async def test_replacement_requires_an_existing_exact_new_bundle_proof(fault, schema):
    row = owner()
    replacement = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=row.project_id,
        job_id=row.job_id,
        input_digest="b" * 64,
        input_schema=schema,
    )
    proof = {
        "schema": schema,
        "bundle_id": str(replacement.id),
        "input_digest": replacement.input_digest,
    }
    if fault == "foreign_project":
        replacement.project_id = uuid.uuid4()
    elif fault == "foreign_job":
        replacement.job_id = uuid.uuid4()
    elif fault == "digest":
        proof["input_digest"] = "c" * 64
    elif fault == "schema":
        proof["schema"] = True
    elif fault == "extra":
        proof["lease"] = "renewed"
    elif fault == "same":
        proof["bundle_id"] = str(row.id)
    elif fault == "input_schema":
        replacement.input_schema = 2 if schema == 3 else 3
    key = "image_media" if schema == 3 else "lidar_media"
    if fault == "proof_kind":
        key = "lidar_media" if key == "image_media" else "image_media"
    payload = {key: proof}
    if fault == "both":
        payload["lidar_media" if key == "image_media" else "image_media"] = proof
    job = SimpleNamespace(
        kind="export",
        status="running",
        project_id=row.project_id,
        payload=payload,
    )
    db = PolicyDB(job, None if fault == "missing" else replacement)
    assert await retirement.attempt_ended(db, row) is (fault is None)


async def test_candidate_excludes_all_operation_ages_and_locations_before_limit():
    query = (
        select(Bundle.id)
        .where(retirement.candidate_predicate(datetime.now(timezone.utc)))
        .limit(3)
        .with_for_update(skip_locked=True)
    )
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "storage_operations.purpose IN" in sql and "NOT (EXISTS" in sql
    assert "storage_operations.started_at" not in sql
    assert "storage_operations.location_id" not in sql
    assert "SKIP LOCKED" in sql and "LIMIT" in sql
    assert {"export_media_", "export_write_", "export_read_"} <= {
        value for value in compiled.params.values() if isinstance(value, str)
    }


@pytest.mark.parametrize("unknown_close", [False, True])
async def test_delete_lifetime_keeps_permit_through_constructor_and_real_close(
    monkeypatch, unknown_close
):
    row = owner(state="retiring", retired_at=datetime.now(timezone.utc))
    receipt = retirement.snapshot(row)
    client, active, events = Client(), [False], []
    for key in row.inventory["objects"]:
        client.add(key)
    if unknown_close:
        client.close_failure = OSError("unknown close")
    permit = SimpleNamespace(
        id=uuid.uuid4(), location_id=physical.LEGACY_LOCATION_ID, revision=1
    )

    async def acquire(*args, **kwargs):
        active[0] = True
        events.append("acquire")
        return permit

    async def guard(*args, **kwargs):
        assert active[0]
        events.append("guard")

    async def settle(value):
        assert value is permit and client.close_count == 1
        active[0] = False
        events.append("settle")

    def construct():
        assert active[0]
        events.append("construct")
        return client

    monkeypatch.setattr(transfer, "_acquire", acquire)
    monkeypatch.setattr(transfer, "_settle", settle)
    monkeypatch.setattr(retirement, "_guard", guard)
    monkeypatch.setattr(physical, "create_client", construct)
    monkeypatch.setattr(retirement.removal, "recorder", lambda *args: None)
    if unknown_close:
        with pytest.raises(physical.ExportMediaStorageError) as error:
            await retirement._physical_delete(None, receipt)
        assert error.value.storage_operation_unsettled
        assert active[0] and events == ["acquire", "guard", "construct"]
    else:
        await retirement._physical_delete(None, receipt)
        assert not active[0] and events[-2:] == ["guard", "settle"]
    assert all(params["Bucket"] == "saved-media" for _, params in client.calls)


async def test_cancelled_delete_drains_before_closing_client_and_permit(monkeypatch):
    row = owner(state="retiring", retired_at=datetime.now(timezone.utc))
    client, entered, release = Client(), threading.Event(), threading.Event()
    for key in row.inventory["objects"]:
        client.add(key)
    original_delete = client.delete_object

    def blocking(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_delete(**kwargs)

    client.delete_object = blocking
    permit = SimpleNamespace(id=uuid.uuid4())
    settled = AsyncMock()
    monkeypatch.setattr(transfer, "_acquire", AsyncMock(return_value=permit))
    monkeypatch.setattr(transfer, "_settle", settled)
    monkeypatch.setattr(retirement, "_guard", AsyncMock())
    monkeypatch.setattr(physical, "create_client", lambda: client)
    monkeypatch.setattr(retirement.removal, "recorder", lambda *args: None)
    pending = asyncio.create_task(
        retirement._physical_delete(None, retirement.snapshot(row))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and client.close_count == 0
        settled.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert client.close_count == 1
    settled.assert_awaited_once_with(permit)


async def test_failure_keeps_committed_retiring_inventory_and_retry_leaves_tombstone(
    monkeypatch,
):
    row = owner()
    db = PolicyDB()
    original = row.inventory.copy()
    monkeypatch.setattr(retirement, "lock_owner", AsyncMock(return_value=row))
    monkeypatch.setattr(retirement, "has_operations", AsyncMock(return_value=False))

    async def failure(db, receipt):
        assert db.commits and row.state == "retiring" and row.retired_at is not None
        raise ContentError("known_delete_failed")

    monkeypatch.setattr(retirement, "_physical_delete", failure)
    with pytest.raises(ContentError, match="known_delete_failed"):
        await retirement._cleanup_one(db, row.id)
    assert row.state == "retiring" and row.inventory == original
    retired = row.retired_at
    monkeypatch.setattr(retirement, "_physical_delete", AsyncMock())
    assert await retirement._cleanup_one(db, row.id)
    assert (
        row.state == "deleted"
        and row.retired_at == retired
        and row.inventory == original
    )


@pytest.mark.parametrize(
    "protection", [None, "legacy", "published", "operation", "running"]
)
async def test_pending_zip_has_its_own_retirement_and_preserves_legacy_pending(
    monkeypatch, protection
):
    bundle = owner()
    output = SimpleNamespace(
        id=bundle.zip_output_id,
        project_id=bundle.project_id,
        job_id=bundle.job_id,
        media_bundle_id=bundle.id,
        media="image",
        format="pointmask",
        file_count=1,
        sha256="a" * 64,
        size_bytes=11,
        state="pending",
        created_at=bundle.created_at,
        expires_at=bundle.expires_at,
        retired_at=None,
        last_attempted_at=None,
        storage_ref={
            **{
                key: value
                for key, value in bundle.storage_ref.items()
                if key != "key_prefix"
            },
            "object_key": export_storage.key_for(
                "image", bundle.project_id, bundle.job_id, bundle.zip_output_id
            ),
        },
    )
    if protection == "published":
        bundle.published_at = datetime.now(timezone.utc)
    if protection == "legacy":
        output.media_bundle_id = None
    job = (
        SimpleNamespace(
            kind="export", status="running", project_id=bundle.project_id, payload={}
        )
        if protection == "running"
        else None
    )
    db = PolicyDB(job)
    db.delete = AsyncMock()
    monkeypatch.setattr(
        export_outputs,
        "_cleanup_locked",
        AsyncMock(return_value=(output, None if protection == "legacy" else bundle)),
    )
    monkeypatch.setattr(export_outputs, "_has_read", AsyncMock(return_value=False))
    monkeypatch.setattr(export_outputs, "_has_index", AsyncMock(return_value=False))
    monkeypatch.setattr(
        retirement, "has_operations", AsyncMock(return_value=protection == "operation")
    )

    async def delete_zip(db, expected_owner, receipt):
        assert output.state == "retired" and db.commits
        assert expected_owner is bundle and receipt.id == output.id
        assert bundle.state == "building" and bundle.inventory

    remove = AsyncMock(side_effect=delete_zip)
    monkeypatch.setattr(retirement, "delete_zip", remove)
    result = await export_outputs._cleanup_one(db, output.id, {"removed": 0})
    assert result is (protection is None)
    if protection is None:
        remove.assert_awaited_once()
        db.delete.assert_awaited_once_with(output)
        assert bundle.state == "building" and bundle.inventory
    else:
        assert output.state == "pending"
        remove.assert_not_awaited()
        db.delete.assert_not_awaited()


@pytest.fixture
async def gc_db(owned_db):
    db = owned_db
    for ddl in (
        "CREATE TABLE storage_maintenance_gates (location_id uuid PRIMARY KEY, state text DEFAULT 'active', revision integer DEFAULT 1, changed_at timestamptz, changed_by uuid)",
        "CREATE TABLE storage_operations (id uuid PRIMARY KEY, location_id uuid, revision integer, purpose text, node_id text, started_at timestamptz DEFAULT now())",
        "ALTER TABLE export_artifacts ADD COLUMN expires_at timestamptz",
    ):
        await db.execute(text(ddl))
    connection = await db.connection()
    # Retirement reads full AsyncJob rows; mirror the production entity rather
    # than a hand-narrowed copy that silently misses later model columns.
    await connection.run_sync(AsyncJob.__table__.create)
    await connection.run_sync(StorageUsageMutation.__table__.create)
    await db.commit()
    return db


async def database_owner(db, **changes):
    values = model_tests._values(**changes)
    values["storage_ref"] = physical.make_ref(values["id"], bucket="saved-media")
    row = Bundle(**values)
    db.add(row)
    await db.flush()
    return row


def objects(monkeypatch, *rows):
    client = Client()
    for row in rows:
        for key in row.inventory["objects"]:
            client.add(key)
    monkeypatch.setattr(physical, "create_client", lambda: client)
    return client


async def test_database_unknown_operations_do_not_starve_next_candidate_before_limit(
    gc_db, monkeypatch
):
    db = gc_db
    protected = [await database_owner(db) for _ in range(3)]
    for index, row in enumerate(protected[:2]):
        db.add(
            StorageOperation(
                id=uuid.uuid4(),
                location_id=uuid.uuid4(),
                revision=1,
                purpose=retirement.purposes(row)[index],
                node_id="unknown",
                started_at=datetime.now(timezone.utc) - timedelta(days=100),
            )
        )
    await db.commit()
    client = objects(monkeypatch, *protected)
    counts = await retirement.cleanup(db, limit=1)
    assert counts == {"deleted": 1, "retained": 0, "failed": 0}
    assert (await db.get(Bundle, protected[2].id)).state == "deleted"
    for row in protected[:2]:
        assert (await db.get(Bundle, row.id)).state == "building"
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 2
    assert {
        params["Key"] for action, params in client.calls if action == "delete"
    } <= set(protected[2].inventory["objects"])


async def test_database_locked_candidate_is_skipped_without_waiting(gc_db, monkeypatch):
    db = gc_db
    first, second = await database_owner(db), await database_owner(db)
    await db.commit()
    objects(monkeypatch, first, second)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as blocker:
        await blocker.scalar(
            select(Bundle).where(Bundle.id == first.id).with_for_update()
        )
        try:
            assert (await retirement.cleanup(db, limit=1))["deleted"] == 1
            assert (await db.get(Bundle, second.id)).state == "deleted"
            assert (await db.get(Bundle, first.id)).state == "building"
        finally:
            await blocker.rollback()


async def test_database_failed_partial_delete_retries_without_dropping_inventory(
    gc_db, monkeypatch
):
    db = gc_db
    values = model_tests._values()
    values["inventory"]["objects"][
        physical.prefix_for(values["id"]) + str(uuid.uuid4()) + ".bin"
    ] = {
        "sha256": "b" * 64,
        "size_bytes": 11,
    }
    values["object_count"], values["size_bytes"] = 2, 22
    row = await database_owner(db, **values)
    await db.commit()
    client = objects(monkeypatch, row)
    original = client.delete_object

    def fail_after_delete(**kwargs):
        original(**kwargs)
        raise OSError("partial delete failed")

    client.delete_object = fail_after_delete
    assert (await retirement.cleanup(db))["failed"] == 1
    await db.refresh(row)
    assert row.state == "retiring" and row.inventory
    assert sum(bool(versions) for versions in client.rows.values()) == 1
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 0
    client.delete_object = original
    assert (await retirement.cleanup(db))["deleted"] == 1
    await db.refresh(row)
    assert row.state == "deleted" and row.inventory


async def test_database_unknown_close_keeps_operation_and_retiring_owner(
    gc_db, monkeypatch
):
    db = gc_db
    row = await database_owner(db)
    await db.commit()
    client = objects(monkeypatch, row)
    client.close_failure = OSError("unknown client close")
    assert (await retirement.cleanup(db))["failed"] == 1
    await db.refresh(row)
    assert row.state == "retiring" and row.inventory
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 1
    client.close_failure = None
    assert await retirement.cleanup(db) == {"deleted": 0, "retained": 0, "failed": 0}


async def test_database_published_delivery_survives_job_and_cache_absence(
    gc_db, monkeypatch
):
    db = gc_db
    row = await database_owner(db)
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.commit()
    output = await model_tests._zip(db, row, created_at=row.created_at)
    row.state, row.published_at, output.state = (
        "ready",
        datetime.now(timezone.utc),
        "ready",
    )
    await db.commit()
    client = objects(monkeypatch, row)
    assert await retirement.cleanup(db) == {"deleted": 0, "retained": 0, "failed": 0}
    assert not client.calls and (await db.get(Bundle, row.id)).state == "ready"


async def test_database_pending_zip_and_media_retire_independently_legacy_pending_stays(
    gc_db, monkeypatch
):
    db = gc_db
    row = await database_owner(db)
    endpoint = physical._runtime_endpoint()
    zip_bucket = "saved-export-zip"
    zip_ref = {
        **{key: value for key, value in row.storage_ref.items() if key != "key_prefix"},
        "bucket": zip_bucket,
        "object_key": export_storage.key_for(
            "image", row.project_id, row.job_id, row.zip_output_id
        ),
        "endpoint_url": endpoint,
        "storage_identity": export_storage.managed_storage_identity(
            endpoint, zip_bucket
        ),
    }
    output = await model_tests._zip(
        db, row, created_at=row.created_at, storage_ref=zip_ref
    )
    legacy_created = datetime.now(timezone.utc) - timedelta(days=8)
    legacy = await model_tests._zip(
        db, created_at=legacy_created, expires_at=legacy_created + timedelta(days=7)
    )
    await db.commit()
    deleted = []
    monkeypatch.setattr(
        export_storage, "delete", lambda receipt, **kwargs: deleted.append(receipt.id)
    )
    client = objects(monkeypatch, row)
    assert (await export_outputs.cleanup(db))["outputs_deleted"] == 1
    assert deleted == [output.id]
    assert await db.get(ExportOutput, output.id) is None
    assert (await db.get(Bundle, row.id)).state == "building"
    assert await db.get(ExportOutput, legacy.id) is not None
    assert not client.calls
    assert (await retirement.cleanup(db))["deleted"] == 1
    assert (await db.get(Bundle, row.id)).state == "deleted"


async def test_database_zip_cleanup_observes_bundle_reads_at_any_location_before_limit(
    gc_db, monkeypatch
):
    db = gc_db
    rows = [await database_owner(db) for _ in range(4)]
    outputs = []
    for row in rows:
        ref = {
            key: value for key, value in row.storage_ref.items() if key != "key_prefix"
        }
        ref["object_key"] = export_storage.key_for(
            "image", row.project_id, row.job_id, row.zip_output_id
        )
        outputs.append(
            await model_tests._zip(db, row, created_at=row.created_at, storage_ref=ref)
        )
    for index, row in enumerate(rows[:3]):
        db.add(
            StorageOperation(
                id=uuid.uuid4(),
                location_id=uuid.uuid4(),
                revision=1,
                purpose=retirement.purposes(row)[index],
                node_id="unknown-close",
                started_at=datetime.now(timezone.utc) - timedelta(days=100),
            )
        )
    await db.commit()
    deleted = []
    monkeypatch.setattr(
        export_storage, "delete", lambda receipt, **kwargs: deleted.append(receipt.id)
    )
    result = await export_outputs.cleanup(db, limit=1)
    assert result["outputs_deleted"] == 1 and deleted == [outputs[3].id]
    for row in outputs[:3]:
        assert (await db.get(ExportOutput, row.id)).state == "pending"
        counters = {"removed": 0}
        assert not await export_outputs._cleanup_one(db, row.id, counters)
    assert await db.scalar(select(func.count()).select_from(StorageOperation)) == 3

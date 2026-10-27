"""Exact prediction input ownership over real isolated SQL and controlled SDK I/O."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from billiard.exceptions import SoftTimeLimitExceeded
from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.prediction_input import PredictionInputLease as Lease
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import prediction_inputs as owner, storage_access
from app.services.content_snapshot import ContentError, Snapshot
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_export_media_bundle_models import _migration


class Storage:
    def __init__(self, world):
        self.world, self.client = world, self
        self.meta = SimpleNamespace(
            endpoint_url=owner._endpoint(), events=SimpleNamespace(register=Mock())
        )
        self.import_bucket = settings.minio_import_bucket
        self.closed = False
        world.clients.append(self)

    def get_bucket_versioning(self, *, Bucket):
        self.world.versioning_reads.append(Bucket)
        return {
            "Status": self.world.versioning,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    @staticmethod
    def seed(world, bucket, key, raw, metadata, *, version=None):
        if version is None:
            version = str(uuid.uuid4()) if world.versioning == "Enabled" else "null"
        world.versions.setdefault((bucket, key), {})[version] = (
            raw,
            deepcopy(metadata),
        )
        world.current_versions[bucket, key] = version
        world.objects[bucket, key] = raw
        world.metadata[bucket, key] = deepcopy(metadata)
        return version

    def put_object(
        self, *, Bucket, Key, Body, ContentLength, IfNoneMatch, Metadata, **kwargs
    ):
        assert IfNoneMatch == "*"
        if self.world.started is not None:
            self.world.started.set()
            assert self.world.release.wait(5)
        if (Bucket, Key) in self.world.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        raw = Body.read()
        assert len(raw) == ContentLength
        version = self.seed(
            self.world,
            Bucket,
            Key,
            raw,
            Metadata,
            version=self.world.headers.get("VersionId")
            or ("null" if "VersionId" in self.world.headers else None),
        )
        # The PUT response belongs to this write, even if another version becomes
        # current before the caller receives the acknowledgement.
        written = self._head(Bucket, Key, version)
        self.world.puts.append((Bucket, Key))
        if self.world.after_put:
            self.world.after_put()
        return written

    def _value(self, bucket, key, version=None):
        current = self.world.current_versions.get((bucket, key), "null")
        version = current if version is None else version
        if version == current and (bucket, key) in self.world.objects:
            return (
                self.world.objects[bucket, key],
                self.world.metadata.get((bucket, key), {}),
                version,
            )
        if version in self.world.versions.get((bucket, key), {}):
            raw, metadata = self.world.versions[bucket, key][version]
            return raw, metadata, version
        raise ClientError(
            {
                "Error": {"Code": "NoSuchVersion"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )

    def _head(self, bucket, key, version=None):
        raw, metadata, version = self._value(bucket, key, version)
        return {
            "ContentLength": len(raw),
            "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
            "VersionId": version,
            "Metadata": dict(metadata),
            "ResponseMetadata": {"HTTPStatusCode": 200},
            **self.world.headers,
        }

    def head_object(self, *, Bucket, Key, VersionId=None):
        self.world.heads.append((Bucket, Key, VersionId))
        return self._head(Bucket, Key, VersionId)

    def get_object(self, *, Bucket, Key, IfMatch, **kwargs):
        version = kwargs["VersionId"]
        assert version not in {None, "null"}
        self.world.reads.append((Bucket, Key, version))
        facts = self._head(Bucket, Key, version)
        assert IfMatch == facts["ETag"]
        raw, _, _ = self._value(Bucket, Key, version)
        if self.world.corrupt_read:
            raw = bytes([raw[0] ^ 1]) + raw[1:]
        body = io.BytesIO(raw)
        self.world.bodies.append(body)
        return {**facts, "Body": body}

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Contents": [
                {"Key": key, "Size": len(raw), "ETag": self._head(bucket, key)["ETag"]}
                for (bucket, key), raw in self.world.objects.items()
                if bucket == Bucket and key.startswith(Prefix)
            ],
        }

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        rows = []
        identities = set(self.world.versions) | set(self.world.objects)
        for bucket, key in sorted(identities):
            if bucket != Bucket or not key.startswith(Prefix):
                continue
            versions = self.world.versions.get((bucket, key)) or {
                "null": (self.world.objects[bucket, key], {})
            }
            for version in versions:
                facts = self._head(bucket, key, version)
                rows.append(
                    {
                        "Key": key,
                        "Size": facts["ContentLength"],
                        "ETag": facts["ETag"],
                        "VersionId": version,
                        "IsLatest": (bucket, key) in self.world.objects
                        and self.world.current_versions.get((bucket, key), "null")
                        == version,
                    }
                )
        if self.world.extra_version and rows:
            rows.append({**rows[0], "VersionId": "unknown-version"})
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Versions": rows,
            "DeleteMarkers": [
                {key: value for key, value in marker.items() if key != "Bucket"}
                for marker in self.world.delete_markers
                if marker.get("Bucket", Bucket) == Bucket
                and marker["Key"].startswith(Prefix)
            ],
        }

    def delete_object(self, *, Bucket, Key, VersionId, IfMatch):
        # Model the observed MinIO behavior: IfMatch is deliberately ignored.
        # Exact immutable VersionId, rather than this header, fences deletion.
        self._value(Bucket, Key, VersionId)
        self.world.deleted.append((Bucket, Key))
        versions = self.world.versions.get((Bucket, Key), {})
        versions.pop(VersionId, None)
        if versions:
            current = next(reversed(versions))
            raw, metadata = versions[current]
            self.world.current_versions[Bucket, Key] = current
            self.world.objects[Bucket, Key] = raw
            self.world.metadata[Bucket, Key] = deepcopy(metadata)
        else:
            self.world.versions.pop((Bucket, Key), None)
            self.world.current_versions.pop((Bucket, Key), None)
            self.world.objects.pop((Bucket, Key), None)
            self.world.metadata.pop((Bucket, Key), None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def generate_download_url(
        self, key, *, expires_in, bucket, align, version_id, audience
    ):
        assert audience == "ml"
        assert not align and 0 < expires_in <= owner.URL_SECONDS
        assert version_id not in {None, "null"}
        self.world.expirations.append(expires_in)
        self.world.signed_versions.append(version_id)
        return (
            f"https://saved.test/{bucket}/{key}?private=receipt&versionId={version_id}"
        )

    def close(self):
        if self.world.fail_close:
            raise OSError("private close uncertainty")
        self.closed = True


@pytest.fixture
async def world(test_engine, monkeypatch):
    schema = "prediction_owner_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for table in (
            "storage_locations",
            "storage_bindings",
            "storage_maintenance_gates",
            "storage_operations",
            "storage_usage_mutations",
        ):
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}".{table} (LIKE public.{table} INCLUDING ALL)'
                )
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            for revision in (
                "0212_prediction_input_leases",
                "0238_prediction_input_ownership",
                "0239_prediction_input_versions",
            ):
                await connection.run_sync(
                    lambda sync, name=revision: _migration(sync, name).upgrade()
                )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
        world = SimpleNamespace(
            factory=factory,
            objects={},
            metadata={},
            versions={},
            current_versions={},
            versioning="Enabled",
            versioning_reads=[],
            reads=[],
            heads=[],
            signed_versions=[],
            delete_markers=[],
            clients=[],
            puts=[],
            bodies=[],
            deleted=[],
            expirations=[],
            started=None,
            release=None,
            after_put=None,
            corrupt_read=False,
            fail_close=False,
            extra_version=False,
            headers={},
        )
        monkeypatch.setattr(owner, "make_storage", lambda: Storage(world))
        yield world
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def inputs(world, **kwargs):
    return await owner.PredictionInputs.create(
        world.factory,
        owner_id=uuid.uuid4(),
        owner_kind="batch_predict",
        task_id=uuid.uuid4(),
        **kwargs,
    )


async def lease(world, identifier):
    async with world.factory() as db:
        return await db.get(Lease, identifier)


def snapshot():
    buffer = io.BytesIO()
    with Image.new("RGB", (3, 2), (4, 5, 6)) as image:
        image.save(buffer, "PNG")
    raw = buffer.getvalue()
    file = tempfile.TemporaryFile("w+b")
    file.write(raw)
    file.seek(0)
    return Snapshot(
        file,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        {},
        "image/png",
        3,
        2,
        _cleanup=file.close,
    )


async def test_planned_intent_precedes_put_and_verified_receipt_survives_cleanup(world):
    current = await inputs(world, source_ref={"canonical": "frozen"})
    image = snapshot()
    world.started, world.release = threading.Event(), threading.Event()
    pending = asyncio.create_task(current.upload_snapshot(image))
    try:
        assert await asyncio.to_thread(world.started.wait, 3)
        row = await lease(world, current.record.id)
        entry = next(iter(row.inventory["objects"].values()))
        assert entry["state"] == "planned" and entry["etag"] is None
        assert row.object_count == 1 and row.size_bytes == image.size
        assert not world.objects and not image.file.closed
        assert world.versioning_reads == [current.record.bucket]
        async with world.factory() as db:
            assert await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.purpose == owner.purpose(current.record)
                )
            )
    finally:
        world.release.set()
    url = await pending
    assert current.prefix in url and all(body.closed for body in world.bodies)
    assert all(client.closed for client in world.clients)
    await current.check_live()
    async with world.factory() as db:
        await owner.check_metadata(db, current)
        await db.rollback()
    row = await lease(world, current.record.id)
    saved = deepcopy(row.inventory)
    entry = next(iter(saved["objects"].values()))
    assert entry["state"] == "verified" and entry["version_id"] not in {None, "null"}
    assert world.signed_versions == [entry["version_id"]]
    assert all(version in {None, entry["version_id"]} for _, _, version in world.heads)
    assert all(version == entry["version_id"] for _, _, version in world.reads)
    await current.close()
    row = await lease(world, current.record.id)
    assert row.state == "deleted" and row.deleted_at is not None
    assert row.inventory == saved and row.source_ref == {"canonical": "frozen"}
    assert image.file.closed and not world.objects


async def test_duplicate_crop_is_verified_not_overwritten_and_tamper_is_refused(world):
    current = await inputs(world)
    task = SimpleNamespace(id=current.record.task_id)
    await current.upload_crop(task, 0, b"encoded crop")
    await current.upload_crop(task, 0, b"encoded crop")
    assert len(world.puts) == 1
    key = next(iter(world.objects))
    world.objects[key] = b"changed crop"
    with pytest.raises(ContentError, match="integrity_mismatch"):
        await current.upload_crop(task, 0, b"encoded crop")
    assert len(world.puts) == 1
    await current.close()
    assert world.objects and not world.deleted
    assert (await lease(world, current.record.id)).state == "cleanup_pending"


async def test_changed_default_bucket_does_not_redirect_existing_lease(
    world, monkeypatch
):
    current = await inputs(world)
    saved_bucket = current.record.bucket
    monkeypatch.setattr(settings, "minio_import_bucket", saved_bucket + "-other")
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 1, b"crop")
    assert world.puts[0][0] == saved_bucket
    # Allocation follows the saved binding; a changed default is inert rather
    # than a reason to reject the existing lease or redirect new inputs.
    replacement = await inputs(world)
    assert replacement.record.bucket == saved_bucket
    await replacement.close()
    await current.close()
    assert (await lease(world, current.record.id)).state == "deleted"


@pytest.mark.parametrize("failure", ["corrupt", "revoke"])
async def test_readback_and_post_io_authority_fail_without_delivery(world, failure):
    revoked = False

    async def guard(db):
        if revoked:
            raise ContentError("original_key_revoked", 403)

    current = await inputs(world, guard=guard)
    if failure == "corrupt":
        world.corrupt_read = True
    else:

        def revoke():
            nonlocal revoked
            revoked = True

        world.after_put = revoke
    with pytest.raises(ContentError, match="integrity_mismatch|original_key_revoked"):
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, b"crop"
        )
    row = await lease(world, current.record.id)
    assert next(iter(row.inventory["objects"].values()))["state"] == "planned"
    assert all(body.closed for body in world.bodies)
    assert all(client.closed for client in world.clients)
    await current.close()
    if failure == "corrupt":
        assert world.objects and not world.deleted
    else:
        assert not world.objects and world.deleted
        assert (await lease(world, current.record.id)).state == "cleanup_pending"


async def test_unknown_client_close_retains_operation_beyond_all_deadlines(
    world, monkeypatch
):
    current = await inputs(world)
    world.fail_close = True
    with pytest.raises(ContentError) as caught:
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, b"crop"
        )
    assert caught.value.storage_operation_unsettled
    world.fail_close = False
    with pytest.raises(ContentError) as caught:
        await current.close()
    assert caught.value.storage_operation_unsettled
    monkeypatch.setattr(
        owner, "now", lambda: current.record.retain_until + timedelta(days=1)
    )
    assert (await owner.maintain(world.factory))["deferred"] == 1
    async with world.factory() as db:
        assert await db.scalar(
            select(StorageOperation.id).where(
                StorageOperation.purpose == owner.purpose(current.record)
            )
        )
    assert world.objects and not world.deleted
    assert (await lease(world, current.record.id)).state == "cleanup_pending"


async def test_any_location_same_purpose_blocks_cleanup(world, monkeypatch):
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    other = uuid.uuid4()
    async with world.factory() as db:
        db.add(StorageMaintenanceGate(location_id=other))
        db.add(
            StorageOperation(
                location_id=other,
                purpose=owner.purpose(current.record),
                revision=1,
                node_id="unknown-node",
            )
        )
        await db.commit()
    await current.close()
    monkeypatch.setattr(
        owner, "now", lambda: current.record.retain_until + timedelta(days=1)
    )
    assert (await owner.maintain(world.factory))["deferred"] == 1
    assert world.objects and not world.deleted


@pytest.mark.parametrize("unexpected", ["key", "version", "delete_marker"])
async def test_cleanup_never_deletes_unknown_key_or_version(world, unexpected):
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    if unexpected == "key":
        world.objects[current.record.bucket, current.prefix + "unregistered.bin"] = (
            b"unknown"
        )
    elif unexpected == "version":
        world.extra_version = True
    else:
        world.delete_markers = [
            {"Key": next(iter(world.objects))[1], "VersionId": "foreign-delete-marker"}
        ]
    before = dict(world.objects)
    await current.close()
    assert world.objects == before and not world.deleted
    assert (await lease(world, current.record.id)).state == "cleanup_pending"


async def test_legacy_without_pin_is_not_promoted_or_physically_opened(world):
    identifier = uuid.uuid4()
    checked = owner.now()
    async with world.factory() as db:
        db.add(
            Lease(
                id=identifier,
                owner_id=uuid.uuid4(),
                owner_kind="historical",
                task_id=uuid.uuid4(),
                contract_version=1,
                bucket=settings.minio_import_bucket,
                storage_identity=owner._identity(
                    owner._endpoint(), settings.minio_import_bucket
                ),
                state="cleanup_pending",
                uncertain_write=True,
                expires_at=checked,
                retain_until=checked + timedelta(days=7),
                next_cleanup_at=checked,
            )
        )
        await db.commit()
    assert await owner.purge_input(world.factory, identifier) == "deferred"
    row = await lease(world, identifier)
    assert (
        row.contract_version == 1 and row.storage_ref is None and row.inventory is None
    )
    assert row.error_code == "legacy_unpinned" and not world.clients


@pytest.mark.parametrize(
    "change", ["expiry", "source", "erase", "receipt", "nonce", "delete"]
)
async def test_database_freezes_facts_and_verified_inventory(world, change):
    current = await inputs(world, source_ref={"source": "immutable"})
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    async with world.factory() as db:
        row = await db.get(Lease, current.record.id)
        if change == "delete":
            statement = delete(Lease).where(Lease.id == row.id)
        else:
            values = {}
            if change == "expiry":
                values["expires_at"] = row.expires_at + timedelta(hours=1)
            elif change == "source":
                values["source_ref"] = {"source": "changed"}
            elif change == "erase":
                values = {
                    "inventory": {"schema": 1, "objects": {}},
                    "object_count": 0,
                    "size_bytes": 0,
                }
            else:
                inventory = deepcopy(row.inventory)
                field = "write_nonce" if change == "nonce" else "etag"
                next(iter(inventory["objects"].values()))[field] = (
                    str(uuid.uuid4()) if change == "nonce" else '"different"'
                )
                values["inventory"] = inventory
            statement = update(Lease).where(Lease.id == row.id).values(**values)
        with pytest.raises(DBAPIError):
            await db.execute(statement)
        await db.rollback()
    await current.close()


async def test_downgrade_refuses_empty_owner_and_tombstone(world):
    current = await inputs(world)
    for terminal in (False, True):
        if terminal:
            await current.close()
        async with world.factory() as db:
            connection = await db.connection()
            with pytest.raises(RuntimeError, match="responsibility"):
                await connection.run_sync(
                    lambda sync: _migration(
                        sync, "0238_prediction_input_ownership"
                    ).downgrade()
                )
            await db.rollback()


@pytest.mark.parametrize("interrupted", [asyncio.CancelledError, SoftTimeLimitExceeded])
async def test_executor_drains_repeated_interruptions_and_preserves_late_unknown(
    monkeypatch, interrupted
):
    started, release = threading.Event(), threading.Event()
    shield = asyncio.shield
    count = 0

    def physical():
        started.set()
        assert release.wait(5)
        raise storage_access.mark_unsettled(OSError("late physical close unknown"))

    async def fail(kind):
        raise kind()

    def interrupt(pending):
        nonlocal count
        count += 1
        if count == 1 and interrupted is SoftTimeLimitExceeded:
            return fail(interrupted)
        if count == 2:
            return fail(SoftTimeLimitExceeded)
        return shield(pending)

    monkeypatch.setattr(asyncio, "shield", interrupt)

    async def invoke():
        try:
            await owner.settled_io(physical)
        except BaseException as exc:
            return exc

    task = asyncio.create_task(invoke())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        if interrupted is asyncio.CancelledError:
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    failure = await task
    assert isinstance(failure, interrupted) and failure.storage_operation_unsettled


async def test_object_budget_rejects_before_sdk_or_partial_reservation(
    world, monkeypatch
):
    current = await inputs(world)
    task = SimpleNamespace(id=current.record.task_id)
    monkeypatch.setattr(owner, "MAX_OBJECTS", 1)
    await current.upload_crop(task, 0, b"first")
    with pytest.raises(ContentError, match="budget_exceeded"):
        await current.upload_crop(task, 1, b"second")
    row = await lease(world, current.record.id)
    assert row.object_count == 1 and row.size_bytes == 5
    assert len(row.inventory["objects"]) == len(world.puts) == 1
    await current.close()


async def test_final_metadata_uses_callers_transaction_without_guard_recursion(world):
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")

    async def must_not_run(db):
        raise AssertionError("final metadata must not reenter the authority callback")

    current.guard = must_not_run
    async with world.factory() as db:
        before = db.sync_session.get_transaction()
        await owner.check_metadata(db, current)
        assert db.sync_session.get_transaction() is not None
        if before is not None:
            assert db.sync_session.get_transaction() is before
        await db.rollback()
    current.guard = None
    await current.close()


async def test_successful_already_done_future_does_not_erase_soft_limit(monkeypatch):
    shield = asyncio.shield
    calls = 0

    async def interrupt_after_success(pending):
        await shield(pending)
        raise SoftTimeLimitExceeded()

    def interrupted(pending):
        nonlocal calls
        calls += 1
        return interrupt_after_success(pending)

    monkeypatch.setattr(asyncio, "shield", interrupted)
    with pytest.raises(SoftTimeLimitExceeded):
        await owner.settled_io(lambda: "physically complete")
    assert calls == 1


async def test_successful_put_before_publication_is_reclaimed_only_by_saved_nonce(
    world, monkeypatch
):
    current = await inputs(world)

    async def fail_publish(*args):
        raise RuntimeError("injected interruption before receipt publication")

    monkeypatch.setattr(current, "_publish", fail_publish)
    with pytest.raises(ContentError, match="upload_failed"):
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, b"crop"
        )
    row = await lease(world, current.record.id)
    saved_inventory = deepcopy(row.inventory)
    entry = next(iter(saved_inventory["objects"].values()))
    assert entry["state"] == "planned" and entry["etag"] is None
    assert world.objects and not world.deleted
    async with world.factory() as db:
        assert not await db.scalar(
            select(StorageOperation.id).where(
                StorageOperation.purpose == owner.purpose(current.record)
            )
        )
    await current.close()
    assert not world.objects and world.deleted
    row = await lease(world, current.record.id)
    assert row.state == "cleanup_pending" and row.inventory == saved_inventory
    monkeypatch.setattr(
        owner, "now", lambda: current.record.retain_until + timedelta(seconds=1)
    )
    assert (await owner.maintain(world.factory))["removed"] == 1
    assert (await lease(world, current.record.id)).inventory == saved_inventory


@pytest.mark.parametrize("proof", ["matching", "missing", "other_nonce", "other_lease"])
async def test_412_requires_prior_durable_lease_and_nonce_not_just_same_sha(
    world, proof
):
    current = await inputs(world)
    content = b"same bytes"
    digest = hashlib.sha256(content).hexdigest()
    relative = f"crops/0-{digest}.jpg"
    entry = await current._reserve(
        relative,
        dict(
            sha256=digest,
            size_bytes=len(content),
            media_type="image/jpeg",
            state="planned",
            etag=None,
            version_id=None,
            write_nonce=str(uuid.uuid4()),
        ),
    )
    key = current.record.bucket, current.prefix + relative
    metadata = {
        "aap-prediction-lease": str(current.record.id),
        "aap-prediction-write": entry["write_nonce"],
    }
    if proof == "missing":
        metadata = {}
    elif proof == "other_nonce":
        metadata["aap-prediction-write"] = str(uuid.uuid4())
    elif proof == "other_lease":
        metadata["aap-prediction-lease"] = str(uuid.uuid4())
    Storage.seed(world, *key, content, metadata)
    if proof == "matching":
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, content
        )
        row = await lease(world, current.record.id)
        observed = row.inventory["objects"][relative]
        assert (
            observed["state"] == "verified"
            and observed["write_nonce"] == entry["write_nonce"]
        )
        await current.close()
        assert not world.objects and world.deleted
    else:
        with pytest.raises(ContentError, match="ownership_mismatch"):
            await current.upload_crop(
                SimpleNamespace(id=current.record.task_id), 0, content
            )
        await current.close()
        assert world.objects[key] == content and not world.deleted
        row = await lease(world, current.record.id)
        assert row.inventory["objects"][relative] == entry
    assert not world.puts


@pytest.mark.parametrize("field", ["ETag", "VersionId"])
async def test_full_readback_publishes_maximum_ascii_header_receipt(world, field):
    # Exercise the actual migration trigger, including PostgreSQL's ARE parser.
    world.headers[field] = "a" * 1024
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    row = await lease(world, current.record.id)
    entry = next(iter(row.inventory["objects"].values()))
    assert entry["state"] == "verified"
    assert len(entry["etag" if field == "ETag" else "version_id"]) == 1024
    await current.close()
    assert (await lease(world, current.record.id)).state == "deleted"
    assert not world.objects


@pytest.mark.parametrize("field", ["etag", "version_id"])
@pytest.mark.parametrize("header", ["a" * 1025, "non-ascii-é", "line\nbreak", ""])
async def test_database_refuses_out_of_contract_header_receipt(world, field, header):
    current = await inputs(world)
    digest = hashlib.sha256(b"crop").hexdigest()
    relative = f"crops/0-{digest}.jpg"
    await current._reserve(
        relative,
        dict(
            sha256=digest,
            size_bytes=4,
            media_type="image/jpeg",
            state="planned",
            etag=None,
            version_id=None,
            write_nonce=str(uuid.uuid4()),
        ),
    )
    async with world.factory() as db:
        row = await db.get(Lease, current.record.id)
        inventory = deepcopy(row.inventory)
        entry = inventory["objects"][relative]
        entry.update(state="verified", etag='"valid-etag"', version_id="valid-version")
        entry[field] = header
        with pytest.raises(DBAPIError):
            await db.execute(
                update(Lease).where(Lease.id == row.id).values(inventory=inventory)
            )
        await db.rollback()
    await current.close()


@pytest.mark.parametrize("status", [None, "Suspended"])
async def test_new_put_requires_enabled_saved_bucket_before_any_write(world, status):
    current = await inputs(world)
    world.versioning = status
    with pytest.raises(ContentError, match="bucket_versioning_required"):
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, b"crop"
        )
    row = await lease(world, current.record.id)
    assert next(iter(row.inventory["objects"].values()))["state"] == "planned"
    assert world.versioning_reads == [current.record.bucket]
    assert not world.puts and not world.objects and not world.signed_versions
    assert all(client.closed for client in world.clients)
    await current.close()


async def test_suspending_versioning_after_probe_retains_unverified_null_put(world):
    current = await inputs(world)
    world.started, world.release = threading.Event(), threading.Event()
    pending = asyncio.create_task(
        current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    )
    try:
        assert await asyncio.to_thread(world.started.wait, 3)
        assert world.versioning_reads == [current.record.bucket]
        assert world.versioning == "Enabled" and not world.objects
        world.versioning = "Suspended"
        world.release.set()
        with pytest.raises(ContentError, match="immutable_version_required"):
            await pending
        saved = deepcopy((await lease(world, current.record.id)).inventory)
        entry = next(iter(saved["objects"].values()))
        assert entry["state"] == "planned" and entry["version_id"] is None
        assert set(next(iter(world.versions.values()))) == {"null"}
        assert world.objects and not world.signed_versions
        assert all(client.closed for client in world.clients)
    finally:
        world.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await current.close()
    row = await lease(world, current.record.id)
    assert row.state == "cleanup_pending" and row.inventory == saved
    assert row.error_code == "prediction_input_immutable_version_required"
    assert world.objects and not world.deleted and not world.signed_versions


async def test_put_receipt_stays_on_its_version_when_acknowledgement_loses_current(
    world,
):
    current = await inputs(world)
    replacements = []

    def replace_current():
        identity = world.puts[-1]
        replacements.append(Storage.seed(world, *identity, b"foreign-new-current", {}))

    world.after_put = replace_current
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    row = await lease(world, current.record.id)
    relative, entry = next(iter(row.inventory["objects"].items()))
    identity = current.record.bucket, current.prefix + relative
    assert entry["state"] == "verified" and entry["version_id"] != replacements[0]
    assert world.current_versions[identity] == replacements[0]
    assert world.objects[identity] == b"foreign-new-current"
    assert world.signed_versions == [entry["version_id"]]
    assert all(version in {None, entry["version_id"]} for _, _, version in world.heads)
    assert all(version == entry["version_id"] for _, _, version in world.reads)
    await current.close()
    assert not world.deleted and len(world.versions[identity]) == 2
    assert (await lease(world, row.id)).inventory == row.inventory


@pytest.mark.parametrize("foreign", [False, True])
async def test_history_behind_delete_marker_is_listed_and_never_swept(world, foreign):
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    row = await lease(world, current.record.id)
    relative, entry = next(iter(row.inventory["objects"].items()))
    key = current.prefix + ("unregistered.bin" if foreign else relative)
    identity = current.record.bucket, key
    historical = (
        Storage.seed(world, *identity, b"unknown-history", {})
        if foreign
        else entry["version_id"]
    )
    marker = str(uuid.uuid4())
    world.objects.pop(identity)
    world.metadata.pop(identity)
    world.current_versions[identity] = marker
    world.delete_markers.append(
        {"Bucket": identity[0], "Key": key, "VersionId": marker, "IsLatest": True}
    )
    versions = deepcopy(world.versions)
    probe = Storage(world)
    try:
        listed = probe.list_object_versions(Bucket=identity[0], Prefix=current.prefix)
        assert any(
            item["Key"] == key
            and item["VersionId"] == historical
            and item["IsLatest"] is False
            for item in listed["Versions"]
        )
        assert listed["DeleteMarkers"] == [
            {"Key": key, "VersionId": marker, "IsLatest": True}
        ]
        assert not any(
            item["Key"] == key
            for item in probe.list_objects_v2(
                Bucket=identity[0], Prefix=current.prefix
            )["Contents"]
        )
    finally:
        probe.close()
    await current.close()
    assert world.versions == versions and not world.deleted
    after = await lease(world, row.id)
    assert after.state == "cleanup_pending" and after.inventory == row.inventory
    assert after.error_code == "prediction_input_unknown_object"


async def test_neighbor_prefix_delete_marker_does_not_block_exact_cleanup(world):
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    neighbor = current.record.bucket, current.prefix.rstrip("/") + "-neighbor/old.bin"
    old = Storage.seed(world, *neighbor, b"neighbor-history", {})
    world.objects.pop(neighbor)
    world.metadata.pop(neighbor)
    world.current_versions[neighbor] = "neighbor-marker"
    world.delete_markers.append(
        {
            "Bucket": neighbor[0],
            "Key": neighbor[1],
            "VersionId": "neighbor-marker",
            "IsLatest": True,
        }
    )
    await current.close()
    assert (await lease(world, current.record.id)).state == "deleted"
    assert set(world.versions) == {neighbor} and old in world.versions[neighbor]
    assert neighbor not in world.deleted


@pytest.mark.parametrize("version", [None, "null"])
async def test_null_put_version_never_publishes_or_deletes_after_bucket_check(
    world, monkeypatch, version
):
    current = await inputs(world)
    world.headers["VersionId"] = version
    with pytest.raises(ContentError, match="immutable_version_required"):
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, b"crop"
        )
    row = await lease(world, current.record.id)
    saved = deepcopy(row.inventory)
    assert next(iter(saved["objects"].values()))["state"] == "planned"
    assert world.versioning_reads == [current.record.bucket]
    assert world.objects and not world.signed_versions
    await current.close()
    monkeypatch.setattr(
        owner, "now", lambda: current.record.retain_until + timedelta(days=1)
    )
    assert (await owner.maintain(world.factory))["deferred"] == 1
    assert world.objects and not world.deleted
    row = await lease(world, current.record.id)
    assert row.state == "cleanup_pending" and row.inventory == saved


@pytest.mark.parametrize("stage", ["head", "get", "final_head"])
async def test_every_readback_step_must_return_the_pinned_put_version(
    world, monkeypatch, stage
):
    current = await inputs(world)
    head, get = Storage.head_object, Storage.get_object
    calls = 0

    def changed_head(client, **kwargs):
        nonlocal calls
        calls += 1
        value = head(client, **kwargs)
        if (stage == "head" and calls == 1) or (stage == "final_head" and calls == 2):
            value["VersionId"] = "different-version"
        return value

    def changed_get(client, **kwargs):
        value = get(client, **kwargs)
        if stage == "get":
            value["VersionId"] = "different-version"
        return value

    monkeypatch.setattr(Storage, "head_object", changed_head)
    monkeypatch.setattr(Storage, "get_object", changed_get)
    with pytest.raises(ContentError, match="integrity_mismatch"):
        await current.upload_crop(
            SimpleNamespace(id=current.record.task_id), 0, b"crop"
        )
    row = await lease(world, current.record.id)
    assert next(iter(row.inventory["objects"].values()))["state"] == "planned"
    assert not world.signed_versions
    assert all(body.closed for body in world.bodies)
    await current.close()


async def test_verified_delivery_stays_on_saved_version_and_extra_version_blocks_cleanup(
    world,
):
    current = await inputs(world)
    task, content = SimpleNamespace(id=current.record.task_id), b"crop"
    first = await current.upload_crop(task, 0, content)
    row = await lease(world, current.record.id)
    relative, entry = next(iter(row.inventory["objects"].items()))
    key = current.record.bucket, current.prefix + relative
    new_version = Storage.seed(world, *key, b"new-current-foreign-object", {})
    assert new_version != entry["version_id"]
    second = await current.upload_crop(task, 0, content)
    assert first == second
    assert world.signed_versions == [entry["version_id"], entry["version_id"]]
    assert all(version == entry["version_id"] for _, _, version in world.reads)
    assert world.objects[key] == b"new-current-foreign-object"
    await current.close()
    assert not world.deleted and len(world.versions[key]) == 2
    assert (await lease(world, current.record.id)).inventory == row.inventory


@pytest.mark.parametrize("version", [None, "null"])
async def test_database_refuses_new_null_version_receipt(world, version):
    current = await inputs(world)
    digest = hashlib.sha256(b"crop").hexdigest()
    relative = f"crops/0-{digest}.jpg"
    await current._reserve(
        relative,
        dict(
            sha256=digest,
            size_bytes=4,
            media_type="image/jpeg",
            state="planned",
            etag=None,
            version_id=None,
            write_nonce=str(uuid.uuid4()),
        ),
    )
    async with world.factory() as db:
        row = await db.get(Lease, current.record.id)
        value = deepcopy(row.inventory)
        value["objects"][relative].update(
            state="verified", etag='"etag"', version_id=version
        )
        with pytest.raises(DBAPIError, match="require immutable object versions"):
            await db.execute(
                update(Lease).where(Lease.id == row.id).values(inventory=value)
            )
        await db.rollback()
    await current.close()


@pytest.mark.parametrize("version", [None, "null"])
async def test_historical_null_receipt_retires_and_remaps_without_any_delete(
    world, version
):
    current = await inputs(world)
    digest = hashlib.sha256(b"crop").hexdigest()
    relative = f"crops/0-{digest}.jpg"
    entry = await current._reserve(
        relative,
        dict(
            sha256=digest,
            size_bytes=4,
            media_type="image/jpeg",
            state="planned",
            etag=None,
            version_id=None,
            write_nonce=str(uuid.uuid4()),
        ),
    )
    metadata = {
        "aap-prediction-lease": str(current.record.id),
        "aap-prediction-write": entry["write_nonce"],
    }
    Storage.seed(
        world,
        current.record.bucket,
        current.prefix + relative,
        b"crop",
        metadata,
        version="null",
    )
    # A real prior migration state, never trigger disabling or receipt adoption.
    async with world.factory() as db:
        connection = await db.connection()
        await connection.run_sync(
            lambda sync: _migration(sync, "0239_prediction_input_versions").downgrade()
        )
        row = await db.get(Lease, current.record.id)
        value = deepcopy(row.inventory)
        value["objects"][relative].update(
            state="verified", etag='"' + digest + '"', version_id=version
        )
        await db.execute(
            update(Lease).where(Lease.id == row.id).values(inventory=value)
        )
        await connection.run_sync(
            lambda sync: _migration(sync, "0239_prediction_input_versions").upgrade()
        )
        await db.commit()
    with pytest.raises(ContentError, match="object_not_verified"):
        await current.check_live()
    await current.close()
    row = await lease(world, current.record.id)
    assert row.state == "cleanup_pending" and row.inventory == value
    assert row.error_code == "prediction_input_immutable_version_required"
    # The fake provider ignores IfMatch; reaching DELETE would destroy the null
    # object. Neither retirement nor bucket remapping may grant that operation.
    assert world.objects and not world.deleted
    async with world.factory() as db:
        row = await db.get(Lease, current.record.id)
        bucket = row.bucket + "-restored"
        ref = {
            **row.storage_ref,
            "bucket": bucket,
            "storage_identity": owner._identity(owner._endpoint(), bucket),
        }
        await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
        await db.execute(
            update(Lease)
            .where(Lease.id == row.id)
            .values(
                bucket=bucket, storage_identity=ref["storage_identity"], storage_ref=ref
            )
        )
        await db.commit()
    assert (await lease(world, current.record.id)).inventory == value
    assert not world.deleted


async def test_delete_targets_saved_version_when_current_changes_and_ifmatch_is_ignored(
    world, monkeypatch
):
    current = await inputs(world)
    await current.upload_crop(SimpleNamespace(id=current.record.task_id), 0, b"crop")
    row = await lease(world, current.record.id)
    relative, entry = next(iter(row.inventory["objects"].items()))
    key = current.record.bucket, current.prefix + relative
    delete = Storage.delete_object
    replacement = []

    def concurrent_replace(client, **kwargs):
        assert kwargs["VersionId"] == entry["version_id"]
        replacement.append(Storage.seed(world, *key, b"concurrent-current", {}))
        # Model the real server's ignored conditional header explicitly.
        return delete(client, **{**kwargs, "IfMatch": '"wrong-etag"'})

    monkeypatch.setattr(Storage, "delete_object", concurrent_replace)
    await current.close()
    assert world.objects[key] == b"concurrent-current"
    assert set(world.versions[key]) == set(replacement)
    assert entry["version_id"] not in world.versions[key]
    after = await lease(world, current.record.id)
    assert after.state == "cleanup_pending" and after.inventory == row.inventory


def test_download_url_version_is_optional_and_exact(monkeypatch):
    from app.services.storage import StorageService

    storage = object.__new__(StorageService)
    storage.bucket = "saved-bucket"
    sign = Mock(return_value="https://saved.test/signed")
    storage.client = SimpleNamespace(
        generate_presigned_url=sign,
        meta=SimpleNamespace(endpoint_url="https://saved.test"),
    )
    storage.generate_download_url("object", align=False)
    assert sign.call_args.kwargs["Params"] == {
        "Bucket": "saved-bucket",
        "Key": "object",
    }
    storage.generate_download_url("object", align=False, version_id="exact-version")
    assert sign.call_args.kwargs["Params"] == {
        "Bucket": "saved-bucket",
        "Key": "object",
        "VersionId": "exact-version",
    }

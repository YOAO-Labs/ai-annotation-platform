"""Frozen outputs retain SDK semantics and wait for the outer build owner."""

import asyncio
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.db.models.storage_maintenance import StorageOperation
from app.services import media_cache_accounting, storage_access
from app.services import training_dataset_originals as originals
from app.services import training_write_accounting as accounting
from app.services import training_storage_io
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError
from app.workers import training_dataset as worker
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_export_zip_accounting import Provider as ZipProvider
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory
from tests.test_training_dataset_references import (
    reference_training as reference_training,
    create_version,
    build,
)
from tests.training_original_fixtures import image_bytes, original_receipt, staged_image


class Provider(ZipProvider):
    def __init__(self):
        super().__init__()
        self.objects, self.metadata, self.upload_args, self.bodies = {}, {}, [], []
        self.body_factory = io.BytesIO
        self.close_failure = False

    def select(self, bucket, key):
        self.versions = self.objects.setdefault((bucket, key), [])

    def head_object(self, **params):
        self.select(params["Bucket"], params["Key"])
        return {
            **super().head_object(**params),
            "Metadata": self.metadata.get((params["Bucket"], params["Key"]), {}),
        }

    def list_object_versions(self, **params):
        self.select(params["Bucket"], params["Prefix"])
        return super().list_object_versions(**params)

    def get_object(self, **params):
        result = super().get_object(**params)
        result["Body"].close()
        result["Body"] = self.body_factory(self.versions[-1]["data"])
        self.bodies.append(result["Body"])
        return result

    def upload_file(self, filename, bucket, key, *, ExtraArgs):
        self.uploads += 1
        self.upload_args.append((bucket, key, ExtraArgs))
        operation = "PutObject"
        model = SimpleNamespace(name=operation)
        if self.hooks:
            self.meta.events.emit(
                f"before-parameter-build.s3.{operation}",
                params={"Bucket": bucket, "Key": key},
                model=model,
            )
            for _ in range(self.sends):
                self.meta.events.emit(f"before-send.s3.{operation}")
        self.select(bucket, key)
        self.seed(Path(filename).read_bytes(), "new-" + str(self.uploads))
        self.metadata[bucket, key] = ExtraArgs.get("Metadata", {})
        if self.after_upload:
            self.after_upload(self)
        if self.lose_ack:
            raise OSError("lost final reply")
        if self.hooks:
            self.meta.events.emit(
                f"after-call.s3.{operation}",
                model=model,
                http_response=SimpleNamespace(status_code=200),
                parsed={
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                    "VersionId": self.versions[-1]["version"]
                    if self.version == "new"
                    else self.version,
                },
            )

    def close(self):
        super().close()
        if self.close_failure:
            raise OSError("client close unknown")


def target(provider):
    @contextmanager
    def client(**kwargs):
        assert kwargs == {"owned": True}
        provider.closed = False
        try:
            yield provider
        finally:
            storage_access.close_registered(provider)

    return SimpleNamespace(
        client=client,
        location_id=LEGACY_LOCATION_ID,
        bucket="saved-training-target",
    )


def recorder(factory, provider, owner_id=None):
    return media_cache_accounting.Recorder(
        factory,
        "training_dataset_outputs",
        owner_id or uuid.uuid4(),
        provider.meta.endpoint_url,
        "saved-training-target",
        purpose="training_version",
    )


async def facts(factory):
    async with factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


async def package(factory, provider, path, *, rec=None):
    rec = rec or recorder(factory, provider)
    failure = None
    raw = path.read_bytes()
    try:
        await originals.physical(
            training_storage_io.physical,
            target(provider),
            lambda bound: worker._upload_output(
                bound,
                "saved-training-target",
                "retained/attempt/dataset.zip",
                hashlib.sha256(raw).hexdigest(),
                len(raw),
                lambda: bound.upload_file(
                    str(path),
                    "retained/attempt/dataset.zip",
                    bucket="saved-training-target",
                    content_type="application/zip",
                ),
            ),
            accounting=rec,
        )
    except BaseException as exc:
        failure = exc
    assert provider.closed
    failure = await accounting.finish(rec, failure)
    if failure:
        raise failure


async def test_package_new_equal_and_shrink_are_explicit_net_changes(
    accounting_factory, tmp_path
):
    provider, path = Provider(), tmp_path / "dataset.zip"
    for data in (b"all-package-bytes", b"all-package-bytes", b"small"):
        path.write_bytes(data)
        await package(accounting_factory, provider, path)
    rows = await facts(accounting_factory)
    assert [row.current_delta_bytes for row in rows] == [17, 0, -12]
    assert all(row.purpose == "training_version" for row in rows)
    assert (
        provider.upload_args
        == [
            (
                "saved-training-target",
                "retained/attempt/dataset.zip",
                {"ContentType": "application/zip"},
            )
        ]
        * 3
    )
    assert all(body.closed for body in provider.bodies)


async def test_media_and_rle_keep_metadata_and_retained_media_has_no_intent(
    accounting_factory, tmp_path
):
    provider, raw, task_id = Provider(), image_bytes(), str(uuid.uuid4())
    rec = recorder(accounting_factory, provider)
    sample = {
        "task_id": task_id,
        "source_key": "registered-original",
        "original": original_receipt(raw),
        "media": {"size_bytes": len(raw)},
        "raster_contract": True,
        "classes": ["shape"],
        "annotations": [],
    }
    stage = staged_image(raw)
    try:
        result = await originals.physical(
            worker._materialize_image,
            target(provider),
            sample,
            "owned-version",
            "snapshot",
            tmp_path,
            bucket="saved-training-target",
            verified_original=stage,
            accounting=rec,
        )
    finally:
        stage.file.close()
    assert provider.closed and stage.file.closed
    assert await accounting.finish(rec, None) is None
    rows = await facts(accounting_factory)
    assert len(rows) == 2 and all(row.state == "confirmed" for row in rows)
    assert {row.object_key for row in rows} == {
        f"owned-version/media/{task_id}",
        f"owned-version/masks/{task_id}.json",
    }
    assert (
        sum(row.current_delta_bytes for row in rows)
        == len(raw) + Path(result["mask_path"]).stat().st_size
    )
    assert provider.upload_args[0][2] == {
        "Metadata": {
            "snapshot-digest": "snapshot",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    }
    rec = recorder(accounting_factory, provider)
    await originals.physical(
        worker._materialize_image,
        target(provider),
        {**sample, "raster_contract": False},
        "owned-version",
        "snapshot",
        tmp_path,
        bucket="saved-training-target",
        accounting=rec,
    )
    assert await accounting.finish(rec, None) is None
    assert len(await facts(accounting_factory)) == 2 and provider.uploads == 2


@pytest.mark.parametrize(
    "mode", ["retry", "no_hooks", "null", "wrong_read", "history", "body", "client"]
)
async def test_uncertain_upload_readback_or_close_is_unknown(
    accounting_factory, tmp_path, mode
):
    provider, path = Provider(), tmp_path / "dataset.zip"
    path.write_bytes(b"actual saved bytes")
    if mode == "retry":
        provider.sends = 2
    elif mode == "no_hooks":
        provider.hooks = False
    elif mode == "null":
        provider.version = "null"
    elif mode == "wrong_read":
        provider.read_version = "foreign-same-bytes"
    elif mode == "history":
        provider.fail_list = True
    elif mode == "client":
        provider.close_failure = True
    else:

        class Body(io.BytesIO):
            def close(self):
                super().close()
                raise OSError("body close unknown")

        provider.body_factory = Body
    if mode in {"body", "client"}:
        with pytest.raises(OSError):
            await package(accounting_factory, provider, path)
    else:
        await package(accounting_factory, provider, path)
    row = (await facts(accounting_factory))[0]
    assert row.physical_outcome == ("unknown" if mode == "no_hooks" else "written")
    assert row.state == "unknown" and row.current_delta_bytes is None


async def test_intent_failure_prevents_upload(accounting_factory, tmp_path):
    provider, path = Provider(), tmp_path / "dataset.zip"
    path.write_bytes(b"not uploaded")
    rec = SimpleNamespace(
        bucket="saved-training-target",
        begin=lambda *args: (_ for _ in ()).throw(OSError("intent unavailable")),
    )
    with pytest.raises(OSError):
        await package(accounting_factory, provider, path, rec=rec)
    assert provider.uploads == 0 and not await facts(accounting_factory)


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "handled_error",
        "handled_cancel",
        "unconsumed",
        "permit",
        "new_cancel",
        "ledger_error",
    ],
)
async def test_build_wrapper_preserves_consumed_errors_and_finishes_after_contents(
    monkeypatch, mode
):
    events = []
    original = (
        asyncio.CancelledError()
        if mode == "handled_cancel"
        else KeyboardInterrupt()
        if mode == "unconsumed"
        else ValueError("business")
    )
    fresh_cancel = asyncio.CancelledError()

    async def publish(*, settled):
        assert events == ["resources_closed"]
        events.append(("publish", settled))
        if mode == "new_cancel":
            raise fresh_cancel
        if mode == "ledger_error":
            raise OSError("ledger unavailable")

    rec = SimpleNamespace(finish=publish)

    async def contents(*args, accounting, **kwargs):
        accounting.recorder = rec
        accounting.permit = SimpleNamespace(_settled=mode != "permit")
        if mode in {"handled_error", "handled_cancel"}:
            accounting.failure = original
        events.append("resources_closed")
        if mode == "unconsumed":
            raise original

    monkeypatch.setattr(worker, "_build_dataset_contents", contents)
    if mode in {"unconsumed", "new_cancel"}:
        with pytest.raises(BaseException) as caught:
            await worker._build_dataset(None, None, None, None)
        assert caught.value is (fresh_cancel if mode == "new_cancel" else original)
    else:
        assert await worker._build_dataset(None, None, None, None) is None
    assert events[-1] == ("publish", mode != "permit")


@pytest.mark.parametrize("fault", ["none", "package", "directory"])
async def test_real_worker_ledger_waits_for_outer_directory_and_preserves_partial_writes(
    db_session, reference_training, httpx_client, super_admin, monkeypatch, fault
):
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    assert "-test-" in storage_service.training_bucket
    await asyncio.to_thread(
        storage_service.client.put_bucket_versioning,
        Bucket=storage_service.training_bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    db, (_, project, _) = db_session, reference_training
    version, _ = await create_version(
        db,
        httpx_client,
        headers(super_admin),
        project,
        {"task": "segment", "bridge": "ultralytics-instance-rle-1"},
    )
    paths, observed = [], []
    directory = originals.temporary_directory
    finish = accounting.finish

    @contextmanager
    def outer(**kwargs):
        with directory(**kwargs) as path:
            paths.append(Path(path))
            yield path
        if fault == "directory" and kwargs.get("prefix") == "aap-training-":
            raise storage_access.mark_unsettled(OSError("directory close unknown"))

    async def settled(rec, failure, *, settled):
        assert paths and all(not path.exists() for path in paths)
        observed.append(
            (
                rec.producer,
                settled,
                bool(getattr(failure, "storage_operation_unsettled", False)),
            )
        )
        return await finish(rec, failure, settled=settled)

    monkeypatch.setattr(originals, "temporary_directory", outer)
    monkeypatch.setattr(accounting, "finish", settled)
    if fault == "package":

        def broken_package(*args, **kwargs):
            raise TrainingDatasetError("package_test_failure", "packaging failed")

        monkeypatch.setattr(worker, "build_training_package", broken_package)
    await build(db, version)
    assert version.status == ("ready" if fault == "none" else "failed"), (
        version.error_message
    )
    rows = list(
        await db.scalars(
            select(Mutation).where(
                Mutation.producer == "training_dataset_outputs",
                Mutation.owner_id == version.id,
            )
        )
    )
    try:
        assert len(rows) == (4 if fault == "package" else 5)
        assert all(row.physical_outcome == "written" for row in rows)
        assert all(
            row.state == ("unknown" if fault == "directory" else "confirmed")
            for row in rows
        )
        assert all(
            row.current_delta_bytes
            == (None if fault == "directory" else row.physical_size_bytes)
            for row in rows
        )
        assert observed == [
            ("training_dataset_outputs", fault != "directory", fault == "directory")
        ]
    finally:
        if fault == "directory":
            # This synthetic close error follows actual directory removal and
            # drained native work; only this test can attest its handle closed.
            assert all(not path.exists() for path in paths)
            async with storage_access.engine(db).begin() as connection:
                await connection.execute(
                    delete(StorageOperation).where(
                        StorageOperation.location_id
                        == uuid.UUID(version.storage_ref["location_id"]),
                        StorageOperation.purpose
                        == originals.output_purpose(version.id),
                    )
                )

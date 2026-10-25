"""Exercise the AL owner, both report workers and real redacted download entry."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import os
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import training_runs as api
from app.config import settings
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import active_learning as active, storage, storage_access
from app.services import training_storage_io as transport
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.managed_s3_target import Target
from app.services.managed_original_reader import BinaryStage
from tests.test_sample_evaluation_worker import world as world
from tests.test_training_write_accounting import (
    Provider,
    PRODUCERS,
    case,
    endpoint,
    install,
    rows,
)

REAL_RUN, REAL_PHYSICAL = transport.run, transport.physical


@pytest.fixture
async def caller(db_session):
    await ensure_legacy_bindings(db_session)
    await db_session.commit()
    factory = async_sessionmaker(
        storage_access.engine(db_session), expire_on_commit=False
    )
    previous = {row.id for row in await rows(factory)}
    async with factory() as db:
        operations = set(await db.scalars(select(StorageOperation.id)))
    value = SimpleNamespace(db=db_session, factory=factory, previous=previous)
    try:
        yield value
    finally:
        async with factory() as db:
            await db.execute(delete(Mutation).where(Mutation.id.not_in(previous)))
            await db.execute(
                delete(StorageOperation).where(StorageOperation.id.not_in(operations))
            )
            await db.commit()


async def facts(caller):
    return [row for row in await rows(caller.factory) if row.id not in caller.previous]


def target_for(provider, value):
    return Target(
        value.owner.location_id,
        1,
        None,
        endpoint(),
        value.owner.bucket,
        "",
        purpose="training",
        _legacy_client=provider,
    )


def active_call(monkeypatch, caller, value, provider, events, *, stage_failure=False):
    # The full owner and actual native/permit transport run; only source admission
    # is deterministic here, with a real closeable source stage.
    sample = {
        "ordinal": 0,
        "sha256": "a" * 64,
        "derived_sha256": hashlib.sha256(value.data).hexdigest(),
        "width": 4,
        "height": 3,
        "size_bytes": len(value.data),
        "preprocessing": {},
        "snapshot": {},
    }
    value.row.spec = {"original_contract": 1}
    context = SimpleNamespace(proof=None, key=None)
    monkeypatch.setattr(active.originals, "delegated", AsyncMock(return_value=context))
    monkeypatch.setattr(active.originals, "check", AsyncMock())
    monkeypatch.setattr(active.originals.access, "check_clock", lambda *args: None)

    @asynccontextmanager
    async def image(*args, **kwargs):
        stage = BinaryStage(
            io.BytesIO(value.data),
            hashlib.sha256(value.data).hexdigest(),
            len(value.data),
            {},
        )
        try:
            yield value.data, sample, stage
        finally:
            pending = await facts(caller)
            assert not pending or all(row.state == "pending" for row in pending)
            events.append("source_close")
            stage.close()
            if stage_failure:
                raise storage_access.mark_unsettled(OSError("source close unknown"))

    monkeypatch.setattr(active.originals, "input_image", image)
    return lambda: active.prepare_image(caller.db, value.row, sample)


def log_call(monkeypatch, caller, value):
    value.attempt.status = "failed"
    value.attempt.artifacts = {"train.log": {**value.artifact, "role": "log"}}
    db = SimpleNamespace(
        bind=caller.db.bind,
        scalar=AsyncMock(return_value=value.row),
        get=AsyncMock(return_value=value.attempt),
    )
    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    return lambda: api.download_artifact(
        value.row.id,
        value.attempt.id,
        "train.log",
        SimpleNamespace(id=value.row.project_id),
        SimpleNamespace(),
        db,
    )


@pytest.mark.parametrize("kind", ["active", "log"])
@pytest.mark.parametrize(
    "failure", [None, "revoke", "settle", "cancel", "body_close", "publish"]
)
async def test_actual_async_owner_retains_facts_after_physical_drain(
    caller, monkeypatch, kind, failure
):
    value = case(kind)
    provider = Provider()
    install(monkeypatch, provider, value)
    provider.puts = []
    monkeypatch.setattr(transport, "prepare", AsyncMock(return_value=value.target))
    events = []
    call = (
        active_call(monkeypatch, caller, value, provider, events)
        if kind == "active"
        else log_call(monkeypatch, caller, value)
    )
    if failure == "revoke":
        if kind == "active":

            async def check(*args, **kwargs):
                if provider.puts:
                    raise HTTPException(403, "revoked")

            monkeypatch.setattr(active.originals, "check", check)
        else:
            provider.generate_presigned_url = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(HTTPException(403, "revoked"))
    if failure == "settle":

        async def settle(permit):
            raise OSError("permit settle failed")

        monkeypatch.setattr(storage_access.Permit, "_settle", settle)
    if failure == "body_close":

        class Body(io.BytesIO):
            def close(self):
                super().close()
                raise OSError("body close unknown")

        provider.body_factory = Body
    if failure == "publish":
        from app.services import media_cache_accounting

        async def publish(*args, **kwargs):
            raise OSError("ledger deferred")

        monkeypatch.setattr(media_cache_accounting.accounting, "publish", publish)
    if failure == "cancel":
        provider.started, provider.release = threading.Event(), threading.Event()
    task = asyncio.create_task(call())
    if failure == "cancel":
        assert await asyncio.to_thread(provider.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not provider.closed
        provider.release.set()
    if failure in {"revoke", "settle", "cancel", "body_close"}:
        with pytest.raises(BaseException) as caught:
            await task
        if failure == "cancel":
            assert isinstance(caught.value, asyncio.CancelledError)
        if failure == "revoke":
            assert (
                isinstance(caught.value, HTTPException)
                and caught.value.status_code == 403
            )
    else:
        await task
    final = await facts(caller)
    if kind == "log" and failure == "body_close":
        assert not final  # Original input failed before any PUT.
        return
    (fact,) = final
    assert fact.owner_id == value.row.id and fact.producer == PRODUCERS[kind]
    if failure in {"settle", "body_close"}:
        assert fact.state == "unknown" and fact.current_delta_bytes is None
    elif failure == "publish":
        assert fact.state == "pending"
    else:
        assert fact.state == "confirmed" and fact.current_delta_bytes == len(value.data)
    assert provider.closed
    if kind == "active" and failure in {None, "revoke", "cancel", "publish"}:
        assert events == ["source_close"]
    if kind == "active" and failure is None:
        # Retained lookup is an ordinary read and adds no second intent.
        shared = transport.storage_service.client
        shared.head_object = provider.head_object
        shared.get_object = provider.get_object
        await call()
        assert len(await facts(caller)) == 1 and len(provider.puts) == 1


@pytest.mark.parametrize("kind", ["active", "report", "log"])
async def test_real_pg_minio_owned_overwrites_and_net_zero(caller, monkeypatch, kind):
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    bucket = settings.minio_training_bucket
    assert re.fullmatch(r"aap-wt-[0-9a-f]{16}-test-training", bucket)
    service = storage.StorageService()
    service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    value = case(kind, bucket=bucket)
    value.target = target_for(service.client, value)
    monkeypatch.setattr(transport, "prepare", AsyncMock(return_value=value.target))
    if kind == "log":
        service.client.put_object(
            Bucket=bucket,
            Key=value.artifact["key"],
            Body=value.source,
            Metadata={
                "attempt-id": str(value.attempt.id),
                "sha256": value.artifact["sha256"],
            },
        )
    call = (
        active_call(monkeypatch, caller, value, service.client, [])
        if kind == "active"
        else log_call(monkeypatch, caller, value)
        if kind == "log"
        else None
    )
    try:
        if call is not None:
            await call()
        else:
            from app.services import training_write_accounting as accounting

            await transport.run(
                caller.db,
                value.owner,
                "training_test_write",
                value.callback,
                target=value.target,
                accounting=accounting.recorder(
                    caller.db,
                    value.owner,
                    value.row.id,
                    producer=PRODUCERS[kind],
                    purpose="other_platform",
                ),
            )
        (first,) = await facts(caller)
        assert first.state == "confirmed" and first.current_delta_bytes == len(
            value.data
        )
        assert first.evidence["final_send_count"] == 1
        # The original writer itself remains an overwrite operation, even for
        # owners whose retained-read path normally avoids a second write.
        from app.services import training_write_accounting as accounting

        await transport.run(
            caller.db,
            value.owner,
            "training_test_write",
            value.callback,
            target=value.target,
            accounting=accounting.recorder(
                caller.db,
                value.owner,
                value.row.id,
                producer=PRODUCERS[kind],
                purpose="other_platform",
            ),
        )
        first, second = await facts(caller)
        assert second.state == "confirmed" and second.current_delta_bytes == 0
        assert first.id != second.id
    finally:
        for key in [value.key, *([value.artifact["key"]] if kind == "log" else [])]:
            response = service.client.list_object_versions(Bucket=bucket, Prefix=key)
            entries = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for field in ("Versions", "DeleteMarkers")
                for item in response.get(field, [])
                if item["Key"] == key
            ]
            if entries:
                service.client.delete_objects(
                    Bucket=bucket, Delete={"Objects": entries}
                )
            response = service.client.list_object_versions(Bucket=bucket, Prefix=key)
            assert not any(
                item["Key"] == key
                for field in ("Versions", "DeleteMarkers")
                for item in response.get(field, [])
            )
        service.client.close()


@pytest.mark.parametrize("entry", ["finish", "stop"])
@pytest.mark.parametrize("mode", ["sdk_fake", "late_business_failure", "real_minio"])
async def test_both_report_worker_entries_finish_after_real_permit(
    world, monkeypatch, entry, mode
):
    from app.services import sample_evaluations as service
    from app.workers import sample_evaluations as worker
    from tests.sample_evaluation_fixtures import complete_rows

    previous = {row.id for row in await rows(world.factory)}
    claim = await worker.claim(world.factory, world.attempt.id, "accounting")
    async with world.factory() as db:
        run, attempt = await worker.fenced(db, world.attempt.id, claim)
        if entry == "finish":
            await complete_rows(db, run, attempt, world.fixture)
            status = "succeeded"
        else:
            await service.request_cancel(db, run)
            status = "cancelled"
        report = service.build_report(
            run, attempt, await service.sample_rows(db, run), status
        )
        data = (
            __import__("json")
            .dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            .encode()
        )
        owner = service.owners.resolve(run)
        key = owner.key(
            str(attempt.id) + "/" + hashlib.sha256(data).hexdigest() + ".json"
        )
        await db.commit()
    value = SimpleNamespace(kind="report", row=run, owner=owner, target=None)
    real_service = None
    if mode == "real_minio":
        assert os.environ.get("AAP_WORKTREE_MODE") == "test"
        assert re.fullmatch(r"aap-wt-[0-9a-f]{16}-test-training", owner.bucket)
        real_service = storage.StorageService()
        real_service.client.put_bucket_versioning(
            Bucket=owner.bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        value.target = target_for(real_service.client, value)
    else:
        provider = Provider()
        install(monkeypatch, provider, value)
    monkeypatch.setattr(transport, "run", REAL_RUN)
    monkeypatch.setattr(transport, "physical", REAL_PHYSICAL)
    monkeypatch.setattr(transport, "prepare", AsyncMock(return_value=value.target))
    if mode == "late_business_failure":

        async def fail(*args, **kwargs):
            raise OSError("business publication failed")

        monkeypatch.setattr(service, "mark_terminal", fail)
    try:

        async def execute():
            if entry == "finish":
                return await worker.finish(world.factory, attempt.id, claim)
            return await worker.settle_cancel(world.factory, run.id)

        if mode == "late_business_failure":
            with pytest.raises(OSError, match="business publication failed"):
                await execute()
        else:
            await execute()
        final = [row for row in await rows(world.factory) if row.id not in previous]
        (fact,) = final
        assert fact.producer == "sample_evaluation_reports" and fact.owner_id == run.id
        assert fact.state == "confirmed" and fact.current_delta_bytes == len(data)
        assert fact.object_key == key
        async with world.factory() as db:
            assert not list(
                await db.scalars(
                    select(StorageOperation).where(
                        StorageOperation.purpose == "sample_evaluation_write"
                    )
                )
            )
    finally:
        async with world.factory() as db:
            await db.execute(delete(Mutation).where(Mutation.id.not_in(previous)))
            await db.commit()
        if real_service:
            response = real_service.client.list_object_versions(
                Bucket=owner.bucket, Prefix=key
            )
            entries = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for field in ("Versions", "DeleteMarkers")
                for item in response.get(field, [])
                if item["Key"] == key
            ]
            if entries:
                real_service.client.delete_objects(
                    Bucket=owner.bucket, Delete={"Objects": entries}
                )
            assert not real_service.client.list_object_versions(
                Bucket=owner.bucket, Prefix=key
            ).get("Versions")
            real_service.client.close()


@pytest.mark.parametrize("kind", ["active", "log"])
async def test_source_stage_or_observational_output_close_cannot_confirm(
    caller, monkeypatch, kind
):
    value = case(kind)
    provider = Provider()
    install(monkeypatch, provider, value)
    monkeypatch.setattr(transport, "prepare", AsyncMock(return_value=value.target))
    if kind == "active":
        call = active_call(monkeypatch, caller, value, provider, [], stage_failure=True)
    else:
        count = 0

        class Closing(io.BytesIO):
            def close(self):
                super().close()
                raise OSError("output Body close unknown")

        def body(raw):
            nonlocal count
            count += 1
            return io.BytesIO(raw) if count == 1 else Closing(raw)

        provider.body_factory = body
        call = log_call(monkeypatch, caller, value)
    with pytest.raises(OSError):
        await call()
    (fact,) = await facts(caller)
    assert (
        fact.physical_outcome == "written"
        and fact.state == "unknown"
        and fact.current_delta_bytes is None
    )
    assert provider.closed

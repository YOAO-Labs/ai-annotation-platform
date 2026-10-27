"""Archive facts wait for exact readback and the real outer input owner."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

from botocore.config import Config
from botocore.hooks import HierarchicalEmitter
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import audit_archive_storage as physical
from app.services import audit_partition_service as owner
from app.services import conditional_write_accounting as accounting
from app.services import media_cache_accounting, storage, storage_access
from app.services.storage_usage_accounting import scope_key
from tests.test_audit_archive_partitions import source_case as source_case
from tests.test_audit_archive_storage import Client, archive, endpoint
from tests.test_bug_attachment_accounting import s3_error
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory

ORIGINAL_WRITE = physical.write


class Provider(Client):
    def __init__(self):
        super().__init__(b"")
        self.meta = SimpleNamespace(
            endpoint_url=endpoint(),
            config=Config(retries={"max_attempts": 3}),
            events=HierarchicalEmitter(),
        )
        self.present, self.closed = False, False
        self.version, self.sends, self.hooks = "archive-1", 1, True
        self.failure, self.read_version = None, None
        self.before_put = None
        self.close_failure = False
        self.body_identity = None

    def put_object(self, **params):
        if self.before_put:
            self.before_put()
        self.body_identity = params["Body"]
        if self.hooks:
            self.meta.events.emit("before-parameter-build.s3.PutObject", params=params)
            for _ in range(self.sends):
                self.meta.events.emit("before-send.s3.PutObject")
        if self.failure:
            raise self.failure
        if self.present:
            raise s3_error("PreconditionFailed", 412)
        super().put_object(**params)
        self.present = True
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": self.version,
        }

    def head_object(self, **params):
        return {
            **super().head_object(**params),
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": self.version,
        }

    def get_object(self, **params):
        return {
            **super().get_object(**params),
            "VersionId": self.read_version or self.version,
        }

    def close(self):
        self.closed = True
        if self.close_failure:
            raise OSError("owned client close failed")


@pytest.fixture
def provider(monkeypatch):
    client = Provider()
    shared = SimpleNamespace(meta=client.meta)
    monkeypatch.setattr(
        physical,
        "storage_service",
        SimpleNamespace(
            client=shared, audit_archive_bucket=settings.minio_audit_archive_bucket
        ),
    )
    configs = []

    def create(**kwargs):
        configs.append(kwargs["client_config"])
        return SimpleNamespace(client=client)

    monkeypatch.setattr(storage, "StorageService", create)
    client.configs = configs
    return client


async def facts(factory):
    async with factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


async def write(factory, provider):
    row, raw = archive()
    ref = row.storage_ref
    rec = media_cache_accounting.Recorder(
        factory,
        "audit_archive_outputs",
        row.id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose="other_platform",
    )
    failure, closed = None, False
    file = io.BytesIO(raw)
    try:
        await asyncio.to_thread(physical.write, row, file, accounting=rec)
    except BaseException as exc:
        failure = exc
    finally:
        file.close()
        closed = True
    assert provider.closed
    failure = await accounting.finish(rec, failure, settled=closed)
    if failure:
        raise failure
    return row, raw


async def test_file_body_exact_intent_precedes_put_and_duplicate_412_is_zero(
    accounting_factory, provider
):
    loop = asyncio.get_running_loop()

    def before():
        rows = asyncio.run_coroutine_threadsafe(
            facts(accounting_factory), loop
        ).result()
        assert rows[-1].state == "pending"
        assert rows[-1].purpose == "other_platform"

    provider.before_put = before
    row, raw = await write(accounting_factory, provider)
    assert provider.body_identity.closed
    assert provider.raw == raw
    assert provider.configs == [provider.meta.config]
    assert all(body.closed for body in provider.bodies)
    first = (await facts(accounting_factory))[0]
    assert first.owner_id == row.id
    assert first.object_key == row.storage_ref["object_key"]
    assert first.scope_key == scope_key(
        row.storage_ref["endpoint_url"], row.storage_ref["bucket"]
    )
    assert first.current_delta_bytes == len(raw)
    with pytest.raises(physical.AuditArchiveStorageError):
        await write(accounting_factory, provider)
    second = (await facts(accounting_factory))[1]
    assert second.current_delta_bytes == 0 and second.physical_outcome == "not_written"


@pytest.mark.parametrize("mode", ["retry", "no_hooks", "null", "wrong_read", "close"])
async def test_uncertain_submission_version_or_native_close_stays_unknown(
    accounting_factory, provider, mode
):
    if mode == "retry":
        provider.sends = 2
    elif mode == "no_hooks":
        provider.hooks = False
    elif mode == "null":
        provider.version = "null"
    elif mode == "wrong_read":
        provider.read_version = "same-content-foreign-version"
    else:
        provider.close_failure = True
    if mode == "close":
        with pytest.raises(OSError):
            await write(accounting_factory, provider)
    else:
        await write(accounting_factory, provider)
    row = (await facts(accounting_factory))[0]
    assert row.physical_outcome == "written"
    assert row.state == "unknown" and row.current_delta_bytes is None


@pytest.mark.parametrize("failure,sends", [("412", 2), ("409", 1), ("bare412", 1)])
async def test_ambiguous_failures_are_not_zero(
    accounting_factory, provider, failure, sends
):
    provider.failure = s3_error(
        "412" if failure == "bare412" else failure,
        None if failure == "bare412" else int(failure),
    )
    provider.sends = sends
    with pytest.raises(physical.AuditArchiveStorageError):
        await write(accounting_factory, provider)
    row = (await facts(accounting_factory))[0]
    assert row.state == "unknown" and row.current_delta_bytes is None


async def test_intent_failure_stops_actual_put(accounting_factory, provider):
    row, raw = archive()
    rec = SimpleNamespace(
        begin=lambda *args: (_ for _ in ()).throw(OSError("intent unavailable"))
    )
    with io.BytesIO(raw) as file:
        with pytest.raises(physical.AuditArchiveStorageError):
            await asyncio.to_thread(physical.write, row, file, accounting=rec)
    assert not provider.present and provider.body_identity is None


async def test_readback_temporary_file_close_failure_stays_unknown(
    accounting_factory, provider, monkeypatch
):
    class Output(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("readback temporary close failed")

    monkeypatch.setattr(physical.tempfile, "TemporaryFile", Output)
    with pytest.raises(physical.AuditArchiveStorageError):
        await write(accounting_factory, provider)
    row = (await facts(accounting_factory))[0]
    assert row.physical_outcome == "written"
    assert row.state == "unknown" and row.current_delta_bytes is None


@pytest.mark.parametrize("publish_failure", [False, True])
async def test_production_partition_owner_retains_fact_after_publication_failure(
    source_case, provider, monkeypatch, publish_failure
):
    case = source_case
    monkeypatch.setattr(physical, "write", ORIGINAL_WRITE)
    if publish_failure:
        monkeypatch.setattr(
            owner, "_publish", AsyncMock(side_effect=ValueError("business"))
        )
    async with case.maker() as db:
        if publish_failure:
            with pytest.raises(owner.AuditArchiveError):
                await owner.AuditPartitionService.archive_old_partitions(
                    db, job_id=case.job_id
                )
        else:
            result = await owner.AuditPartitionService.archive_old_partitions(
                db, job_id=case.job_id
            )
            assert result["archived_partitions"] == [case.source.name]
    async with case.maker() as db:
        output = await db.scalar(
            select(AuditArchiveOutput).where(AuditArchiveOutput.job_id == case.job_id)
        )
        fact = await db.scalar(select(Mutation).where(Mutation.owner_id == output.id))
        assert output.state == ("pending" if publish_failure else "ready")
        assert (
            fact.state == "confirmed" and fact.current_delta_bytes == output.size_bytes
        )
        assert (
            fact.producer == "audit_archive_outputs" and provider.body_identity.closed
        )
        exists = await db.scalar(
            text("SELECT to_regclass(:name)"), {"name": f"public.{case.source.name}"}
        )
        assert (exists is not None) is publish_failure


class Input(io.BytesIO):
    def __init__(self, *, fail=False):
        super().__init__(b"compressed input")
        self.fail = fail

    def close(self):
        super().close()
        if self.fail:
            raise OSError("input close failed")


@pytest.mark.parametrize(
    "mode", ["success", "publish_error", "cancel", "input_close", "permit", "unsettled"]
)
async def test_outer_owner_finishes_only_after_file_and_actual_permit(
    monkeypatch, mode
):
    file = Input(fail=mode == "input_close")
    row, _ = archive()
    recorded = []
    rec = SimpleNamespace()
    original = asyncio.CancelledError() if mode == "cancel" else ValueError("business")
    if mode == "unsettled":
        storage_access.mark_unsettled(original)

    async def write_candidate(db, receipt, handle, *, accounting):
        assert handle is file and not handle.closed
        accounting.permit = SimpleNamespace(_settled=mode != "permit")
        return 3

    async def publish(*args):
        assert not file.closed
        if mode in {"publish_error", "cancel", "unsettled"}:
            raise original
        return {"published": True}

    async def finish(accounting_rec, failure, *, settled):
        assert file.closed and accounting_rec is rec
        recorded.append((failure, settled))
        return failure

    monkeypatch.setattr(owner, "TemporaryFile", lambda **kwargs: file)
    monkeypatch.setattr(owner, "_candidate", AsyncMock(return_value=row))
    monkeypatch.setattr(owner, "_write_candidate", write_candidate)
    monkeypatch.setattr(owner, "_publish", publish)
    monkeypatch.setattr(accounting, "recorder", lambda *args, **kwargs: rec)
    monkeypatch.setattr(accounting, "finish", finish)
    if mode in {"publish_error", "cancel", "unsettled", "input_close"}:
        with pytest.raises(BaseException) as caught:
            await owner._archive_partition(object(), object(), row.job_id, 1)
        assert (
            isinstance(caught.value, OSError)
            if mode == "input_close"
            else caught.value is original
        )
    else:
        assert await owner._archive_partition(object(), object(), row.job_id, 1) == {
            "published": True
        }
    assert len(recorded) == 1
    # The generic finisher also checks the error's unsettled marker.
    assert recorded[0][1] is (mode not in {"input_close", "permit"})
    if mode == "input_close":
        assert getattr(recorded[0][0], "storage_operation_unsettled", False)


async def test_accounting_drain_cancellation_preserves_propagation_after_input_close(
    monkeypatch,
):
    file, (row, _) = Input(), archive()
    original = asyncio.CancelledError()
    rec = SimpleNamespace(finish=AsyncMock(side_effect=original))

    async def write_candidate(db, receipt, handle, *, accounting):
        accounting.permit = SimpleNamespace(_settled=True)
        return 3

    monkeypatch.setattr(owner, "TemporaryFile", lambda **kwargs: file)
    monkeypatch.setattr(owner, "_candidate", AsyncMock(return_value=row))
    monkeypatch.setattr(owner, "_write_candidate", write_candidate)
    monkeypatch.setattr(owner, "_publish", AsyncMock(return_value={}))
    monkeypatch.setattr(accounting, "recorder", lambda *args, **kwargs: rec)
    with pytest.raises(asyncio.CancelledError) as caught:
        await owner._archive_partition(object(), object(), row.job_id, 1)
    assert caught.value is original and file.closed


async def test_write_admission_captures_actual_permit_before_business_recheck(
    monkeypatch,
):
    row, _ = archive()
    state = owner._WriteAccounting(object())
    permit = SimpleNamespace(revision=7, _settled=False)
    failure = ValueError("admission failed")
    db = SimpleNamespace()

    async def run(*args, on_acquired, **kwargs):
        await on_acquired(permit)

    monkeypatch.setattr(physical, "io_target", AsyncMock(return_value=None))
    monkeypatch.setattr(storage_access, "run", run)
    monkeypatch.setattr(owner, "_begin", AsyncMock(side_effect=failure))
    with pytest.raises(ValueError) as caught:
        await owner._write_candidate(db, row, object(), accounting=state)
    assert caught.value is failure and state.permit is permit

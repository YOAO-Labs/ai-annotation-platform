"""Source identity, publication transactions and concurrent archive regression tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import gzip
import hashlib
from itertools import count
from tempfile import TemporaryFile
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob, AsyncJobKind, AsyncJobStatus
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.services import audit_archive_storage as codec
from app.services import audit_partition_service as service
from app.services.audit_archive_partitions import (
    AuditArchiveCancelled,
    AuditArchiveError,
    SourcePartition,
    build_archive,
    catalog,
    parse_bounds,
    partition_name,
    retention_cutoff,
    stream_source,
    verify_source,
)
from app.services.audit_partition_service import AuditPartitionService
from app.services.jobs_lifecycle_data import job_lock_statement


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "12", None])
def test_retention_requires_strict_positive_integer(value):
    with pytest.raises(AuditArchiveError, match="audit_archive_invalid_retention"):
        retention_cutoff(date(2026, 1, 19), value)


def test_retention_uses_complete_calendar_months():
    assert retention_cutoff(date(2026, 1, 19), 12) == date(2025, 1, 1)
    assert retention_cutoff(date(2026, 1, 19), 10**100) == date.min


@pytest.mark.parametrize(
    ("name", "bound"),
    [
        ("audit_logs_y2024m02", "DEFAULT"),
        (
            "renamed",
            "FOR VALUES FROM ('2024-02-01 00:00:00+00') TO ('2024-03-01 00:00:00+00')",
        ),
        (
            "audit_logs_y2024m02",
            "FOR VALUES FROM ('2024-02-01 00:00:00') TO ('2024-03-01 00:00:00')",
        ),
        (
            "audit_logs_y2024m02",
            "FOR VALUES FROM ('2024-02-01 00:00:00+08') TO ('2024-03-01 00:00:00+08')",
        ),
        (
            "audit_logs_y2024m02",
            "FOR VALUES FROM ('2024-02-02 00:00:00+00') TO ('2024-03-01 00:00:00+00')",
        ),
        (
            "audit_logs_y2024m02",
            "FOR VALUES FROM ('2024-02-01 00:00:00+00') TO ('2024-04-01 00:00:00+00')",
        ),
        (
            "audit_logs_y2024m02",
            "FOR VALUES FROM ('2024-02-01 00:00:00+00') TO ('2024-03-01 00:00:00.000001+00')",
        ),
        (
            "audit_logs_y2024m03",
            "FOR VALUES FROM ('2024-02-01 00:00:00+00') TO ('2024-03-01 00:00:00+00')",
        ),
        (
            "audit_logs_y1999m01",
            "FOR VALUES FROM ('1999-01-01 00:00:00+00') TO ('1999-02-01 00:00:00+00')",
        ),
    ],
)
def test_bounds_reject_non_month_sources(name, bound):
    with pytest.raises(AuditArchiveError, match="audit_archive_source_invalid"):
        parse_bounds(name, bound)


def test_bounds_accept_complete_utc_leap_month():
    assert parse_bounds(
        "audit_logs_y2024m02",
        "FOR VALUES FROM ('2024-02-01 00:00:00+00') TO ('2024-03-01 00:00:00+00')",
    ) == (date(2024, 2, 1), date(2024, 3, 1))


_months = count()


@pytest.fixture
async def source_case(test_engine, monkeypatch):
    """Own only fresh committed test partitions in the verified disposable DB.

    Permanent output facts intentionally survive individual tests; the owner of
    the disposable database removes that database after the complete test run.
    """
    index = next(_months)
    start = date(2000 + index // 12, index % 12 + 1, 1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    name = partition_name(start)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    job_id = uuid4()
    async with maker() as db:
        assert (
            await db.scalar(
                text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}
            )
            is None
        )
        await db.execute(
            text(
                f'CREATE TABLE public."{name}" PARTITION OF public.audit_logs '
                f"FOR VALUES FROM ('{start} 00:00:00+00') TO ('{end} 00:00:00+00')"
            )
        )
        await db.execute(
            text(
                f'INSERT INTO public."{name}" (action, created_at) VALUES '
                "('test.archive.source', :created_at)"
            ),
            {"created_at": datetime.combine(start, datetime.min.time(), timezone.utc)},
        )
        db.add(
            AsyncJob(
                id=job_id,
                kind=AsyncJobKind.AUDIT_ARCHIVE.value,
                status=AsyncJobStatus.RUNNING.value,
                payload={},
                result={},
            )
        )
        await db.commit()
        source = next(row for row in await catalog(db) if row.name == name)
        await db.rollback()
    case = SimpleNamespace(maker=maker, job_id=job_id, source=source, writes=[])

    def write(receipt, file, **kwargs):
        assert not file.closed
        data = file.read()
        assert hashlib.sha256(data).hexdigest() == receipt.sha256
        assert len(gzip.decompress(data).splitlines()) == receipt.row_count + 1
        file.seek(0)
        case.writes.append(receipt)

    monkeypatch.setattr(codec, "write", write)
    try:
        yield case
    finally:
        async with maker() as db:
            # Both names belong only to the partition created above.
            for owned in (name, f"{name}_renamed"):
                await db.execute(text(f'DROP TABLE IF EXISTS public."{owned}"'))
            await db.execute(
                text("DELETE FROM async_jobs WHERE id = :id"), {"id": job_id}
            )
            await db.commit()


@asynccontextmanager
async def _paused_write(case, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    file_handle = []
    normal = codec.write

    def write(receipt, file, **kwargs):
        file_handle.append(file)
        entered.set()
        if not release.wait(15):
            raise TimeoutError("test write was not released")
        normal(receipt, file, **kwargs)

    monkeypatch.setattr(codec, "write", write)
    async with case.maker() as db:
        task = asyncio.create_task(
            AuditPartitionService.archive_old_partitions(db, job_id=case.job_id)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10), (
                "archive did not reach physical I/O"
            )
            yield task, release, file_handle
        finally:
            release.set()
            if not task.done():
                task.cancel()
            try:
                await task
            except (AuditArchiveError, asyncio.CancelledError):
                pass


async def _facts(case):
    async with case.maker() as db:
        output = (
            await db.scalars(
                select(AuditArchiveOutput).where(
                    AuditArchiveOutput.job_id == case.job_id
                )
            )
        ).all()
        job = await db.get(AsyncJob, case.job_id)
        exists = await db.scalar(
            text("SELECT to_regclass(:name)"), {"name": f"public.{case.source.name}"}
        )
        return output, job, exists


async def test_archive_publication_commits_drop_owner_audit_and_job(source_case):
    case = source_case
    async with case.maker() as db:
        result = await AuditPartitionService.archive_old_partitions(
            db, job_id=case.job_id
        )
    output, job, exists = await _facts(case)
    assert exists is None
    assert len(output) == len(case.writes) == 1
    assert output[0].state == "ready" and output[0].published_at is not None
    assert job.result == result
    assert result["archived_partitions"] == [case.source.name]
    assert result["total_rows"] == 1
    async with case.maker() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM audit_logs WHERE action = 'audit.archive' "
                    "AND detail_json->>'archive_output_id' = :id"
                ),
                {"id": str(output[0].id)},
            )
            == 1
        )


async def test_pending_is_committed_and_parent_is_unlocked_during_io(
    source_case, monkeypatch
):
    case = source_case
    async with _paused_write(case, monkeypatch) as (task, release, _):
        output, job, exists = await _facts(case)
        assert exists is not None and output[0].state == "pending"
        assert job.result == {}
        async with case.maker() as observer:
            await observer.execute(
                text(
                    "LOCK TABLE ONLY public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT"
                )
            )
            await observer.rollback()
        release.set()
        await task


async def test_source_insert_after_dump_keeps_pending_and_online_rows(
    source_case, monkeypatch
):
    case = source_case
    async with _paused_write(case, monkeypatch) as (task, release, _):
        async with case.maker() as writer:
            await writer.execute(
                text(
                    f'INSERT INTO public."{case.source.name}" (action,created_at) '
                    "VALUES ('test.concurrent', :at)"
                ),
                {
                    "at": datetime.combine(
                        case.source.month_start, datetime.min.time(), timezone.utc
                    )
                },
            )
            await writer.commit()
        release.set()
        with pytest.raises(AuditArchiveError, match="audit_archive_source_changed"):
            await task
    output, job, exists = await _facts(case)
    assert exists is not None and output[0].state == "pending" and job.result == {}


@pytest.mark.parametrize("mutation", ["rename", "recreate"])
async def test_source_identity_change_during_io_never_drops_replacement(
    source_case, monkeypatch, mutation
):
    case = source_case
    source = case.source
    async with _paused_write(case, monkeypatch) as (task, release, _):
        async with case.maker() as owner:
            if mutation == "rename":
                await owner.execute(
                    text(
                        f'ALTER TABLE {source.qualified} RENAME TO "{source.name}_renamed"'
                    )
                )
            else:
                await owner.execute(text(f"DROP TABLE {source.qualified}"))
                await owner.execute(
                    text(
                        f"CREATE TABLE {source.qualified} PARTITION OF public.audit_logs "
                        f"FOR VALUES FROM ('{source.month_start} 00:00:00+00') "
                        f"TO ('{source.month_end} 00:00:00+00')"
                    )
                )
            await owner.commit()
        release.set()
        with pytest.raises(AuditArchiveError):
            await task
    output, job, exists = await _facts(case)
    assert output[0].state == "pending" and job.result == {}
    if mutation == "recreate":
        assert exists is not None


async def test_committed_cancellation_during_io_keeps_pending(source_case, monkeypatch):
    case = source_case
    async with _paused_write(case, monkeypatch) as (task, release, _):
        async with case.maker() as cancellation:
            job = await cancellation.scalar(
                job_lock_statement(case.job_id, mode="update", populate_existing=True)
            )
            job.status = AsyncJobStatus.CANCELLED.value
            await cancellation.commit()
        release.set()
        with pytest.raises(AuditArchiveCancelled):
            await task
    output, job, exists = await _facts(case)
    assert exists is not None and output[0].state == "pending" and job.result == {}


async def test_repeated_task_cancellation_drains_write_before_file_close(
    source_case, monkeypatch
):
    case = source_case
    async with _paused_write(case, monkeypatch) as (task, release, handles):
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not handles[0].closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handles[0].closed
    output, job, exists = await _facts(case)
    assert exists is not None and output[0].state == "pending" and job.result == {}


async def test_ready_month_conflict_preserves_recreated_source(source_case):
    case = source_case
    source = case.source
    async with case.maker() as db:
        await AuditPartitionService.archive_old_partitions(db, job_id=case.job_id)
    async with case.maker() as db:
        await db.execute(
            text(
                f"CREATE TABLE {source.qualified} PARTITION OF public.audit_logs "
                f"FOR VALUES FROM ('{source.month_start} 00:00:00+00') "
                f"TO ('{source.month_end} 00:00:00+00')"
            )
        )
        await db.commit()
    async with case.maker() as db:
        with pytest.raises(
            AuditArchiveError, match="audit_archive_month_already_ready"
        ):
            await AuditPartitionService.archive_old_partitions(db, job_id=case.job_id)
    output, _, exists = await _facts(case)
    assert exists is not None and len(output) == 1 and output[0].state == "ready"
    assert len(case.writes) == 1


async def test_stream_receipt_matches_deterministic_gzip_and_source(source_case):
    case = source_case
    source = case.source
    async with case.maker() as db:
        await db.execute(
            text(f"LOCK TABLE ONLY {source.qualified} IN SHARE MODE NOWAIT")
        )
        observed = await stream_source(db, source)
        output = SimpleNamespace(
            id=uuid4(),
            job_id=case.job_id,
            year=source.year,
            month=source.month,
            source_schema="public",
            source_table=source.name,
            parent_oid=source.parent_oid,
            source_oid=source.source_oid,
            month_start=source.month_start,
            month_end=source.month_end,
            format_revision=1,
        )
        with TemporaryFile(mode="w+b") as first, TemporaryFile(mode="w+b") as second:
            await build_archive(db, source, output, first)
            first_bytes = first.read()
            await build_archive(db, source, output, second)
            assert first_bytes == second.read()
            assert output.source_sha256 == observed.sha256
            assert output.source_size_bytes == observed.size_bytes
            assert output.row_count == observed.row_count == 1
            assert output.sha256 == hashlib.sha256(first_bytes).hexdigest()
        await db.rollback()


async def test_existing_transaction_cannot_archive_actual_source(source_case):
    case = source_case
    async with case.maker() as db:
        await db.execute(text("SELECT 1"))
        with pytest.raises(
            AuditArchiveError, match="audit_archive_transaction_required"
        ):
            await AuditPartitionService.archive_old_partitions(db, job_id=case.job_id)
        assert db.in_transaction()
        await db.rollback()
    output, _, exists = await _facts(case)
    assert exists is not None and output == [] and case.writes == []


async def test_verify_rejects_same_name_different_oid(source_case):
    case = source_case
    async with case.maker() as db:
        changed = SourcePartition(
            case.source.name,
            case.source.parent_oid,
            case.source.source_oid + 1,
            case.source.month_start,
            case.source.month_end,
        )
        with pytest.raises(AuditArchiveError, match="audit_archive_source_changed"):
            await verify_source(db, changed)
        await db.rollback()


@pytest.mark.parametrize(
    ("commit_number", "after_commit", "expected_state", "source_exists", "write_count"),
    [
        (2, False, None, True, 0),
        (2, True, "pending", True, 0),
        (4, False, "pending", True, 1),
        (4, True, "ready", False, 1),
    ],
)
async def test_commit_failures_never_guess_or_remove_ownership(
    source_case,
    monkeypatch,
    commit_number,
    after_commit,
    expected_state,
    source_exists,
    write_count,
):
    case = source_case
    async with case.maker() as db:
        commit = db.commit
        commits = 0

        async def uncertain_commit():
            nonlocal commits
            commits += 1
            if commits == commit_number and not after_commit:
                raise RuntimeError("private injected database details")
            await commit()
            if commits == commit_number:
                raise RuntimeError("private injected acknowledgement loss")

        monkeypatch.setattr(db, "commit", uncertain_commit)
        with pytest.raises(AuditArchiveError, match="^audit_archive_unavailable$"):
            await AuditPartitionService.archive_old_partitions(db, job_id=case.job_id)
    output, job, exists = await _facts(case)
    assert (exists is not None) is source_exists
    assert len(case.writes) == write_count
    if expected_state is None:
        assert output == []
    else:
        assert len(output) == 1 and output[0].state == expected_state
    assert bool(job.result.get("output_ids")) is (expected_state == "ready")


async def test_parent_routed_insert_never_deadlocks_final_lock_upgrade(
    source_case, monkeypatch
):
    case = source_case
    hashed = asyncio.Event()
    proceed = asyncio.Event()
    original = service.stream_source

    async def before_upgrade(*args, **kwargs):
        receipt = await original(*args, **kwargs)
        hashed.set()
        await proceed.wait()
        return receipt

    monkeypatch.setattr(service, "stream_source", before_upgrade)
    async with (
        case.maker() as archive,
        case.maker() as writer,
        case.maker() as observer,
    ):
        archive_task = asyncio.create_task(
            AuditPartitionService.archive_old_partitions(archive, job_id=case.job_id)
        )
        writer_task = None
        try:
            await asyncio.wait_for(hashed.wait(), 10)
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))

            async def insert():
                await writer.execute(
                    text(
                        "INSERT INTO public.audit_logs (action,created_at) "
                        "VALUES ('test.parent_routed', :at)"
                    ),
                    {
                        "at": datetime.combine(
                            case.source.month_start, datetime.min.time(), timezone.utc
                        )
                    },
                )
                await writer.commit()

            writer_task = asyncio.create_task(insert())
            for _ in range(200):
                holds_parent = await observer.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks "
                        "WHERE pid=:pid AND relation='public.audit_logs'::regclass "
                        "AND mode='RowExclusiveLock' AND granted)"
                    ),
                    {"pid": writer_pid},
                )
                if holds_parent:
                    break
                await asyncio.sleep(0.01)
            assert holds_parent and not writer_task.done()
            await observer.rollback()
            proceed.set()
            with pytest.raises(AuditArchiveError, match="audit_archive_busy"):
                await asyncio.wait_for(archive_task, 3)
            await asyncio.wait_for(writer_task, 3)
        finally:
            proceed.set()
            for task in (archive_task, writer_task):
                if task is not None and not task.done():
                    task.cancel()
                if task is not None:
                    try:
                        await task
                    except (AuditArchiveError, asyncio.CancelledError):
                        pass
    output, job, exists = await _facts(case)
    assert exists is not None and output[0].state == "pending" and job.result == {}


async def test_multiple_workers_publish_at_most_one_ready_month(
    source_case, monkeypatch
):
    case = source_case
    other_job_id = uuid4()
    normal_write = codec.write
    async with case.maker() as db:
        db.add(
            AsyncJob(
                id=other_job_id,
                kind=AsyncJobKind.AUDIT_ARCHIVE.value,
                status=AsyncJobStatus.RUNNING.value,
                payload={},
                result={},
            )
        )
        await db.commit()
    try:
        async with _paused_write(case, monkeypatch) as (first_task, release, _):
            paused = codec.write

            def write(receipt, file, **kwargs):
                if receipt.job_id == case.job_id:
                    paused(receipt, file, **kwargs)
                else:
                    normal_write(receipt, file)

            monkeypatch.setattr(codec, "write", write)
            async with case.maker() as second:
                result = await AuditPartitionService.archive_old_partitions(
                    second, job_id=other_job_id
                )
            assert result["archived_partitions"] == [case.source.name]
            release.set()
            with pytest.raises(
                AuditArchiveError, match="audit_archive_month_already_ready"
            ):
                await first_task
        async with case.maker() as db:
            outputs = (
                await db.scalars(
                    select(AuditArchiveOutput).where(
                        AuditArchiveOutput.year == case.source.year,
                        AuditArchiveOutput.month == case.source.month,
                    )
                )
            ).all()
            assert sorted(row.state for row in outputs) == ["pending", "ready"]
    finally:
        async with case.maker() as db:
            await db.execute(
                text("DELETE FROM async_jobs WHERE id=:id"), {"id": other_job_id}
            )
            await db.commit()


async def test_same_size_source_mutation_is_detected_by_full_sha(
    source_case, monkeypatch
):
    case = source_case
    async with _paused_write(case, monkeypatch) as (task, release, _):
        async with case.maker() as owner:
            # Exercise historical row changes (for example a restored source),
            # retaining the exact row count and canonical byte length.
            await owner.execute(
                text(f"ALTER TABLE {case.source.qualified} DISABLE TRIGGER USER")
            )
            await owner.execute(
                text(
                    f"UPDATE ONLY {case.source.qualified} "
                    "SET action='test.archive.change'"
                )
            )
            await owner.execute(
                text(f"ALTER TABLE {case.source.qualified} ENABLE TRIGGER USER")
            )
            observed = await stream_source(owner, case.source)
            await owner.commit()
        output, _, _ = await _facts(case)
        assert observed.row_count == output[0].row_count
        assert observed.size_bytes == output[0].source_size_bytes
        assert observed.sha256 != output[0].source_sha256
        release.set()
        with pytest.raises(AuditArchiveError, match="audit_archive_source_changed"):
            await task
    output, _, exists = await _facts(case)
    assert exists is not None and output[0].state == "pending"


@pytest.mark.parametrize("shape", ["partial_month", "default", "subpartition"])
async def test_catalog_rejects_invalid_direct_child_shapes(db_session, shape):
    if shape == "default":
        ddl = "CREATE TABLE public.audit_logs_y2082m01 PARTITION OF public.audit_logs DEFAULT"
    elif shape == "partial_month":
        ddl = (
            "CREATE TABLE public.audit_logs_y2082m01 PARTITION OF public.audit_logs "
            "FOR VALUES FROM ('2082-01-02 00:00:00+00') TO ('2082-02-01 00:00:00+00')"
        )
    else:
        ddl = (
            "CREATE TABLE public.audit_logs_y2082m01 PARTITION OF public.audit_logs "
            "FOR VALUES FROM ('2082-01-01 00:00:00+00') TO ('2082-02-01 00:00:00+00') "
            "PARTITION BY HASH (id)"
        )
    await db_session.execute(text(ddl))
    with pytest.raises(AuditArchiveError, match="audit_archive_source_invalid"):
        await catalog(db_session)


@pytest.mark.parametrize("limit", ["MAX_ROWS", "MAX_SOURCE_BYTES"])
async def test_stream_limits_close_cursor_without_loading_partition(monkeypatch, limit):
    source = SourcePartition(
        "audit_logs_y2000m01", 1, 2, date(2000, 1, 1), date(2000, 2, 1)
    )
    row = {
        "id": 1,
        "actor_id": None,
        "actor_email": None,
        "actor_role": None,
        "action": "test",
        "target_type": None,
        "target_id": None,
        "method": None,
        "path": None,
        "status_code": None,
        "ip": None,
        "detail_json": None,
        "request_id": None,
        "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
    }

    class DB:
        closed = False
        seen = 0
        name = None

        async def execute(self, statement):
            sql = str(statement)
            if sql.startswith("DECLARE "):
                assert "ORDER BY id, created_at" in sql
                self.name = sql.split('"')[1]
            elif sql.startswith("FETCH "):
                assert sql == f'FETCH FORWARD 1 FROM "{self.name}"'
                self.seen += 1
                return SimpleNamespace(
                    mappings=lambda: SimpleNamespace(one_or_none=lambda: row)
                )
            else:
                assert sql == f'CLOSE "{self.name}"'
                self.closed = True

    db = DB()
    monkeypatch.setattr(codec, limit, 1)
    with pytest.raises(AuditArchiveError, match="audit_archive_source_limit"):
        await stream_source(db, source)
    assert db.closed and db.seen <= 2


async def test_stream_deadline_covers_database_wait(monkeypatch):
    source = SourcePartition(
        "audit_logs_y2000m01", 1, 2, date(2000, 1, 1), date(2000, 2, 1)
    )

    class DB:
        async def execute(self, statement):
            await asyncio.sleep(10)
            pytest.fail("database wait should have been cancelled")

    monkeypatch.setattr(codec, "VERIFY_SECONDS", 0.001)
    with pytest.raises(AuditArchiveError, match="audit_archive_verification_timeout"):
        await stream_source(DB(), source)


async def test_cancellation_can_commit_during_local_dump(source_case, monkeypatch):
    case = source_case
    dumped = asyncio.Event()
    proceed = asyncio.Event()
    normal = service.build_archive

    async def pause_dump(*args):
        await normal(*args)
        dumped.set()
        await proceed.wait()

    monkeypatch.setattr(service, "build_archive", pause_dump)
    async with case.maker() as db:
        task = asyncio.create_task(
            AuditPartitionService.archive_old_partitions(db, job_id=case.job_id)
        )
        try:
            await asyncio.wait_for(dumped.wait(), 10)
            async with case.maker() as cancellation:
                job = await asyncio.wait_for(
                    cancellation.scalar(
                        job_lock_statement(
                            case.job_id, mode="update", populate_existing=True
                        )
                    ),
                    2,
                )
                job.status = AsyncJobStatus.CANCELLED.value
                await cancellation.commit()
            proceed.set()
            with pytest.raises(AuditArchiveCancelled):
                await task
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
            try:
                await task
            except (AuditArchiveError, asyncio.CancelledError):
                pass
    output, job, exists = await _facts(case)
    assert (
        exists is not None and output == [] and case.writes == [] and job.result == {}
    )


async def test_later_month_failure_preserves_committed_partial_job_result(
    source_case, monkeypatch
):
    case = source_case
    index = next(_months)
    start = date(2000 + index // 12, index % 12 + 1, 1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    name = partition_name(start)
    async with case.maker() as db:
        await db.execute(
            text(
                f'CREATE TABLE public."{name}" PARTITION OF public.audit_logs '
                f"FOR VALUES FROM ('{start} 00:00:00+00') TO ('{end} 00:00:00+00')"
            )
        )
        await db.execute(
            text(
                f'INSERT INTO public."{name}" (action,created_at) '
                "VALUES ('test.archive.second', :at)"
            ),
            {"at": datetime.combine(start, datetime.min.time(), timezone.utc)},
        )
        await db.commit()
    normal = codec.write

    def write(receipt, file, **kwargs):
        if receipt.source_table == name:
            raise codec.AuditArchiveStorageError(
                "audit_archive_storage_unavailable", status_code=503
            )
        normal(receipt, file, **kwargs)

    monkeypatch.setattr(codec, "write", write)
    try:
        async with case.maker() as db:
            with pytest.raises(
                AuditArchiveError, match="audit_archive_storage_unavailable"
            ):
                await AuditPartitionService.archive_old_partitions(
                    db, job_id=case.job_id
                )
        output, job, exists = await _facts(case)
        assert exists is None and sorted(row.state for row in output) == [
            "pending",
            "ready",
        ]
        assert job.result["archived_partitions"] == [case.source.name]
        assert job.result["total_rows"] == 1 and len(job.result["output_ids"]) == 1
        assert job.progress_pct == 50
    finally:
        async with case.maker() as db:
            await db.execute(text(f'DROP TABLE IF EXISTS public."{name}"'))
            await db.commit()


async def test_stream_closes_server_portal_before_drop_and_preserves_other_cursor(
    source_case,
):
    case = source_case
    unrelated = f"test_unrelated_{uuid4().hex}"
    async with case.maker() as db:
        await db.execute(
            text(f"LOCK TABLE ONLY {case.source.qualified} IN SHARE MODE NOWAIT")
        )
        await db.execute(text(f'DECLARE "{unrelated}" NO SCROLL CURSOR FOR SELECT 1'))
        try:
            observed = await stream_source(db, case.source)
            assert observed.row_count == 1
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_cursors WHERE name LIKE 'audit_archive_%'"
                    )
                )
                == 0
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM pg_cursors WHERE name=:name"),
                    {"name": unrelated},
                )
                == 1
            )
            # Reproduces PostgreSQL 55006 if any archive portal remains active.
            await db.execute(text(f"DROP TABLE {case.source.qualified}"))
            assert await db.scalar(text(f'FETCH FORWARD 1 FROM "{unrelated}"')) == 1
        finally:
            # Rollback also closes our unrelated test cursor and restores the
            # fixture's source; no ownership or archive was created.
            await db.rollback()


async def test_stream_repeated_cancellation_drains_its_explicit_cursor_close():
    source = SourcePartition(
        "audit_logs_y2000m01", 1, 2, date(2000, 1, 1), date(2000, 2, 1)
    )
    fetching = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    class DB:
        name = None
        closed = False

        async def execute(self, statement):
            sql = str(statement)
            if sql.startswith("DECLARE "):
                self.name = sql.split('"')[1]
            elif sql.startswith("FETCH "):
                fetching.set()
                await asyncio.Event().wait()
            else:
                assert sql == f'CLOSE "{self.name}"'
                closing.set()
                await release.wait()
                self.closed = True

    db = DB()
    task = asyncio.create_task(stream_source(db, source))
    try:
        await asyncio.wait_for(fetching.wait(), 1)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not db.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.closed
    finally:
        release.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

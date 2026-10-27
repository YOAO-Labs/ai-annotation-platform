"""Recover independent archive facts without reusing historical source OIDs."""

from datetime import date, datetime, timezone
import gzip
import hashlib
import io
import uuid


def identifier(kind, state):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aap:recovery-audit-archive:{kind}:{state}")


def table_for(state):
    return "audit_logs_y2002m01" if state == "ready" else "audit_logs_y2002m02"


def content(row, rows):
    from app.services import audit_archive_storage as storage

    source = b"".join(storage.canonical_line(value) for value in rows)
    row.row_count = len(rows)
    row.source_size_bytes = len(source)
    row.source_sha256 = hashlib.sha256(source).hexdigest()
    target = io.BytesIO()
    with gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0) as writer:
        writer.write(source)
        writer.write(storage.encode_meta(row))
    result = target.getvalue()
    row.sha256 = hashlib.sha256(result).hexdigest()
    row.size_bytes = len(result)
    return result


async def seed_audit_archives(db, *, storage, now):
    from sqlalchemy import text
    from app.db.models.audit_archive_output import AuditArchiveOutput
    from app.services import audit_archive_storage

    parent_oid = await db.scalar(text("SELECT 'public.audit_logs'::regclass::oid"))
    for state, month in (("ready", 1), ("pending", 2)):
        table = table_for(state)
        start, end = date(2002, month, 1), date(2002, month + 1, 1)
        await db.execute(
            text(
                f'CREATE TABLE public."{table}" PARTITION OF public.audit_logs '
                f"FOR VALUES FROM ('{start.isoformat()} 00:00:00+00') "
                f"TO ('{end.isoformat()} 00:00:00+00')"
            )
        )
        await db.execute(
            text(
                f'INSERT INTO public."{table}" (id, action, created_at, detail_json) '
                "VALUES (:id, :action, :created, :detail)"
            ),
            {
                "id": 2002000 + month,
                "action": "recovery.archive.synthetic",
                "created": datetime(2002, month, 1, tzinfo=timezone.utc),
                "detail": '{"evidence":"offline-only"}',
            },
        )
        rows = list(
            (
                await db.execute(
                    text(f'SELECT * FROM ONLY public."{table}" ORDER BY id,created_at')
                )
            )
            .mappings()
            .all()
        )
        source_oid = await db.scalar(
            text("SELECT to_regclass(:table)::oid"), {"table": "public." + table}
        )
        row = AuditArchiveOutput(
            id=identifier("output", state),
            job_id=identifier("absent-job", state),
            year=2002,
            month=month,
            source_schema="public",
            source_table=table,
            parent_oid=parent_oid,
            source_oid=source_oid,
            month_start=start,
            month_end=end,
            format_revision=1,
            created_at=now,
            state=state,
            published_at=now if state == "ready" else None,
        )
        raw = content(row, rows)
        row.storage_ref = await audit_archive_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        owner = audit_archive_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType="application/gzip",
        )
        if state == "ready":
            await db.execute(text(f'DROP TABLE public."{table}"'))
    legacy = gzip.compress(b'{"legacy":"unknown-ownership"}\n', mtime=0)
    storage.client.put_object(
        Bucket=storage.audit_archive_bucket,
        Key="2001/12.jsonl.gz",
        Body=legacy,
        ContentType="application/gzip",
    )


async def verify_audit_archives(db, *, storage):
    from sqlalchemy import select, text
    from app.db.models.async_job import AsyncJob
    from app.db.models.audit_archive_output import AuditArchiveOutput
    from app.services import audit_archive_storage

    actual_parent_oid = await db.scalar(
        text("SELECT 'public.audit_logs'::regclass::oid")
    )
    for state in ("ready", "pending"):
        row = await db.get(AuditArchiveOutput, identifier("output", state))
        assert row is not None and row.state == state
        assert row.parent_oid != actual_parent_oid
        assert row.published_at == (row.created_at if state == "ready" else None)
        assert await db.get(AsyncJob, row.job_id) is None
        owner = audit_archive_storage.resolve(row)
        assert owner.bucket == storage.audit_archive_bucket
        page = audit_archive_storage.read(row, limit=100, offset=0)
        assert page["total"] == 2 and len(page["items"]) == 2
        assert page["items"][0]["action"] == "recovery.archive.synthetic"
        assert page["items"][-1] == audit_archive_storage.metadata(row)
        actual_oid = await db.scalar(
            text("SELECT to_regclass(:table)::oid"),
            {"table": "public." + row.source_table},
        )
        if state == "ready":
            assert actual_oid is None
        else:
            assert actual_oid is not None and actual_oid != row.source_oid
            rows = list(
                (
                    await db.execute(
                        text(
                            f'SELECT * FROM ONLY public."{table_for(state)}" '
                            "ORDER BY id,created_at"
                        )
                    )
                )
                .mappings()
                .all()
            )
            source = b"".join(
                audit_archive_storage.canonical_line(value) for value in rows
            )
            assert row.source_sha256 == hashlib.sha256(source).hexdigest()
            assert row.source_size_bytes == len(source)
    assert (
        await db.scalar(
            select(AuditArchiveOutput.id).where(
                AuditArchiveOutput.year == 2001, AuditArchiveOutput.month == 12
            )
        )
        is None
    )
    body = storage.client.get_object(
        Bucket=storage.audit_archive_bucket, Key="2001/12.jsonl.gz"
    )["Body"]
    try:
        assert body.read(1024) == gzip.compress(
            b'{"legacy":"unknown-ownership"}\n', mtime=0
        )
    finally:
        body.close()
    return {
        "pinned_audit_archive_outputs": 2,
        "verified_audit_archive_files": 2,
        "ready_audit_sources_absent": 1,
        "pending_audit_sources_retained": 1,
        "unowned_legacy_audit_archive_files": 1,
    }

"""Strict PostgreSQL source identities and bounded canonical archive streams."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import re
from time import monotonic
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_archive_storage as archive_storage
from app.services import storage_access


class AuditArchiveError(Exception):
    """A public classification that never contains database or object payloads."""

    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class AuditArchiveCancelled(AuditArchiveError):
    def __init__(self):
        super().__init__("audit_archive_cancelled")


@dataclass(frozen=True)
class SourcePartition:
    name: str
    parent_oid: int
    source_oid: int
    month_start: date
    month_end: date

    def __post_init__(self):
        try:
            valid = (
                type(self.month_start) is date
                and type(self.month_end) is date
                and self.month_start.year >= 2000
                and self.month_start.day == 1
                and self.name == partition_name(self.month_start)
                and self.month_end == next_month(self.month_start)
            )
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            raise AuditArchiveError("audit_archive_source_invalid")
        _oid(self.parent_oid)
        _oid(self.source_oid)

    @property
    def qualified(self) -> str:
        # Construction is restricted to validated, canonical catalog names.
        return f'public."{self.name}"'

    @property
    def year(self) -> int:
        return self.month_start.year

    @property
    def month(self) -> int:
        return self.month_start.month


@dataclass(frozen=True)
class SourceReceipt:
    row_count: int
    size_bytes: int
    sha256: str


_BOUND = re.compile(r"FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)")
_NAME = re.compile(r"audit_logs_y([0-9]{4})m([0-9]{2})")
_MONTH_MUTEX_NAMESPACE = 727278


def next_month(value: date) -> date:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def partition_name(value: date) -> str:
    return f"audit_logs_y{value.year:04d}m{value.month:02d}"


def retention_cutoff(today: date, retain_months: int) -> date:
    if type(retain_months) is not int or retain_months <= 0:
        raise AuditArchiveError("audit_archive_invalid_retention", 422)
    month_index = (today.year - 1) * 12 + today.month - 1 - retain_months
    if month_index < 0:
        return date.min
    return date(month_index // 12 + 1, month_index % 12 + 1, 1)


def parse_bounds(name: str, bound: str) -> tuple[date, date]:
    """Accept exactly one complete UTC calendar month with its canonical name."""
    match = _BOUND.fullmatch(bound)
    if not _NAME.fullmatch(name) or match is None:
        raise AuditArchiveError("audit_archive_source_invalid")
    try:
        start, end = (datetime.fromisoformat(part) for part in match.groups())
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() != timezone.utc.utcoffset(start)
            or end.utcoffset() != timezone.utc.utcoffset(end)
            or start.hour
            or start.minute
            or start.second
            or start.microsecond
            or end.hour
            or end.minute
            or end.second
            or end.microsecond
            or start.day != 1
            or start.date().year < 2000
            or end.date() != next_month(start.date())
            or name != partition_name(start.date())
        ):
            raise ValueError
    except (OverflowError, ValueError):
        raise AuditArchiveError("audit_archive_source_invalid") from None
    return start.date(), end.date()


def _oid(value) -> int:
    if type(value) is not int or not 0 < value < 2**32:
        raise AuditArchiveError("audit_archive_source_invalid")
    return value


async def catalog(db: AsyncSession) -> list[SourcePartition]:
    """Reject malformed children rather than silently omitting online sources."""
    await db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    parent = (
        (
            await db.execute(
                text(
                    """
                    SELECT c.oid::bigint AS parent_oid, c.relkind::text AS kind,
                           c.relpersistence::text AS persistence,
                           p.partstrat::text AS strategy, p.partnatts AS key_count,
                           p.partattrs::text AS key_attributes,
                           a.attnum::text AS created_at_attribute,
                           a.atttypid = 'timestamptz'::regtype AS timestamp_key
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    LEFT JOIN pg_partitioned_table p ON p.partrelid = c.oid
                    LEFT JOIN pg_attribute a ON a.attrelid = c.oid
                        AND a.attname = 'created_at' AND NOT a.attisdropped
                    WHERE n.nspname = 'public' AND c.relname = 'audit_logs'
                    """
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        parent is None
        or parent["kind"] != "p"
        or parent["persistence"] != "p"
        or parent["strategy"] != "r"
        or parent["key_count"] != 1
        or parent["key_attributes"] != parent["created_at_attribute"]
        or parent["timestamp_key"] is not True
    ):
        raise AuditArchiveError("audit_archive_source_invalid")
    parent_oid = _oid(parent["parent_oid"])
    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT c.oid::bigint AS source_oid, c.relname AS name,
                           n.nspname AS schema, c.relkind::text AS kind,
                           c.relpersistence::text AS persistence, c.relispartition,
                           i.inhdetachpending AS detach_pending,
                           pg_get_expr(c.relpartbound, c.oid) AS bound,
                           EXISTS (SELECT 1 FROM pg_inherits s
                                   WHERE s.inhparent = c.oid) AS has_children,
                           (SELECT count(*) FROM pg_inherits s
                            WHERE s.inhrelid = c.oid) AS parent_count
                    FROM pg_inherits i
                    JOIN pg_class c ON c.oid = i.inhrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE i.inhparent = :parent_oid
                    """
                ),
                {"parent_oid": parent_oid},
            )
        )
        .mappings()
        .all()
    )
    result = []
    for row in rows:
        if (
            row["schema"] != "public"
            or row["kind"] != "r"
            or row["persistence"] != "p"
            or row["relispartition"] is not True
            or row["detach_pending"] is not False
            or row["has_children"] is not False
            or row["parent_count"] != 1
        ):
            raise AuditArchiveError("audit_archive_source_invalid")
        start, end = parse_bounds(row["name"], row["bound"] or "")
        result.append(
            SourcePartition(
                row["name"], parent_oid, _oid(row["source_oid"]), start, end
            )
        )
    return sorted(result, key=lambda source: source.month_start)


async def verify_source(db: AsyncSession, expected: SourcePartition) -> None:
    current = next(
        (source for source in await catalog(db) if source.name == expected.name), None
    )
    if current != expected:
        raise AuditArchiveError("audit_archive_source_changed")


async def month_mutex(db: AsyncSession, month_start: date) -> None:
    acquired = await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:namespace, :month)"),
        {
            "namespace": _MONTH_MUTEX_NAMESPACE,
            "month": month_start.year * 12 + month_start.month,
        },
    )
    if acquired is not True:
        raise AuditArchiveError("audit_archive_busy")


async def lock_source(db: AsyncSession, source: SourcePartition) -> None:
    await db.execute(text(f"LOCK TABLE ONLY {source.qualified} IN SHARE MODE NOWAIT"))
    # Name resolution and the lock must both identify the originally selected OID.
    await verify_source(db, source)


def _deadline(deadline: float) -> None:
    if monotonic() > deadline:
        raise AuditArchiveError("audit_archive_verification_timeout", 503)
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


async def _stream_source(
    db: AsyncSession,
    source: SourcePartition,
    *,
    compressed=None,
    deadline: float | None = None,
    expected_rows: int | None = None,
) -> SourceReceipt:
    """Hash ordered rows without materializing the partition or an old snapshot."""
    if deadline is None:
        deadline = monotonic() + archive_storage.VERIFY_SECONDS
    digest = hashlib.sha256()
    row_count = size_bytes = 0
    # SQLAlchemy's asyncpg result.close() only drops its client cursor reference;
    # the server portal can still prevent DROP in this very transaction. Own a
    # uniquely named SQL cursor and explicitly close precisely that portal.
    name = f"audit_archive_{uuid4().hex}"
    declared = failed = False
    try:
        await db.execute(
            text(
                f'DECLARE "{name}" NO SCROLL CURSOR FOR '
                f"SELECT * FROM ONLY {source.qualified} ORDER BY id, created_at"
            )
        )
        declared = True
        while True:
            _deadline(deadline)
            # Fetch one bounded row, even when it approaches the 16 MiB limit.
            # PostgreSQL advances the portal without rescanning a keyset index.
            row = (
                (await db.execute(text(f'FETCH FORWARD 1 FROM "{name}"')))
                .mappings()
                .one_or_none()
            )
            if row is None:
                break
            _deadline(deadline)
            line = archive_storage.canonical_line(row)
            row_count += 1
            size_bytes += len(line)
            if (
                len(line) > archive_storage.MAX_ROW_BYTES
                or row_count > archive_storage.MAX_ROWS
                or size_bytes > archive_storage.MAX_SOURCE_BYTES
            ):
                raise AuditArchiveError("audit_archive_source_limit", 422)
            digest.update(line)
            if compressed is not None:
                compressed.write(line)
                if compressed.fileobj.tell() > archive_storage.MAX_BYTES:
                    raise AuditArchiveError("audit_archive_compressed_limit", 422)
    except BaseException:
        failed = True
        raise
    finally:
        if declared:
            try:
                await storage_access.finish(
                    asyncio.create_task(db.execute(text(f'CLOSE "{name}"')))
                )
            except BaseException:
                if not failed:
                    raise
                # Preserve the original error/cancellation. The archive owner's
                # mandatory rollback also destroys a portal in an aborted tx.
    _deadline(deadline)
    if expected_rows is not None and row_count != expected_rows:
        raise AuditArchiveError("audit_archive_source_changed")
    return SourceReceipt(row_count, size_bytes, digest.hexdigest())


async def stream_source(
    db: AsyncSession,
    source: SourcePartition,
    *,
    compressed=None,
    deadline: float | None = None,
    expected_rows: int | None = None,
) -> SourceReceipt:
    if deadline is None:
        deadline = monotonic() + archive_storage.VERIFY_SECONDS
    try:
        async with asyncio.timeout(max(0, deadline - monotonic())):
            return await _stream_source(
                db,
                source,
                compressed=compressed,
                deadline=deadline,
                expected_rows=expected_rows,
            )
    except TimeoutError:
        raise AuditArchiveError("audit_archive_verification_timeout", 503) from None


async def build_archive(
    db: AsyncSession, source: SourcePartition, output, file: BinaryIO
) -> None:
    """Finalize immutable source and compressed receipts before ownership commits."""
    deadline = monotonic() + archive_storage.VERIFY_SECONDS
    file.seek(0)
    file.truncate(0)
    with gzip.GzipFile(fileobj=file, mode="wb", mtime=0, filename="") as compressed:
        receipt = await stream_source(
            db, source, compressed=compressed, deadline=deadline
        )
        output.row_count = receipt.row_count
        output.source_size_bytes = receipt.size_bytes
        output.source_sha256 = receipt.sha256
        compressed.write(archive_storage.encode_meta(output))
    output.size_bytes = file.tell()
    if output.size_bytes > archive_storage.MAX_BYTES:
        raise AuditArchiveError("audit_archive_compressed_limit", 422)
    digest = hashlib.sha256()
    file.seek(0)
    while chunk := file.read(1024 * 1024):
        _deadline(deadline)
        digest.update(chunk)
    _deadline(deadline)
    output.sha256 = digest.hexdigest()
    file.seek(0)

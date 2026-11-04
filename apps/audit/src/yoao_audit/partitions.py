"""Audit partition maintenance and cold archive behavior."""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from typing import Any

from .maintenance_contract import AuditPartition

logger = logging.getLogger(__name__)

ArchiveRecorder = Callable[[AuditPartition, int, str], Awaitable[None]]


def _next_month(value: date) -> date:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _month_floor(value: date) -> date:
    return value.replace(day=1)


def _partition_name(value: date) -> str:
    return f"audit_logs_y{value.year}m{value.month:02d}"


class AuditPartitionManager:
    def __init__(self, data, storage, *, record_archive: ArchiveRecorder | None = None):
        self.data = data
        self.storage = storage
        self.record_archive = record_archive

    async def ensure_future_partitions(self, months_ahead: int = 3) -> list[str]:
        """Ensure the current month through ``months_ahead`` inclusive exist."""
        existing = {
            partition.name for partition in await self._data().list_partitions()
        }
        current = _month_floor(datetime.now(timezone.utc).date())
        target = current
        for _ in range(months_ahead):
            target = _next_month(target)
        end = _next_month(target)

        created: list[str] = []
        month = current
        while month < end:
            name = _partition_name(month)
            if name not in existing and await self._data().ensure_partition(month):
                created.append(name)
            month = _next_month(month)
        return created

    async def archive_old_partitions(self, retain_months: int = 12) -> dict[str, Any]:
        """Archive and drop partitions older than the configured month cutoff."""
        storage = self._storage()
        cutoff = _month_floor(datetime.now(timezone.utc).date())
        for _ in range(retain_months):
            if cutoff.month == 1:
                cutoff = cutoff.replace(year=cutoff.year - 1, month=12)
            else:
                cutoff = cutoff.replace(month=cutoff.month - 1)

        archived: list[str] = []
        archive_keys: list[str] = []
        total_rows = 0

        partitions = sorted(
            await self._data().list_partitions(), key=lambda part: part.range_from
        )
        for partition in partitions:
            if partition.range_from >= cutoff:
                continue

            rows = await self._data().snapshot_partition(partition)
            row_count = len(rows)
            body = gzip.compress(
                ("\n".join(_serialize_row(row) for row in rows) + "\n").encode("utf-8")
            )
            month = partition.range_from
            object_key = f"{month.year}/{month.month:02d}.jsonl.gz"
            try:
                storage.ensure_bucket(storage.audit_archive_bucket)
                storage.client.put_object(
                    Bucket=storage.audit_archive_bucket,
                    Key=object_key,
                    Body=body,
                    ContentType="application/gzip",
                    Metadata={
                        "partition": partition.name,
                        "row_count": str(row_count),
                        "archived_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as error:
                logger.error("archive %s failed: %s", partition.name, error)
                continue

            await self._data().drop_partition(partition)
            await self._record_archive(partition, row_count, object_key)
            archived.append(partition.name)
            archive_keys.append(object_key)
            total_rows += row_count

        return {
            "archived_partitions": archived,
            "archive_keys": archive_keys,
            "total_rows": total_rows,
        }

    def list_archives(self) -> list[dict[str, Any]]:
        """List archived months from the existing object-store bucket."""
        storage = self._storage()
        bucket = storage.audit_archive_bucket
        archives: list[dict[str, Any]] = []
        for obj in storage.list_objects("", bucket=bucket):
            key = obj["key"]
            if key.endswith("/") or not key.endswith(".jsonl.gz"):
                continue
            try:
                year_str, filename = key.split("/", 1)
                month_str = filename.removesuffix(".jsonl.gz")
                year, month = int(year_str), int(month_str)
            except (ValueError, IndexError):
                logger.warning("skip non-archive key: %s", key)
                continue
            last_modified = obj.get("last_modified")
            archives.append(
                {
                    "year": year,
                    "month": month,
                    "object_key": key,
                    "size_bytes": int(obj["size"]),
                    "archived_at": (
                        last_modified.isoformat() if last_modified else None
                    ),
                }
            )
        archives.sort(key=lambda item: (item["year"], item["month"]), reverse=True)
        return archives

    def read_archive_rows(self, year: int, month: int) -> list[dict] | None:
        """Read the existing gzip JSON-lines object for a month, if present."""
        storage = self._storage()
        bucket = storage.audit_archive_bucket
        object_key = f"{year}/{month:02d}.jsonl.gz"
        try:
            response = storage.client.get_object(Bucket=bucket, Key=object_key)
            raw = response["Body"].read()
        except Exception as error:
            no_such_key = getattr(
                getattr(storage.client, "exceptions", None), "NoSuchKey", None
            )
            if no_such_key is not None and isinstance(error, no_such_key):
                return None
            error_response = getattr(error, "response", None) or {}
            if error_response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

        text_data = gzip.decompress(raw).decode("utf-8")
        rows: list[dict] = []
        for line in text_data.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    async def refresh_daily(self) -> None:
        await self._data().refresh_daily()

    def _data(self):
        if self.data is None:
            raise RuntimeError("Audit maintenance data is required for this operation")
        return self.data

    def _storage(self):
        if self.storage is None:
            raise RuntimeError("Audit archive storage is required for this operation")
        return self.storage

    async def _record_archive(
        self, partition: AuditPartition, row_count: int, object_key: str
    ) -> None:
        if self.record_archive is not None:
            await self.record_archive(partition, row_count, object_key)
        else:
            await self._data().record_archive_event(partition, row_count, object_key)


def _serialize_row(row: dict) -> str:
    """Serialize the original archive row format, including DB-native values."""

    def default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "hex"):
            return str(value)
        return str(value)

    return json.dumps(row, default=default, ensure_ascii=False)

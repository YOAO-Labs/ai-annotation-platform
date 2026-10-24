from __future__ import annotations

from datetime import date, datetime, timezone
import gzip
import json
from pathlib import Path

import pytest

from aap_audit.maintenance_contract import (
    AuditPartition,
    audit_maintenance_schema,
)
from aap_audit.partitions import AuditPartitionManager


def _previous_month(value: date) -> date:
    return (
        value.replace(year=value.year - 1, month=12)
        if value.month == 1
        else value.replace(month=value.month - 1)
    )


class _Data:
    def __init__(self, partitions: list[AuditPartition], events: list[tuple]):
        self.partitions = partitions
        self.events = events
        self.dropped: list[str] = []

    async def list_partitions(self):
        return self.partitions

    async def snapshot_partition(self, partition):
        self.events.append(("snapshot", partition.name))
        return [{"id": 9223372036854775807, "action": "audit.test"}]

    async def drop_partition(self, partition):
        self.events.append(("drop", partition.name))
        self.dropped.append(partition.name)


class _Client:
    def __init__(self, events: list[tuple], fail_key: str):
        self.events = events
        self.fail_key = fail_key
        self.objects = {}

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        self.events.append(("upload", key))
        if key == self.fail_key:
            raise RuntimeError("object store unavailable")
        self.objects[key] = kwargs["Body"]


class _Storage:
    audit_archive_bucket = "audit-archive"

    def __init__(self, events: list[tuple], fail_key: str):
        self.events = events
        self.client = _Client(events, fail_key)

    def ensure_bucket(self, bucket):
        self.events.append(("ensure_bucket", bucket))


@pytest.mark.asyncio
async def test_archive_failure_keeps_partition_and_success_preserves_native_json():
    current = datetime.now(timezone.utc).date().replace(day=1)
    older = _previous_month(_previous_month(current))
    newer = _previous_month(current)

    def partition(month: date, oid: int) -> AuditPartition:
        end = (
            date(month.year + 1, 1, 1)
            if month.month == 12
            else date(month.year, month.month + 1, 1)
        )
        return AuditPartition(
            name=f"audit_logs_y{month.year}m{month.month:02d}",
            range_from=month,
            range_to=end,
            relation_oid=oid,
        )

    events: list[tuple] = []
    partitions = [partition(older, 1001), partition(newer, 1002)]
    data = _Data(partitions, events)
    storage = _Storage(events, f"{older.year}/{older.month:02d}.jsonl.gz")

    async def record_archive(partition, row_count, object_key):
        events.append(("record", partition.name, row_count, object_key))

    result = await AuditPartitionManager(
        data, storage, record_archive=record_archive
    ).archive_old_partitions(retain_months=0)

    assert result == {
        "archived_partitions": [partitions[1].name],
        "archive_keys": [f"{newer.year}/{newer.month:02d}.jsonl.gz"],
        "total_rows": 1,
    }
    assert data.dropped == [partitions[1].name]
    assert events.index(("drop", partitions[1].name)) < events.index(
        ("record", partitions[1].name, 1, result["archive_keys"][0])
    )
    archived_row = json.loads(
        gzip.decompress(storage.client.objects[result["archive_keys"][0]]).decode()
    )
    assert archived_row["id"] == 9223372036854775807


def test_maintenance_schema_artifact_matches_closed_contract():
    artifact = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src/aap_audit/maintenance.schema.json"
        ).read_text()
    )
    assert artifact == audit_maintenance_schema()

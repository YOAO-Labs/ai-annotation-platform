from __future__ import annotations

import gzip
import io

import pytest

from aap_audit.maintenance_config import (
    AuditArchiveS3Config,
    ConfigurationError,
)
from app.config import settings
from app.services import audit_partition_service


def _configure_archive_read(monkeypatch, **values: str) -> None:
    defaults = {
        "endpoint_url": "",
        "region": "us-east-1",
        "bucket": "audit-archive",
        "access_key_id": "",
        "access_key_id_file": "",
        "secret_access_key": "",
        "secret_access_key_file": "",
    }
    defaults.update(values)
    for name, value in defaults.items():
        monkeypatch.setattr(settings, f"audit_archive_read_s3_{name}", value)


def test_dedicated_archive_read_binding_closes_each_client(monkeypatch, tmp_path):
    key_file = tmp_path / "access-key"
    key_file.write_text("archive-reader", encoding="utf-8")
    _configure_archive_read(
        monkeypatch,
        endpoint_url="https://archive.example.test",
        access_key_id_file=str(key_file),
        secret_access_key="private-secret",
    )

    class FakeClient:
        fail = False

        def get_object(self, **_kwargs):
            if self.fail:
                raise RuntimeError("archive read failed")
            return {"Body": io.BytesIO(gzip.compress(b'{"action":"login"}\n'))}

    instances = []

    class FakeStorage:
        audit_archive_bucket = "audit-archive"

        def __init__(self):
            self.closed = False
            self.client = FakeClient()
            instances.append(self)

        def list_objects(self, prefix, *, bucket):
            assert prefix == ""
            assert bucket == self.audit_archive_bucket
            return [
                {
                    "key": "2026/10.jsonl.gz",
                    "size": 32,
                    "last_modified": None,
                }
            ]

        def close(self):
            self.closed = True

    def make_storage(config):
        assert config.endpoint_url == "https://archive.example.test"
        assert config.access_key_id == "archive-reader"
        assert config.secret_access_key == "private-secret"
        return FakeStorage()

    monkeypatch.setattr(AuditArchiveS3Config, "archive_storage", make_storage)
    monkeypatch.setattr(audit_partition_service, "storage_service", None)

    assert (
        audit_partition_service.AuditPartitionService.list_archives()[0]["object_key"]
        == "2026/10.jsonl.gz"
    )
    assert audit_partition_service.AuditPartitionService.read_archive_rows(
        2026, 10
    ) == [{"action": "login"}]
    FakeClient.fail = True
    with pytest.raises(RuntimeError, match="archive read failed"):
        audit_partition_service.AuditPartitionService.read_archive_rows(2026, 10)
    assert len(instances) == 3
    assert all(storage.closed for storage in instances)

    _configure_archive_read(monkeypatch)

    class LegacyStorage:
        audit_archive_bucket = "legacy-archive"

        def list_objects(self, prefix, *, bucket):
            assert prefix == ""
            assert bucket == self.audit_archive_bucket
            return []

    monkeypatch.setattr(audit_partition_service, "storage_service", LegacyStorage())
    assert audit_partition_service.AuditPartitionService.list_archives() == []


def test_partial_dedicated_credentials_fail_without_fallback(
    monkeypatch,
):
    secret = "must-not-appear-in-error"
    _configure_archive_read(
        monkeypatch,
        endpoint_url="https://archive.example.test",
        access_key_id=secret,
    )
    monkeypatch.setattr(audit_partition_service, "storage_service", None)
    monkeypatch.setattr(
        AuditArchiveS3Config,
        "archive_storage",
        lambda _config: pytest.fail("invalid configuration must not create a client"),
    )

    with pytest.raises(ConfigurationError) as error:
        audit_partition_service.AuditPartitionService.list_archives()

    assert secret not in str(error.value)

from unittest.mock import Mock, call

import pytest

from app.config import Settings
from app.services.storage import StorageService


def test_inputs_bucket_is_distinct_from_existing_storage_namespaces():
    with pytest.raises(ValueError, match="dedicated bucket"):
        Settings(_env_file=None, minio_inputs_bucket="media-cache")


def test_startup_enables_versioning_only_for_the_inputs_bucket():
    storage = object.__new__(StorageService)
    storage.bucket = "annotations"
    storage.datasets_bucket = "datasets"
    storage.bug_reports_bucket = "bug-reports"
    storage.media_cache_bucket = "media-cache"
    storage.audit_archive_bucket = "audit-archive"
    storage.import_bucket = "import"
    storage.export_bucket = "export"
    storage.avatars_bucket = "avatars"
    storage.training_bucket = "training"
    storage.inputs_bucket = "accepted-inputs"
    storage.client = Mock()
    storage.client.get_bucket_versioning.side_effect = [
        {},
        {"Status": "Enabled"},
    ]
    storage.ensure_bucket = Mock()
    storage._ensure_lifecycle = Mock()

    storage.ensure_all_buckets()

    assert storage.ensure_bucket.call_args_list == [
        call(name)
        for name in (
            "annotations",
            "datasets",
            "bug-reports",
            "media-cache",
            "audit-archive",
            "import",
            "export",
            "avatars",
            "training",
            "accepted-inputs",
        )
    ]
    assert storage.client.get_bucket_versioning.call_args_list == [
        call(Bucket="accepted-inputs"),
        call(Bucket="accepted-inputs"),
    ]
    storage.client.put_bucket_versioning.assert_called_once_with(
        Bucket="accepted-inputs",
        VersioningConfiguration={"Status": "Enabled"},
    )
    storage._ensure_lifecycle.assert_called_once_with()


def test_startup_fails_if_inputs_versioning_cannot_be_enabled():
    storage = object.__new__(StorageService)
    storage.inputs_bucket = "accepted-inputs"
    storage.client = Mock()
    storage.client.get_bucket_versioning.return_value = {}

    with pytest.raises(RuntimeError, match="MINIO_INPUTS_BUCKET"):
        storage.ensure_inputs_bucket_versioning()

    storage.client.put_bucket_versioning.assert_called_once_with(
        Bucket="accepted-inputs",
        VersioningConfiguration={"Status": "Enabled"},
    )

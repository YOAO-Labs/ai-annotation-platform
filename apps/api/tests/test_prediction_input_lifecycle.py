"""Bucket expiry must not bypass durable prediction input ownership."""

from copy import deepcopy
import logging
from unittest.mock import Mock

from botocore.exceptions import ClientError
import pytest

from app.services.storage import StorageService


def storage(rules):
    service = StorageService.__new__(StorageService)
    service.client = Mock()
    service.import_bucket = "owned-import"
    service.export_bucket = "owned-export"
    service.media_cache_bucket = "owned-media"
    service.client.get_bucket_lifecycle_configuration.return_value = {
        "Rules": deepcopy(rules)
    }
    return service


def rule(identifier, prefix=""):
    return {
        "ID": identifier,
        "Status": "Enabled",
        "Filter": {"Prefix": prefix},
        "Expiration": {"Days": 7},
    }


def test_owner_cleanup_replaces_platform_import_expiry_without_reinstalling():
    service = storage([rule("import-artifacts-7d")])
    service._ensure_cache_lifecycle()
    service.client.delete_bucket_lifecycle.assert_called_once_with(
        Bucket=service.import_bucket
    )
    configurations = {
        call.kwargs["Bucket"]: call.kwargs["LifecycleConfiguration"]["Rules"]
        for call in service.client.put_bucket_lifecycle_configuration.call_args_list
    }
    assert service.import_bucket not in configurations
    assert configurations[service.export_bucket] == [rule("export-artifacts-7d")]


def test_import_expiry_removal_preserves_operator_rules_exactly():
    operator = rule("operator-owned", "temporary-legacy/")
    service = storage([rule("import-artifacts-7d"), operator])
    service._remove_import_artifact_lifecycle()
    service.client.put_bucket_lifecycle_configuration.assert_called_once_with(
        Bucket=service.import_bucket, LifecycleConfiguration={"Rules": [operator]}
    )
    service.client.delete_bucket_lifecycle.assert_not_called()


@pytest.mark.parametrize("rules", [[], [rule("operator-owned")]])
def test_absent_platform_import_expiry_is_a_zero_write_operation(rules):
    service = storage(rules)
    service._remove_import_artifact_lifecycle()
    service.client.put_bucket_lifecycle_configuration.assert_not_called()
    service.client.delete_bucket_lifecycle.assert_not_called()


@pytest.mark.parametrize("code", ["NoSuchLifecycleConfiguration", "AccessDenied"])
def test_import_expiry_removal_reports_real_failures_and_tolerates_absence(
    caplog, code, monkeypatch
):
    service = storage([])
    service.client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {"Error": {"Code": code}}, "GetBucketLifecycleConfiguration"
    )
    # Alembic's logging configuration can disable already imported loggers in
    # a combined database suite; this test owns the warning capture itself.
    monkeypatch.setattr(logging.getLogger("app.services.storage"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="app.services.storage"):
        service._remove_import_artifact_lifecycle()
    assert ("Failed to remove import artifact lifecycle" in caplog.text) == (
        code == "AccessDenied"
    )
    service.client.put_bucket_lifecycle_configuration.assert_not_called()
    service.client.delete_bucket_lifecycle.assert_not_called()

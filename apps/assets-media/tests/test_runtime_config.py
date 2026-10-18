from __future__ import annotations

from pathlib import Path

import pytest

from aap_assets_media_runtime.config import (
    ConfigurationError,
    load_config,
    load_storage_config,
)


def _storage_environment() -> dict[str, str]:
    return {
        "ASSETS_MEDIA_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": "https://media.example.test",
        "ASSETS_MEDIA_S3_REGION": "us-east-1",
        "ASSETS_MEDIA_DATASETS_BUCKET": "datasets",
        "ASSETS_MEDIA_CACHE_BUCKET": "media-cache",
        "ASSETS_MEDIA_S3_ACCESS_KEY": "media-worker",
        "ASSETS_MEDIA_S3_SECRET_KEY": "media-secret",
    }


def test_storage_config_needs_only_explicit_storage_settings():
    config = load_storage_config(_storage_environment())
    assert config.endpoint == "http://minio.internal:9000"
    assert config.public_endpoint == "https://media.example.test"
    assert config.datasets_bucket == "datasets"
    assert config.cache_bucket == "media-cache"
    assert config.region == "us-east-1"


def test_worker_config_uses_its_separate_database_login():
    environment = _storage_environment()
    environment["ASSETS_MEDIA_WORKER_DATABASE_URL"] = (
        "postgresql://asset_media_worker:secret@postgres/annotation"
    )
    config = load_config(environment)
    assert "asset_media_worker" in config.database_url
    assert config.poll_seconds == 5
    assert config.execution.lease_seconds == 1200
    assert config.execution.frame_ttl_days == 14
    assert config.execution.chunk_ttl_days == 30


def test_secret_source_is_exclusive_and_empty_values_fail(tmp_path: Path):
    secret = tmp_path / "s3-secret"
    secret.write_text("file-secret\n", encoding="utf-8")
    environment = _storage_environment()
    environment["ASSETS_MEDIA_S3_SECRET_KEY_FILE"] = str(secret)
    with pytest.raises(ConfigurationError, match="Set only one"):
        load_storage_config(environment)

    del environment["ASSETS_MEDIA_S3_SECRET_KEY_FILE"]
    environment["ASSETS_MEDIA_S3_SECRET_KEY"] = ""
    with pytest.raises(ConfigurationError, match="empty"):
        load_storage_config(environment)


def test_storage_config_rejects_a_public_endpoint_path():
    environment = _storage_environment()
    environment["ASSETS_MEDIA_S3_PUBLIC_ENDPOINT"] = "https://media.example.test/prefix"
    with pytest.raises(ConfigurationError, match=r"HTTP\(S\) origin"):
        load_storage_config(environment)


def test_worker_database_url_is_required_and_secrets_are_not_in_repr():
    with pytest.raises(ConfigurationError, match="WORKER_DATABASE_URL"):
        load_config(_storage_environment())

    environment = _storage_environment()
    environment["ASSETS_MEDIA_WORKER_DATABASE_URL"] = (
        "postgresql://asset_media_worker:db-secret@postgres/annotation"
    )
    config = load_config(environment)
    assert "db-secret" not in repr(config)
    assert "media-secret" not in repr(config)


@pytest.mark.parametrize("lease_seconds", [1, 29, 3601])
def test_worker_lease_rejects_values_outside_native_bounds(lease_seconds: int):
    environment = _storage_environment()
    environment["ASSETS_MEDIA_WORKER_DATABASE_URL"] = (
        "postgresql://asset_media_worker:secret@postgres/annotation"
    )
    environment["ASSETS_MEDIA_LEASE_SECONDS"] = str(lease_seconds)
    with pytest.raises(ConfigurationError, match="LEASE"):
        load_config(environment)

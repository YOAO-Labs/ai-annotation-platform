from __future__ import annotations

import pytest

from aap_audit.maintenance_config import ConfigurationError, load_config


def test_database_secret_file_is_read_and_never_shown(tmp_path):
    secret = "postgresql://audit:private@db/aap"
    secret_file = tmp_path / "database-url"
    secret_file.write_text(secret, encoding="utf-8")

    config = load_config(
        {
            "AUDIT_MAINTENANCE_DATABASE_URL_FILE": str(secret_file),
            "AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL": "not a URL",
        }
    )

    assert config.database_url == secret
    assert secret not in repr(config)
    assert config.archive_s3_endpoint_url is None
    with pytest.raises(ConfigurationError, match="missing dedicated Audit archive"):
        config.archive_storage()
    archive_config = load_config(
        {
            "AUDIT_MAINTENANCE_DATABASE_URL": secret,
            "AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL": "http://minio:9000",
            "AUDIT_MAINTENANCE_ARCHIVE_S3_ACCESS_KEY_ID": "archive-user",
            "AUDIT_MAINTENANCE_ARCHIVE_S3_SECRET_ACCESS_KEY": "archive-secret",
        },
        require_archive_storage=True,
    )
    assert archive_config.archive_s3_bucket == "audit-archive"
    assert "archive-secret" not in repr(archive_config)
    with pytest.raises(ConfigurationError, match="set only one"):
        load_config(
            {
                "AUDIT_MAINTENANCE_DATABASE_URL": secret,
                "AUDIT_MAINTENANCE_DATABASE_URL_FILE": str(secret_file),
            }
        )

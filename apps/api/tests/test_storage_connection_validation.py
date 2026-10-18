"""Portable storage-connection validation contracts used by API consumers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_assets_source.connectors.management import (
    ConnectorValidationError,
    public_config,
    validate_config,
    validate_secret,
)
from app.schemas.storage_connection import ConnectorAllowlistUpdate


def test_allowlist_entries_are_required_but_empty_override_is_valid():
    assert ConnectorAllowlistUpdate(entries=[]).entries == []
    with pytest.raises(ValidationError):
        ConnectorAllowlistUpdate()


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        (
            "s3",
            {
                "endpoint": "https://s3.example.test",
                "bucket": "data",
                "vendor_extension": True,
            },
        ),
        (
            "sftp",
            {"host": "sftp.example.test", "username": "svc", "timeout": 5},
        ),
    ],
)
def test_portable_config_rejects_unknown_keys_instead_of_silently_dropping(
    kind, config
):
    with pytest.raises(ConnectorValidationError, match="不支持配置项"):
        validate_config(kind, config)


def test_portable_config_rejects_s3_userinfo_and_public_config_redacts_legacy_values():
    with pytest.raises(ConnectorValidationError, match="不能包含用户名或密码"):
        validate_config(
            "s3",
            {
                "endpoint": "https://alice:secret@example.test:9000/api",
                "bucket": "data",
            },
        )

    original = {
        "endpoint": "https://alice:secret@example.test:9000/api?region=x",
        "bucket": "data",
        "unknown_internal": "never return",
    }
    public = public_config("s3", original)
    assert public == {
        "endpoint": "https://example.test:9000/api?region=x",
        "bucket": "data",
    }
    assert original["endpoint"] == "https://alice:secret@example.test:9000/api?region=x"

    assert (
        public_config(
            "s3", {"endpoint": "https://alice:secret@[invalid", "bucket": "data"}
        )["endpoint"]
        == "[invalid endpoint]"
    )


def test_portable_sftp_secret_is_exclusive_and_infers_legacy_auth_mode():
    config = {"host": "sftp.example.test", "username": "svc"}
    assert validate_secret("sftp", {"private_key": "key"}, config) == "key"
    assert validate_secret("sftp", {"password": "password"}, config) == "password"

    for secret in (
        {},
        {"password": "password", "private_key": "key"},
        {"password": 123},
        {"private_key": ""},
    ):
        with pytest.raises(ConnectorValidationError):
            validate_secret("sftp", secret, config)

    with pytest.raises(ConnectorValidationError, match="凭据类型一致"):
        validate_secret(
            "sftp",
            {"private_key": "key"},
            {**config, "auth_type": "password"},
        )


def test_portable_s3_secret_requirements_are_unchanged():
    validate_secret(
        "s3",
        {"access_key": "AK", "secret_key": "SK"},
        {"endpoint": "https://s3.example.test", "bucket": "data"},
    )
    with pytest.raises(ConnectorValidationError, match="access_key 与 secret_key"):
        validate_secret(
            "s3",
            {"access_key": "AK"},
            {"endpoint": "https://s3.example.test", "bucket": "data"},
        )

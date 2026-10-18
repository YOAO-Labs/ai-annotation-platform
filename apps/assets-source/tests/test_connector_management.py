from __future__ import annotations

import pytest

from aap_assets_source.connectors.management import (
    ConnectorValidationError,
    public_config,
    validate_config,
    validate_secret,
)


@pytest.mark.parametrize(
    ("kind", "config"),
    [
        (
            "s3",
            {"endpoint": "https://s3.example.test", "bucket": "data", "extra": True},
        ),
        ("sftp", {"host": "sftp.example.test", "username": "svc", "timeout": 5}),
    ],
)
def test_validate_config_rejects_unknown_fields(kind: str, config: dict) -> None:
    with pytest.raises(ConnectorValidationError, match="不支持配置项"):
        validate_config(kind, config)


def test_validate_config_preserves_required_fields_and_s3_userinfo_rejection() -> None:
    with pytest.raises(ConnectorValidationError, match="缺少必填配置"):
        validate_config("sftp", None)
    with pytest.raises(ConnectorValidationError, match="不能包含用户名或密码"):
        validate_config(
            "s3",
            {
                "endpoint": "https://alice:secret@example.test:9000/api",
                "bucket": "data",
            },
        )


def test_public_config_redacts_legacy_s3_userinfo_without_mutating_source() -> None:
    source = {
        "endpoint": "https://alice:secret@example.test:9000/api?region=x",
        "bucket": "data",
        "access_key": "legacy-secret",
        "password": "legacy-password",
        "vendor_extension": True,
    }

    result = public_config("s3", source)

    assert result == {
        "endpoint": "https://example.test:9000/api?region=x",
        "bucket": "data",
    }
    assert source["endpoint"] == "https://alice:secret@example.test:9000/api?region=x"
    assert public_config("sftp", {"host": "sftp.example.test"}) == {
        "host": "sftp.example.test"
    }
    assert public_config("s3", {"endpoint": "https://alice:secret@[invalid"}) == {
        "endpoint": "[invalid endpoint]"
    }


def test_validate_secret_preserves_sftp_exclusivity_and_config_only_inference() -> None:
    legacy_config = validate_config(
        "sftp", {"host": "sftp.example.test", "username": "svc"}
    )
    inferred = validate_secret("sftp", {"private_key": "legacy-key"}, legacy_config)
    assert inferred == "key"

    password_config = {"host": "sftp.example.test", "username": "svc"}
    assert (
        validate_secret("sftp", {"password": "secret"}, password_config) == "password"
    )
    assert (
        validate_secret(
            "sftp", {"private_key": "key", "passphrase": "passphrase"}, legacy_config
        )
        == "key"
    )

    for secret in (
        None,
        {},
        {"password": "password", "private_key": "key"},
        {"private_key": ""},
        {"password": 123},
    ):
        with pytest.raises(ConnectorValidationError):
            validate_secret("sftp", secret)

    with pytest.raises(ConnectorValidationError, match="凭据类型一致"):
        validate_secret("sftp", {"private_key": "key"}, {"auth_type": "password"})


def test_validate_secret_keeps_s3_requirements_and_auth_is_not_inferred() -> None:
    assert (
        validate_secret(
            "s3",
            {"access_key": "AK", "secret_key": "SK", "legacy_extra": "kept"},
        )
        is None
    )
    for secret in (None, {}, {"access_key": "AK", "secret_key": ""}):
        with pytest.raises(ConnectorValidationError, match="access_key 与 secret_key"):
            validate_secret("s3", secret)

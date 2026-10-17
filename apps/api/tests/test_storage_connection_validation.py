"""Pure storage-connection contract checks without DB or network access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.storage_connection import ConnectorAllowlistUpdate
from app.services import storage_connection


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
def test_config_rejects_unknown_keys_instead_of_silently_dropping(kind, config):
    with pytest.raises(
        storage_connection.ConnectorValidationError, match="不支持配置项"
    ):
        storage_connection._validate_and_sanitize_config(kind, config)


def test_s3_endpoint_rejects_userinfo_and_legacy_readback_redacts_without_mutation():
    with pytest.raises(
        storage_connection.ConnectorValidationError, match="不能包含用户名或密码"
    ):
        storage_connection._validate_and_sanitize_config(
            "s3",
            {
                "endpoint": "https://alice:secret@example.test:9000/api",
                "bucket": "data",
            },
        )

    original_config = {
        "endpoint": "https://alice:secret@example.test:9000/api?region=x",
        "bucket": "data",
    }
    connection = SimpleNamespace(
        id="connection-id",
        name="Legacy",
        kind="s3",
        config=original_config,
        scope="owner",
        project_id=None,
        secret_enc=b"encrypted",
        created_by="creator-id",
        created_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
    )

    result = storage_connection.to_out_dict(connection)

    assert result["config"]["endpoint"] == "https://example.test:9000/api?region=x"
    assert (
        original_config["endpoint"]
        == "https://alice:secret@example.test:9000/api?region=x"
    )
    connection.config = {"endpoint": "https://alice:secret@[invalid", "bucket": "data"}
    assert (
        "secret" not in storage_connection.to_out_dict(connection)["config"]["endpoint"]
    )
    for endpoint in (None, 123, ""):
        with pytest.raises(storage_connection.ConnectorValidationError):
            storage_connection._validate_and_sanitize_config(
                "s3", {"endpoint": endpoint, "bucket": "data"}
            )


def test_sftp_secret_must_be_exclusive_and_match_auth_type():
    assert (
        storage_connection._validate_secret(
            "sftp", {"private_key": "key"}, {"host": "sftp.example.test"}
        )
        == "key"
    )
    assert (
        storage_connection._validate_secret(
            "sftp", {"password": "password"}, {"auth_type": "password"}
        )
        == "password"
    )
    for secret in (
        {},
        {"password": "password", "private_key": "key"},
        {"password": 123},
        {"private_key": ""},
    ):
        with pytest.raises(storage_connection.ConnectorValidationError):
            storage_connection._validate_secret("sftp", secret, {})
    with pytest.raises(
        storage_connection.ConnectorValidationError, match="凭据类型一致"
    ):
        storage_connection._validate_secret(
            "sftp", {"private_key": "key"}, {"auth_type": "password"}
        )


def test_config_only_update_validates_existing_sftp_secret_and_infers_legacy_mode(
    monkeypatch,
):
    async def allow_target(*_args):
        return None

    async def flush():
        return None

    monkeypatch.setattr(
        storage_connection, "decrypt_secret", lambda _blob: {"private_key": "key"}
    )
    monkeypatch.setattr(
        storage_connection.connector_guard,
        "assert_connection_target_allowed",
        allow_target,
    )
    db = SimpleNamespace(flush=flush)
    original_config = {"host": "sftp.example.test", "username": "svc"}
    connection = SimpleNamespace(
        name="SFTP",
        kind="sftp",
        config=original_config,
        secret_enc=b"encrypted",
    )

    async def mismatch():
        await storage_connection.StorageConnectionService.update(
            db,
            connection,
            name=None,
            config={
                "host": "sftp.example.test",
                "username": "svc",
                "auth_type": "password",
            },
            secret=None,
        )

    with pytest.raises(
        storage_connection.ConnectorValidationError, match="凭据类型一致"
    ):
        asyncio.run(mismatch())
    assert connection.config is original_config

    async def infer_legacy_mode():
        await storage_connection.StorageConnectionService.update(
            db,
            connection,
            name=None,
            config={"host": "sftp.example.test", "username": "svc"},
            secret=None,
        )

    asyncio.run(infer_legacy_mode())
    assert connection.config["auth_type"] == "key"


def test_create_infers_auth_type_for_legacy_key_only_payload(monkeypatch):
    async def allow_target(*_args):
        return None

    class FakeDB:
        connection = None

        def add(self, value):
            self.connection = value

        async def flush(self):
            return None

    monkeypatch.setattr(
        storage_connection.connector_guard,
        "assert_connection_target_allowed",
        allow_target,
    )
    monkeypatch.setattr(
        storage_connection, "encrypt_secret", lambda _secret: b"encrypted"
    )
    db = FakeDB()

    result = asyncio.run(
        storage_connection.StorageConnectionService.create(
            db,
            name="SFTP",
            kind="sftp",
            config={"host": "sftp.example.test", "username": "svc"},
            secret={"private_key": "legacy-key"},
            scope="owner",
            created_by=uuid4(),
        )
    )

    assert result is db.connection
    assert result.config["auth_type"] == "key"

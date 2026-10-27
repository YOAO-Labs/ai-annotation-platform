from __future__ import annotations

import builtins
from types import ModuleType, SimpleNamespace
import sys

import pytest

from aap_assets_source.connectors.host_guard import ConnectorHostDenied
from aap_assets_source.connectors.probe import probe_connection
from aap_assets_source.connectors import sftp as sftp_module


def _install_s3(monkeypatch, *, response=None, error=None):
    class FakeConfig:
        def __init__(self, **kwargs):
            self.options = kwargs

    class FakeClient:
        closed = False
        list_arguments = None

        def list_objects_v2(self, **kwargs):
            self.list_arguments = kwargs
            if error is not None:
                raise error
            return response or {"KeyCount": 0}

        def close(self):
            self.closed = True

    client = FakeClient()
    client_arguments = {}

    def create_client(service, **kwargs):
        client_arguments["service"] = service
        client_arguments.update(kwargs)
        return client

    boto3_module = ModuleType("boto3")
    boto3_module.client = create_client
    botocore_module = ModuleType("botocore")
    botocore_module.__path__ = []
    botocore_config_module = ModuleType("botocore.config")
    botocore_config_module.Config = FakeConfig
    monkeypatch.setitem(sys.modules, "boto3", boto3_module)
    monkeypatch.setitem(sys.modules, "botocore", botocore_module)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config_module)
    return client, client_arguments


def _s3_config() -> dict:
    return {
        "endpoint": "8.8.8.8:9000",
        "bucket": "source-bucket",
        "region": "test-region",
        "base_prefix": "imports/",
        "use_ssl": False,
    }


@pytest.mark.asyncio
async def test_s3_probe_preserves_limits_counts_samples_and_closes_client(monkeypatch):
    client, arguments = _install_s3(monkeypatch, response={"KeyCount": 4})

    result = await probe_connection(
        "s3",
        _s3_config(),
        {"access_key": "access", "secret_key": "secret"},
        allowlist=["8.8.8.0/24"],
    )

    assert result == (True, "连接成功", 4)
    assert arguments["service"] == "s3"
    assert arguments["endpoint_url"] == "http://8.8.8.8:9000"
    assert arguments["aws_access_key_id"] == "access"
    assert arguments["aws_secret_access_key"] == "secret"
    assert arguments["region_name"] == "test-region"
    assert client.list_arguments == {
        "Bucket": "source-bucket",
        "Prefix": "imports/",
        "MaxKeys": 20,
    }
    assert client.closed


@pytest.mark.asyncio
async def test_s3_probe_hides_sdk_error_text_and_closes_client(monkeypatch):
    secret_url = "https://access:secret@storage.example.test/private"
    client, _ = _install_s3(
        monkeypatch,
        error=RuntimeError(f"failed for {secret_url}"),
    )

    result = await probe_connection(
        "s3",
        _s3_config(),
        {"access_key": "access", "secret_key": "secret"},
        allowlist=["8.8.8.0/24"],
    )

    assert result == (False, "连接失败: RuntimeError", None)
    assert "storage.example.test" not in result[1]
    assert "secret" not in result[1]
    assert client.closed


@pytest.mark.asyncio
async def test_s3_probe_never_falls_back_to_ambient_credentials(monkeypatch):
    client, arguments = _install_s3(monkeypatch)
    await probe_connection("s3", _s3_config(), {}, allowlist=["8.8.8.0/24"])
    assert arguments["aws_access_key_id"] == ""
    assert arguments["aws_secret_access_key"] == ""
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(("key_count", "expected_count"), [(25, 20), (-1, 0)])
async def test_s3_probe_clamps_sample_count(monkeypatch, key_count, expected_count):
    client, _ = _install_s3(monkeypatch, response={"KeyCount": key_count})

    result = await probe_connection(
        "s3",
        _s3_config(),
        {"access_key": "access", "secret_key": "secret"},
        allowlist=["8.8.8.0/24"],
    )

    assert result == (True, "连接成功", expected_count)
    assert client.closed


@pytest.mark.asyncio
async def test_probe_exception_class_is_bounded_ascii_identifier(monkeypatch):
    non_identifier_error = type("connector failure", (Exception,), {})
    _install_s3(monkeypatch, error=non_identifier_error("secret URL text"))

    result = await probe_connection(
        "s3",
        _s3_config(),
        {"access_key": "access", "secret_key": "secret"},
        allowlist=["8.8.8.0/24"],
    )

    assert result == (False, "连接失败: Exception", None)


@pytest.mark.asyncio
async def test_host_guard_failure_raises_before_sdk_import(monkeypatch):
    original_import = builtins.__import__

    def reject_connector_sdk(name, *args, **kwargs):
        if name in {"boto3", "botocore.config"}:
            pytest.fail("host guard must run before loading the S3 SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_connector_sdk)

    with pytest.raises(ConnectorHostDenied):
        await probe_connection(
            "s3",
            {"endpoint": "127.0.0.1:9000", "bucket": "source-bucket"},
            {"access_key": "access", "secret_key": "secret"},
            allowlist=["127.0.0.1/32"],
        )


@pytest.mark.parametrize("known_hosts_file", [None, "/etc/ssh/known_hosts"])
@pytest.mark.asyncio
async def test_sftp_probe_pins_hosts_credentials_timeout_and_sample_limit(
    monkeypatch, known_hosts_file
):

    events = []

    class FakeSftp:
        def listdir_iter(self, path, read_aheads=1):
            events.append(("listdir_iter", path, read_aheads))
            return iter([f"entry-{index}" for index in range(25)])

        def get_channel(self):
            return None

        def close(self):
            events.append("sftp.close")

    sftp = FakeSftp()

    class FakeClient:
        def load_host_keys(self, path):
            events.append(("load_host_keys", path))

        def load_system_host_keys(self):
            events.append("load_system_host_keys")

        def set_missing_host_key_policy(self, policy):
            events.append(("policy", policy))

        def connect(self, **kwargs):
            events.append(("connect", kwargs))

        def open_sftp(self):
            return sftp

        def close(self):
            events.append("client.close")

    reject_policy = object()
    monkeypatch.setitem(
        sys.modules,
        "paramiko",
        SimpleNamespace(SSHClient=FakeClient, RejectPolicy=lambda: reject_policy),
    )
    monkeypatch.setattr(
        sftp_module, "load_sftp_private_key", lambda _secret: "parsed-key"
    )

    result = await probe_connection(
        "sftp",
        {
            "host": "8.8.8.8",
            "port": 2222,
            "username": "svc",
            "base_path": "/data/imports",
        },
        {"private_key": "key", "passphrase": "phrase"},
        allowlist=["8.8.8.0/24"],
        known_hosts_file=known_hosts_file,
    )

    assert result == (True, "连接成功", 20)
    assert ("policy", reject_policy) in events
    if known_hosts_file is None:
        assert "load_system_host_keys" in events
        assert not any(
            event[0] == "load_host_keys" for event in events if isinstance(event, tuple)
        )
    else:
        assert ("load_host_keys", known_hosts_file) in events
        assert "load_system_host_keys" not in events
    connect = next(
        event[1]
        for event in events
        if isinstance(event, tuple) and event[0] == "connect"
    )
    # Pinned identity and credential use, not the connector SDK's wire shape.
    assert (connect["hostname"], connect["port"], connect["username"]) == (
        "8.8.8.8",
        2222,
        "svc",
    )
    assert connect["password"] is None
    assert connect["pkey"] == "parsed-key"
    assert connect["allow_agent"] is False
    assert connect["look_for_keys"] is False
    assert events[-2:] == ["sftp.close", "client.close"]


@pytest.mark.asyncio
async def test_sftp_probe_hides_exception_text_and_closes_both_resources(monkeypatch):

    secret_url = "sftp://svc:secret@source.example.test/private"
    events = []

    class FakeSftp:
        def listdir_iter(self, _path, read_aheads=1):
            raise OSError(f"cannot list {secret_url}")

        def get_channel(self):
            return None

        def close(self):
            events.append("sftp.close")

    class FakeClient:
        def load_system_host_keys(self):
            events.append("system-host-keys")

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **_kwargs):
            pass

        def open_sftp(self):
            return FakeSftp()

        def close(self):
            events.append("client.close")

    monkeypatch.setitem(
        sys.modules,
        "paramiko",
        SimpleNamespace(SSHClient=FakeClient, RejectPolicy=lambda: object()),
    )
    monkeypatch.setattr(sftp_module, "load_sftp_private_key", lambda _secret: None)

    result = await probe_connection(
        "sftp",
        {"host": "8.8.8.8", "username": "svc", "base_path": "/data"},
        {"password": "secret"},
        allowlist=["8.8.8.0/24"],
    )

    assert result == (False, "连接失败: OSError", None)
    assert "source.example.test" not in result[1]
    assert "secret" not in result[1]
    assert events[-2:] == ["sftp.close", "client.close"]

from __future__ import annotations

import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    RegistryEnvSyncUnavailable,
    make_registry_env_sync_request,
    registry_env_sync_command_sha256,
    registry_env_sync_status_request,
)

import aap_inference.registry_ingress_manager as manager_module
from aap_inference.registry_ingress_manager import RegistryEnvSyncManager

_REQUEST_ID = UUID("b9894ce1-f23c-4624-b8c7-7316c386f275")
_URL = "http://trusted-backend.test"


class _CommitFailure(Exception):
    pass


class _Connection:
    def __init__(self, *, commit_failure=False):
        self.active = False
        self.commit_failure = commit_failure
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.terminate_count = 0

    def is_in_transaction(self):
        return self.active

    def transaction(self):
        return _Transaction(self)

    async def close(self, *, timeout):
        assert timeout == 2
        self.close_count += 1

    def terminate(self):
        self.terminate_count += 1


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.active = True

    async def __aexit__(self, error_type, _error, _traceback):
        self.connection.active = False
        if error_type is not None:
            self.connection.rollback_count += 1
            return False
        self.connection.commit_count += 1
        if self.connection.commit_failure:
            raise _CommitFailure("synthetic ambiguous commit")
        return False


class _Data:
    def __init__(self, connection, receipt=None):
        assert connection.active is True
        self.receipt = receipt
        self.calls = []

    async def sync_result(self, request_id, command_sha256):
        self.calls.append((request_id, command_sha256))
        if self.receipt is None:
            return None
        return self.receipt


def _request(urls=None):
    return make_registry_env_sync_request(
        [_URL] if urls is None else urls,
        timeout_seconds=4,
        capability_limits=CapabilityLimits(),
        request_id=_REQUEST_ID,
    )


def _result(request, *, state="caller_pending", observed_urls=1):
    return {
        "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
        "request_id": str(request.request_id),
        "command_sha256": registry_env_sync_command_sha256(request),
        "state": state,
        "observed_urls": observed_urls,
    }


def _runtime(monkeypatch, connections, *, receipts=()):
    connect_calls = []
    receipt_values = list(receipts)

    async def connect(*args, **kwargs):
        assert args == ("postgresql://registry-role@localhost/annotation_test",)
        assert kwargs["server_settings"] == {
            "application_name": "aap-inference-registry-ingress"
        }
        connection = connections[len(connect_calls)]
        connect_calls.append((args, kwargs))
        return connection

    bind_calls = []

    async def bind(cls, connection):
        bind_calls.append(connection)
        receipt = receipt_values[len(bind_calls) - 1]
        return _Data(connection, receipt=receipt)

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(
        manager_module.RegistryIngressPostgresData,
        "bind",
        classmethod(bind),
    )
    return connect_calls, bind_calls


@pytest.mark.asyncio
async def test_sync_delegates_probes_before_opening_one_committing_transaction(
    monkeypatch,
):
    connection = _Connection()
    _runtime(monkeypatch, [connection], receipts=[None])
    events = []
    request = _request()

    async def core_sync(
        urls,
        *,
        timeout_seconds,
        data_factory,
        capability_limits_factory,
        client_factory,
        setup_probe,
        clock,
        request_id,
        command_sha256,
    ):
        events.append(("probe_window_complete", urls, timeout_seconds))
        assert capability_limits_factory() == CapabilityLimits()
        assert client_factory is None
        assert setup_probe is None
        assert clock is None
        assert request_id == _REQUEST_ID
        assert command_sha256 == registry_env_sync_command_sha256(request)
        async with data_factory() as data:
            events.append(("transaction_open", data))
            return _result(request)

    monkeypatch.setattr(manager_module, "sync_environment", core_sync)
    result = await RegistryEnvSyncManager(
        "postgresql+asyncpg://registry-role@localhost/annotation_test"
    ).sync(request)

    assert result.state == "committed"
    assert result.observed_urls == 1
    assert events[0] == ("probe_window_complete", [_URL], 4)
    assert events[1][0] == "transaction_open"
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert connection.close_count == 1


@pytest.mark.asyncio
async def test_sync_rolls_back_if_core_fails_before_transaction_success(monkeypatch):
    connection = _Connection()
    _runtime(monkeypatch, [connection], receipts=[None])

    async def core_sync(*_args, data_factory, **_kwargs):
        async with data_factory():
            raise ValueError("private probe diagnostic")

    monkeypatch.setattr(manager_module, "sync_environment", core_sync)
    with pytest.raises(ValueError, match="private probe diagnostic"):
        await RegistryEnvSyncManager(
            "postgresql://registry-role@localhost/annotation_test"
        ).sync(_request())

    assert connection.commit_count == 0
    assert connection.rollback_count == 1
    assert connection.close_count == 1


@pytest.mark.asyncio
async def test_ambiguous_commit_is_unavailable_until_caller_checks_status(
    monkeypatch,
):
    request = _request()
    committed_receipt = _result(request, state="committed")
    connections = [_Connection(commit_failure=True), _Connection()]
    connect_calls, _bind_calls = _runtime(
        monkeypatch, connections, receipts=[None, committed_receipt]
    )

    async def core_sync(
        _urls,
        *,
        data_factory,
        **_kwargs,
    ):
        async with data_factory():
            return _result(request)

    monkeypatch.setattr(manager_module, "sync_environment", core_sync)
    manager = RegistryEnvSyncManager(
        "postgresql://registry-role@localhost/annotation_test"
    )
    with pytest.raises(RegistryEnvSyncUnavailable):
        await manager.sync(request)

    assert len(connect_calls) == 1
    result = await manager.status(registry_env_sync_status_request(request))
    assert result.state == "committed"
    assert len(connect_calls) == 2
    assert connections[0].commit_count == 1
    assert connections[0].close_count == 1
    assert connections[1].commit_count == 1
    assert connections[1].close_count == 1


@pytest.mark.asyncio
async def test_ambiguous_commit_without_a_receipt_remains_unknown(monkeypatch):
    request = _request()
    connection = _Connection(commit_failure=True)
    status_connection = _Connection()
    connect_calls, _bind_calls = _runtime(
        monkeypatch, [connection, status_connection], receipts=[None, None]
    )

    async def core_sync(_urls, *, data_factory, **_kwargs):
        async with data_factory():
            return _result(request)

    monkeypatch.setattr(manager_module, "sync_environment", core_sync)
    manager = RegistryEnvSyncManager(
        "postgresql://registry-role@localhost/annotation_test"
    )
    with pytest.raises(RegistryEnvSyncUnavailable):
        await manager.sync(request)

    assert len(connect_calls) == 1
    result = await manager.status(registry_env_sync_status_request(request))
    assert result.state == "unknown"
    assert result.observed_urls is None
    assert connection.rollback_count == 0
    assert status_connection.commit_count == 1


@pytest.mark.asyncio
async def test_status_reads_a_receipt_in_a_new_transaction_or_returns_unknown(
    monkeypatch,
):
    request = _request()
    status_request = registry_env_sync_status_request(request)
    committed = _result(request, state="committed")
    connection = _Connection()
    _runtime(monkeypatch, [connection], receipts=[committed])

    result = await RegistryEnvSyncManager(
        "postgresql://registry-role@localhost/annotation_test"
    ).status(status_request)

    assert result.state == "committed"
    assert connection.commit_count == 1
    assert connection.close_count == 1

    connection = _Connection()
    _runtime(monkeypatch, [connection], receipts=[None])
    result = await RegistryEnvSyncManager(
        "postgresql://registry-role@localhost/annotation_test"
    ).status(status_request)
    assert result.state == "unknown"
    assert result.observed_urls is None


@pytest.mark.asyncio
async def test_empty_configuration_still_runs_core_and_commits_disconnect_pass(
    monkeypatch,
):
    connection = _Connection()
    _runtime(monkeypatch, [connection], receipts=[None])
    request = _request(urls=[])

    async def core_sync(urls, *, data_factory, request_id, command_sha256, **_kwargs):
        assert urls == []
        async with data_factory():
            return _result(request, observed_urls=0)

    monkeypatch.setattr(manager_module, "sync_environment", core_sync)
    result = await RegistryEnvSyncManager(
        "postgresql://registry-role@localhost/annotation_test"
    ).sync(request)

    assert result.state == "committed"
    assert result.observed_urls == 0
    assert connection.commit_count == 1

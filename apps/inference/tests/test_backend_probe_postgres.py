from __future__ import annotations

import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from aap_inference_contract.probe import BackendProbeUnavailable

from aap_inference.backend_probe_postgres import (
    BackendProbePostgresData,
    _FUNCTIONS,
    _OPEN_SUBJECT_SQL,
    _POLICY_SQL,
    _SCHEMA,
)


_REGISTRY_ID = UUID("764c1509-a323-4b89-8cc3-991cb3a0f72f")
_SUBJECT = "v1.c25hcHNob3Qtc3ViamVjdC1maXh0dXJl"
_BACKEND_JSON = (
    '{"id":"764c1509-a323-4b89-8cc3-991cb3a0f72f",'
    '"url":"http://synthetic-backend.test","auth_method":"none",'
    '"auth_token":null,"extra_params":{}}'
)
_POLICY = {
    "schema": "aap-inference-backend-probe-native/v1",
    "probe_protocol": "aap-inference-backend-probe/v1",
    "subject_protocol": "aap-inference-backend-probe-subject/v1",
    "origin_transaction": "in_progress",
    "functions": [
        "aap_inference_backend_probe_v1.backend_probe_policy()",
        "aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)",
    ],
    "ready": True,
}


class _FakePostgresError(Exception):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        super().__init__("synthetic private driver diagnostic")


class _FakeConnection:
    def __init__(self, *, failure=None, policy=_POLICY, subject=_BACKEND_JSON):
        self.failure = failure
        self.policy = policy
        self.subject = subject
        self.calls = []
        self.closed = 0
        self.terminated = 0

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if self.failure is not None:
            raise self.failure
        if sql == _POLICY_SQL:
            return self.policy
        if sql == _OPEN_SUBJECT_SQL:
            return self.subject
        raise AssertionError("unexpected Native function call")

    async def close(self, *, timeout):
        self.closed += 1
        assert timeout == 2

    def terminate(self):
        self.terminated += 1


def _fake_runtime(monkeypatch, connection):
    verified = []

    async def connect(*args, **kwargs):
        assert args == ("postgresql://probe-role@localhost/annotation_test",)
        assert kwargs["server_settings"] == {
            "application_name": "aap-inference-backend-probe",
            "default_transaction_read_only": "on",
        }
        return connection

    async def verify(connection, *, schema, functions):
        verified.append((schema, functions))

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=connect,
            PostgresError=_FakePostgresError,
            InterfaceError=type("InterfaceError", (Exception,), {}),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "aap_owner_data.postgres",
        SimpleNamespace(
            FunctionRoleUnavailable=type("FunctionRoleUnavailable", (Exception,), {}),
            verify_function_only_role=verify,
        ),
    )
    return verified


def _provider():
    return BackendProbePostgresData(
        "postgresql+asyncpg://probe-role@localhost/annotation_test"
    )


@pytest.mark.asyncio
async def test_postgres_provider_calls_only_verified_probe_functions(monkeypatch):
    connection = _FakeConnection()
    verified = _fake_runtime(monkeypatch, connection)
    data = _provider()

    assert await data.readiness() is True
    assert await data.open_subject(_SUBJECT, _REGISTRY_ID) == _BACKEND_JSON
    assert verified == [(_SCHEMA, _FUNCTIONS), (_SCHEMA, _FUNCTIONS)]
    assert connection.calls == [
        (_POLICY_SQL, ()),
        (_POLICY_SQL, ()),
        (_OPEN_SUBJECT_SQL, (_SUBJECT, _REGISTRY_ID)),
    ]
    assert connection.closed == 2
    assert connection.terminated == 0
    await data.close()


@pytest.mark.asyncio
async def test_postgres_provider_returns_not_ready_for_incompatible_native_policy(
    monkeypatch,
):
    connection = _FakeConnection(policy={**_POLICY, "ready": "true"})
    _fake_runtime(monkeypatch, connection)
    data = _provider()

    assert await data.readiness() is False
    assert connection.calls == [(_POLICY_SQL, ())]
    assert connection.closed == 1


@pytest.mark.asyncio
async def test_postgres_provider_sanitizes_native_connection_errors(monkeypatch):
    connection = _FakeConnection(failure=_FakePostgresError("08006"))
    _fake_runtime(monkeypatch, connection)
    data = _provider()

    with pytest.raises(BackendProbeUnavailable) as unavailable:
        await data.open_subject(_SUBJECT, _REGISTRY_ID)
    assert "synthetic private driver diagnostic" not in str(unavailable.value)
    assert connection.closed == 1

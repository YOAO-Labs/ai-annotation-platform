from __future__ import annotations

import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from aap_inference_contract.health_observation import (
    BackendHealthObservationUnavailable,
    HEALTH_OBSERVATION_NATIVE_FUNCTIONS,
)

from aap_inference.health_observation_postgres import (
    BackendHealthObservationPostgresData,
    _FUNCTIONS,
    _OPEN_SUBJECT_SQL,
    _POLICY_SQL,
    _SCHEMA,
    _SEAL_OBSERVATION_SQL,
)

_REGISTRY_ID = UUID("9ad8377b-f969-4135-8f48-22eb45c80bbd")
_REQUEST_ID = UUID("6a8382ce-7330-4779-8515-1fa84689f15f")
_SUBJECT = "v1.c3ludGhldGljIG9ic2VydmF0aW9uIHN1YmplY3Q="
_EVIDENCE = "v1.c3ludGhldGljIHNlYWxlZCBldmlkZW5jZQ=="
_RAW_SNAPSHOT = '{"backend_json":"{}","gpu_health_challenge":null,"timeout_seconds":0}'
_POLICY = {
    "schema": "aap-inference-health-observation-native/v1",
    "observation_protocol": "aap-inference-health-observation/v1",
    "subject_protocol": "aap-inference-health-observation-subject/v1",
    "evidence_protocol": "aap-inference-health-observation-evidence/v1",
    "origin_transaction": "in_progress",
    "evidence_protection": "pgcrypto-pgp-aes256-mdc",
    "functions": list(HEALTH_OBSERVATION_NATIVE_FUNCTIONS),
    "ready": True,
}


class _FakePostgresError(Exception):
    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__("synthetic private PostgreSQL diagnostic")


class _FakeConnection:
    def __init__(self, *, result_by_sql=None, failure=None):
        self.result_by_sql = result_by_sql or {
            _POLICY_SQL: _POLICY,
            _OPEN_SUBJECT_SQL: _RAW_SNAPSHOT,
            _SEAL_OBSERVATION_SQL: _EVIDENCE,
        }
        self.failure = failure
        self.calls = []
        self.closed = 0
        self.terminated = 0

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if self.failure is not None:
            raise self.failure
        return self.result_by_sql[sql]

    async def close(self, *, timeout):
        assert timeout == 2
        self.closed += 1

    def terminate(self):
        self.terminated += 1


def _runtime(monkeypatch, connections):
    verified = []

    async def connect(*args, **kwargs):
        assert args == ("postgresql://observation-role@localhost/annotation_test",)
        assert kwargs["server_settings"] == {
            "application_name": "aap-inference-health-observation",
            "default_transaction_read_only": "on",
        }
        connection = connections[len(verified)]
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
    return BackendHealthObservationPostgresData(
        "postgresql+asyncpg://observation-role@localhost/annotation_test"
    )


@pytest.mark.asyncio
async def test_provider_uses_only_the_three_provider_functions(monkeypatch):
    connections = [_FakeConnection() for _ in range(3)]
    verified = _runtime(monkeypatch, connections)
    data = _provider()

    assert await data.readiness() is True
    assert await data.open_subject(_SUBJECT, _REGISTRY_ID, _REQUEST_ID) == _RAW_SNAPSHOT
    assert (
        await data.seal_observation(
            _SUBJECT,
            _REGISTRY_ID,
            _REQUEST_ID,
            '{"kind":"observed","healthy":true}',
        )
        == _EVIDENCE
    )

    assert verified == [(_SCHEMA, _FUNCTIONS)] * 3
    assert _FUNCTIONS == {
        ("health_observation_policy", ""),
        ("open_health_observation_subject", "text, uuid, uuid"),
        ("seal_health_observation", "text, uuid, uuid, text"),
    }
    assert connections[0].calls == [(_POLICY_SQL, ())]
    assert connections[1].calls == [
        (_POLICY_SQL, ()),
        (_OPEN_SUBJECT_SQL, (_SUBJECT, _REGISTRY_ID, _REQUEST_ID)),
    ]
    assert connections[2].calls == [
        (_POLICY_SQL, ()),
        (
            _SEAL_OBSERVATION_SQL,
            (_SUBJECT, _REGISTRY_ID, _REQUEST_ID, '{"kind":"observed","healthy":true}'),
        ),
    ]
    assert [connection.closed for connection in connections] == [1, 1, 1]
    assert [connection.terminated for connection in connections] == [0, 0, 0]
    await data.close()


@pytest.mark.asyncio
async def test_provider_rejects_an_incompatible_native_policy(monkeypatch):
    connection = _FakeConnection(
        result_by_sql={_POLICY_SQL: {**_POLICY, "ready": "true"}}
    )
    _runtime(monkeypatch, [connection])

    assert await _provider().readiness() is False
    assert connection.calls == [(_POLICY_SQL, ())]
    assert connection.closed == 1


@pytest.mark.asyncio
async def test_provider_sanitizes_database_errors_and_closes_connections(monkeypatch):
    connection = _FakeConnection(failure=_FakePostgresError("08006"))
    _runtime(monkeypatch, [connection])

    with pytest.raises(BackendHealthObservationUnavailable) as error:
        await _provider().open_subject(_SUBJECT, _REGISTRY_ID, _REQUEST_ID)

    assert "synthetic private PostgreSQL diagnostic" not in str(error.value)
    assert connection.closed == 1

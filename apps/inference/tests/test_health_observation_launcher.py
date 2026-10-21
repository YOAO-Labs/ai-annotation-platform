from __future__ import annotations

import pytest

import aap_inference.__main__ as launcher


class _StopRuntime(Exception):
    pass


@pytest.mark.asyncio
async def test_observation_runtime_uses_its_own_dsn_and_token_and_closes(monkeypatch):
    for name in (
        "INFERENCE_PIPELINES_DATABASE_URL",
        "INFERENCE_CONTROL_TOKEN",
        "INFERENCE_BACKEND_PROBE_DATABASE_URL",
        "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN",
        "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL",
        "INFERENCE_HEALTH_OBSERVATION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("INFERENCE_HOST", "127.0.0.1")
    monkeypatch.setenv("INFERENCE_PORT", "8768")
    monkeypatch.setenv("INFERENCE_MAX_CONCURRENCY", "8")
    monkeypatch.setenv(
        "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL",
        "postgresql://observation-role@localhost/annotation_test",
    )
    monkeypatch.setenv("INFERENCE_HEALTH_OBSERVATION_TOKEN", "observation-secret")

    class _Data:
        def __init__(self, dsn):
            self.dsn = dsn
            self.closed = False

        async def close(self):
            self.closed = True

    class _Manager:
        def __init__(self, data):
            self.data = data

    class _Server:
        def __init__(self, manager, data, token, **kwargs):
            self.manager = manager
            self.data = data
            self.token = token
            self.kwargs = kwargs
            self.closed = False

        async def serve_forever(self):
            raise _StopRuntime()

        async def close(self):
            self.closed = True

    created = {}

    def data_factory(dsn):
        created["data"] = _Data(dsn)
        return created["data"]

    def manager_factory(data):
        created["manager"] = _Manager(data)
        return created["manager"]

    def server_factory(manager, data, token, **kwargs):
        created["server"] = _Server(manager, data, token, **kwargs)
        return created["server"]

    monkeypatch.setattr(launcher, "BackendHealthObservationPostgresData", data_factory)
    monkeypatch.setattr(launcher, "BackendHealthObservationManager", manager_factory)
    monkeypatch.setattr(launcher, "PipelineHttpServer", server_factory)

    with pytest.raises(_StopRuntime):
        await launcher.run()

    assert created["data"].dsn.endswith("/annotation_test")
    assert created["manager"].data is created["data"]
    assert created["server"].kwargs["health_observation_manager"] is created["manager"]
    assert (
        created["server"].kwargs["health_observation_control_token"]
        == "observation-secret"
    )
    assert created["server"].token is None
    assert created["data"].closed is True
    assert created["server"].closed is True


@pytest.mark.asyncio
async def test_observation_runtime_rejects_partial_selector_pair(monkeypatch):
    for name in (
        "INFERENCE_PIPELINES_DATABASE_URL",
        "INFERENCE_CONTROL_TOKEN",
        "INFERENCE_BACKEND_PROBE_DATABASE_URL",
        "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN",
        "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL",
        "INFERENCE_HEALTH_OBSERVATION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("INFERENCE_HOST", "127.0.0.1")
    monkeypatch.setenv("INFERENCE_PORT", "8768")
    monkeypatch.setenv("INFERENCE_MAX_CONCURRENCY", "8")
    monkeypatch.setenv(
        "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL",
        "postgresql://observation-role@localhost/annotation_test",
    )

    with pytest.raises(ValueError, match="must be configured together"):
        await launcher.run()

from __future__ import annotations

import pytest

import aap_inference.__main__ as launcher


class _StopRuntime(Exception):
    pass


@pytest.mark.asyncio
async def test_registry_ingress_uses_its_own_dsn_and_token_and_closes(monkeypatch):
    selector_names = (
        "INFERENCE_PIPELINES_DATABASE_URL",
        "INFERENCE_CONTROL_TOKEN",
        "INFERENCE_BACKEND_PROBE_DATABASE_URL",
        "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN",
        "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL",
        "INFERENCE_HEALTH_OBSERVATION_TOKEN",
        "INFERENCE_REGISTRY_INGRESS_DATABASE_URL",
        "INFERENCE_REGISTRY_INGRESS_TOKEN",
    )
    for name in selector_names:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("INFERENCE_HOST", "127.0.0.1")
    monkeypatch.setenv("INFERENCE_PORT", "8768")
    monkeypatch.setenv("INFERENCE_MAX_CONCURRENCY", "8")
    monkeypatch.setenv("INFERENCE_CONTROL_TOKEN", "pipeline-secret")
    monkeypatch.setenv(
        "INFERENCE_REGISTRY_INGRESS_DATABASE_URL",
        "postgresql://registry-role@localhost/annotation_test",
    )
    monkeypatch.setenv("INFERENCE_REGISTRY_INGRESS_TOKEN", "registry-secret")

    class _Manager:
        def __init__(self, dsn):
            self.dsn = dsn
            self.closed = False

        async def close(self):
            self.closed = True

    class _Server:
        def __init__(self, manager, data, token, **kwargs):
            self.manager = manager
            self.data = data
            self.pipeline_token = token
            self.kwargs = kwargs
            self.closed = False

        async def serve_forever(self):
            raise _StopRuntime()

        async def close(self):
            self.closed = True

    created = {}

    def manager_factory(dsn):
        created["manager"] = _Manager(dsn)
        return created["manager"]

    def server_factory(manager, data, token, **kwargs):
        created["server"] = _Server(manager, data, token, **kwargs)
        return created["server"]

    monkeypatch.setattr(launcher, "RegistryEnvSyncManager", manager_factory)
    monkeypatch.setattr(launcher, "PipelineHttpServer", server_factory)

    with pytest.raises(_StopRuntime):
        await launcher.run()

    assert created["manager"].dsn.endswith("/annotation_test")
    assert created["server"].manager is None
    assert created["server"].pipeline_token == "pipeline-secret"
    assert created["server"].kwargs["registry_ingress_manager"] is created["manager"]
    assert (
        created["server"].kwargs["registry_ingress_control_token"] == "registry-secret"
    )
    assert created["manager"].closed is True
    assert created["server"].closed is True


@pytest.mark.asyncio
async def test_registry_ingress_requires_its_own_complete_selector_pair(monkeypatch):
    for name in (
        "INFERENCE_PIPELINES_DATABASE_URL",
        "INFERENCE_CONTROL_TOKEN",
        "INFERENCE_BACKEND_PROBE_DATABASE_URL",
        "INFERENCE_BACKEND_PROBE_CONTROL_TOKEN",
        "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL",
        "INFERENCE_HEALTH_OBSERVATION_TOKEN",
        "INFERENCE_REGISTRY_INGRESS_DATABASE_URL",
        "INFERENCE_REGISTRY_INGRESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("INFERENCE_HOST", "127.0.0.1")
    monkeypatch.setenv("INFERENCE_PORT", "8768")
    monkeypatch.setenv("INFERENCE_MAX_CONCURRENCY", "8")
    monkeypatch.setenv(
        "INFERENCE_REGISTRY_INGRESS_DATABASE_URL",
        "postgresql://registry-role@localhost/annotation_test",
    )

    with pytest.raises(ValueError, match="must be configured together"):
        await launcher.run()

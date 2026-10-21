from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID

import pytest
from aap_inference_contract.health_observation import (
    make_health_observation_request,
)

import aap_inference.health_observation_manager as observation_module
from aap_inference.health_observation_manager import BackendHealthObservationManager

_REGISTRY_ID = UUID("9ad8377b-f969-4135-8f48-22eb45c80bbd")
_REQUEST_ID = UUID("6a8382ce-7330-4779-8515-1fa84689f15f")
_SUBJECT = "v1." + base64.b64encode(b"opaque initial registry snapshot").decode()
_EVIDENCE = "v1." + base64.b64encode(b"opaque sealed observation").decode()
_CHALLENGE = "a" * 64


def _snapshot(*, timeout=3, backend=None):
    return json.dumps(
        {
            "backend_json": json.dumps(
                backend
                or {
                    "id": str(_REGISTRY_ID),
                    "url": "http://synthetic-backend.test",
                    "auth_method": "token",
                    "auth_token": "synthetic-secret",
                    "gpu_resource_id": "gpu-test",
                    "vram_budget_mb": 4096,
                    "extra_params": {"max_concurrency": 3},
                }
            ),
            "gpu_health_challenge": _CHALLENGE,
            "timeout_seconds": timeout,
        }
    )


class _Data:
    def __init__(self, raw_snapshot: str):
        self.raw_snapshot = raw_snapshot
        self.open_calls = []
        self.seal_calls = []

    async def readiness(self):
        return True

    async def open_subject(self, subject, registry_id, request_id):
        self.open_calls.append((subject, registry_id, request_id))
        return self.raw_snapshot

    async def seal_observation(
        self, subject, registry_id, request_id, observation_json
    ):
        self.seal_calls.append((subject, registry_id, request_id, observation_json))
        return _EVIDENCE


class _Client:
    def __init__(self, *, health=(True, {"gpu_info": {"name": "fixture"}}), setup=None):
        self.health = health
        self.setup_result = setup if setup is not None else {"loaded": True}
        self.calls = []

    async def health_meta(self, *, gpu_health_challenge=None):
        self.calls.append(("health_meta", gpu_health_challenge))
        if isinstance(self.health, Exception):
            raise self.health
        return self.health

    async def setup(self):
        self.calls.append(("setup",))
        if isinstance(self.setup_result, Exception):
            raise self.setup_result
        return self.setup_result


def _request():
    return make_health_observation_request(
        _REGISTRY_ID,
        _SUBJECT,
        request_id=_REQUEST_ID,
    )


@pytest.mark.asyncio
async def test_manager_observes_then_seals_only_the_private_result():
    data = _Data(_snapshot())
    client = _Client()
    seen_backend = []

    def factory(backend):
        seen_backend.append(backend)
        return client

    reply = await BackendHealthObservationManager(data, factory).execute(_request())

    assert data.open_calls == [(_SUBJECT, _REGISTRY_ID, _REQUEST_ID)]
    assert client.calls == [("health_meta", _CHALLENGE), ("setup",)]
    assert seen_backend[0].url == "http://synthetic-backend.test"
    assert seen_backend[0].auth_token == "synthetic-secret"
    assert data.seal_calls[0][:3] == (_SUBJECT, _REGISTRY_ID, _REQUEST_ID)
    assert json.loads(data.seal_calls[0][3]) == {
        "kind": "observed",
        "healthy": True,
        "meta": {"gpu_info": {"name": "fixture"}},
        "setup_failed": False,
        "setup": {"loaded": True},
    }
    assert reply.model_dump(mode="json", by_alias=True) == {
        "schema": "aap-inference-health-observation/v1",
        "request_id": str(_REQUEST_ID),
        "registry_id": str(_REGISTRY_ID),
        "subject_sha256": hashlib.sha256(_SUBJECT.encode()).hexdigest(),
        "evidence": _EVIDENCE,
    }
    assert "synthetic-secret" not in repr(reply)


@pytest.mark.asyncio
async def test_manager_skips_setup_when_health_result_is_not_eligible():
    data = _Data(_snapshot())
    client = _Client(health=(True, None))

    await BackendHealthObservationManager(data, lambda _backend: client).execute(
        _request()
    )

    assert client.calls == [("health_meta", _CHALLENGE)]
    assert json.loads(data.seal_calls[0][3]) == {
        "kind": "observed",
        "healthy": True,
        "meta": None,
        "setup_failed": False,
        "setup": None,
    }


@pytest.mark.asyncio
async def test_manager_seals_setup_failure_and_preserves_nonfinite_raw_values():
    data = _Data(_snapshot())
    client = _Client(
        health=(True, {"load": float("nan")}),
        setup=RuntimeError("private setup diagnostic"),
    )

    await BackendHealthObservationManager(data, lambda _backend: client).execute(
        _request()
    )

    sealed_text = data.seal_calls[0][3]
    assert '"setup_failed":true' in sealed_text
    assert '"load":NaN' in sealed_text
    assert "private setup diagnostic" not in sealed_text
    assert client.calls == [("health_meta", _CHALLENGE), ("setup",)]


@pytest.mark.asyncio
async def test_constructor_error_is_sealed_without_exposing_it_on_the_reply():
    data = _Data(_snapshot())

    def factory(_backend):
        raise ValueError("synthetic constructor detail")

    reply = await BackendHealthObservationManager(data, factory).execute(_request())

    assert json.loads(data.seal_calls[0][3]) == {
        "kind": "legacy_constructor_error",
        "error_type": "ValueError",
        "error_message": "synthetic constructor detail",
    }
    assert "synthetic constructor detail" not in repr(reply)


@pytest.mark.asyncio
async def test_legacy_health_failure_is_sealed_without_exposing_it_on_the_wire():
    data = _Data(_snapshot())
    client = _Client(health=RuntimeError("synthetic-secret diagnostic"))

    reply = await BackendHealthObservationManager(
        data, lambda _backend: client
    ).execute(_request())
    assert json.loads(data.seal_calls[0][3]) == {
        "kind": "legacy_health_error",
        "error_type": "RuntimeError",
        "error_message": "synthetic-secret diagnostic",
    }
    assert "synthetic-secret" not in repr(reply)
    assert client.calls == [("health_meta", _CHALLENGE)]


@pytest.mark.asyncio
async def test_default_transport_receives_legacy_nonpositive_timeout(monkeypatch):
    data = _Data(_snapshot(timeout=-0.5))
    captured = {}

    class _Transport(_Client):
        def __init__(self, backend, *, options, dispatch_context, observe):
            captured.update(
                backend=backend,
                health_timeout=options.ml_health_timeout,
                predict_timeout=options.ml_predict_timeout,
                dispatch_context=dispatch_context,
                observe=observe,
            )
            super().__init__()

    monkeypatch.setattr(observation_module, "BackendTransport", _Transport)
    await BackendHealthObservationManager(data).execute(_request())

    assert captured["health_timeout"] == -0.5
    assert captured["predict_timeout"] == -0.5
    assert callable(captured["dispatch_context"])
    assert callable(captured["observe"])

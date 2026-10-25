from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID

import pytest
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BACKEND_PROBE_PROTOCOL,
    BackendProbeFailure,
    BackendProbeLegacyFailure,
    BackendProbeUnavailable,
    make_backend_probe_request,
    reply_modalities,
)

import aap_inference.backend_probe_manager as manager_module
from aap_inference.backend_probe_manager import BackendProbeManager


_REGISTRY_ID = UUID("764c1509-a323-4b89-8cc3-991cb3a0f72f")
_SUBJECT = "v1." + base64.b64encode(b"opaque snapshot payload").decode("ascii")


class _Data:
    def __init__(self, raw_json: str | None = None, error=None) -> None:
        self.raw_json = raw_json
        self.error = error
        self.calls = []

    async def open_subject(self, probe_subject, registry_id):
        self.calls.append((probe_subject, registry_id))
        if self.error is not None:
            raise self.error
        return self.raw_json

    async def readiness(self) -> bool:
        return True


def _raw_backend_json(*, extra_params=None, url="http://synthetic-backend.test"):
    return json.dumps(
        {
            "id": str(_REGISTRY_ID),
            "url": url,
            "auth_method": "token",
            "auth_token": "synthetic-endpoint-token",
            "extra_params": {"opaque": [1, None]}
            if extra_params is None
            else extra_params,
        },
        separators=(",", ":"),
    )


def _request():
    return make_backend_probe_request(
        _REGISTRY_ID,
        _SUBJECT,
        timeout=2.5,
        limits=CapabilityLimits(),
        request_id=UUID("1079f90d-b6d8-420d-87f5-4f8f244093be"),
    )


@pytest.mark.asyncio
async def test_manager_opens_bound_snapshot_and_reuses_existing_probe(monkeypatch):
    data = _Data(_raw_backend_json())
    manager = BackendProbeManager(data)
    observed = []

    async def fake_probe(locator, *, timeout, limits):
        observed.append((locator, timeout, limits))
        return {
            "supported_prompts": ["point"],
            "supported_trackers": ["sam3_video"],
        }

    monkeypatch.setattr(manager_module, "probe_backend_capabilities", fake_probe)
    request = _request()
    reply = await manager.execute(request)

    assert data.calls == [(_SUBJECT, _REGISTRY_ID)]
    assert len(observed) == 1
    locator, timeout, limits = observed[0]
    assert locator.id == _REGISTRY_ID
    assert locator.url == "http://synthetic-backend.test"
    assert locator.auth_method == "token"
    assert locator.auth_token == "synthetic-endpoint-token"
    assert locator.extra_params == {"opaque": [1, None]}
    assert timeout == 2.5
    assert limits == CapabilityLimits()
    assert type(limits) is CapabilityLimits
    assert reply.model_dump(by_alias=True)["schema"] == BACKEND_PROBE_PROTOCOL
    assert reply.ok is True
    assert reply.modalities == ["image", "video"]
    assert reply.request_id == request.request_id
    assert reply.registry_id == _REGISTRY_ID
    assert reply.subject_sha256 == hashlib.sha256(_SUBJECT.encode("ascii")).hexdigest()
    assert "synthetic-endpoint-token" not in reply.model_dump_json()


@pytest.mark.asyncio
async def test_manager_materialization_failure_is_bound_and_secret_free(monkeypatch):
    huge_integer = "9" * 5000
    raw_json = (
        '{"id":"'
        + str(_REGISTRY_ID)
        + '","url":"http://synthetic-backend.test","auth_method":"token",'
        '"auth_token":"synthetic-endpoint-token","extra_params":{"n":'
        + huge_integer
        + "}}"
    )
    data = _Data(raw_json)
    manager = BackendProbeManager(data)

    async def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("legacy JSON materialization must fail before probing")

    monkeypatch.setattr(manager_module, "probe_backend_capabilities", unexpected_probe)
    request = _request()
    reply = await manager.execute(request)

    assert isinstance(reply, BackendProbeFailure)
    assert reply.model_dump(by_alias=True)["schema"] == BACKEND_PROBE_PROTOCOL
    assert reply.ok is False
    assert reply.error == "legacy_materialization_failure"
    assert reply.request_id == request.request_id
    assert reply.registry_id == _REGISTRY_ID
    assert reply.subject_sha256 == hashlib.sha256(_SUBJECT.encode("ascii")).hexdigest()
    serialized = reply.model_dump_json()
    assert "synthetic-endpoint-token" not in serialized
    assert huge_integer not in serialized
    with pytest.raises(BackendProbeLegacyFailure):
        reply_modalities(request, reply)


@pytest.mark.asyncio
async def test_backend_probe_failures_remain_fail_open_but_subject_open_is_required(
    monkeypatch,
):
    data = _Data(_raw_backend_json())
    manager = BackendProbeManager(data)

    async def failed_probe(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager_module, "probe_backend_capabilities", failed_probe)
    request = _request()
    reply = await manager.execute(request)
    assert reply.ok is True
    assert reply.modalities is None
    assert (
        await manager.probe_modalities(
            _REGISTRY_ID,
            _SUBJECT,
            timeout=2.5,
            limits=CapabilityLimits(),
        )
        is None
    )

    unavailable = BackendProbeUnavailable()
    broken = BackendProbeManager(_Data(error=unavailable))
    with pytest.raises(BackendProbeUnavailable):
        await broken.execute(request)


@pytest.mark.asyncio
async def test_nullable_auth_method_keeps_the_existing_unauthenticated_probe(
    monkeypatch,
):
    endpoint = json.loads(_raw_backend_json())
    endpoint["auth_method"] = None
    endpoint["auth_token"] = None
    observed = []

    async def probe(locator, **kwargs):
        observed.append(locator)
        return {"supported_prompts": ["bbox"]}

    monkeypatch.setattr(manager_module, "probe_backend_capabilities", probe)
    reply = await BackendProbeManager(_Data(json.dumps(endpoint))).execute(_request())
    assert reply.ok is True and reply.modalities == ["image"]
    assert len(observed) == 1
    assert observed[0].auth_method is None and observed[0].auth_token is None

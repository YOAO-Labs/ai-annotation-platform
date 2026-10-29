from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from aap_protocol_v2.lifecycle import LifecycleModeResponse, verify_admission_token

from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference.gpu import membership_activation
from aap_inference.gpu.control_preparation import GPULegacyAckPreparation
from aap_inference.gpu.contracts import GPUArbiterDispatchError
from aap_inference.gpu.installation_activation import activate_gpu_installation
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference_contract.gpu import GPUInstallationControlRequest


class Harness:
    def __init__(self, monkeypatch):
        self.request = GPUInstallationControlRequest(
            registry_id=uuid4(),
            resource_id="node/GPU-a",
            endpoint="http://backend:8000",
            boot_id="boot-1",
            operation_id=uuid4(),
            container_id="a" * 64,
            pool_ids=["embeddings", "models"],
        )
        r = self.request
        resource = {"gpu_resource_id": r.resource_id}
        self.endpoint = SimpleNamespace(
            registry_id=r.registry_id,
            url=r.endpoint,
            gpu_resource_id=r.resource_id,
            endpoint_sha256="endpoint-1",
            extra_params={
                "plugin": {
                    "installation_id": str(r.registry_id),
                    "status": "prepared",
                    "resource": resource,
                },
                "plugin_lifecycle": {
                    "launch": {
                        "registry_id": str(r.registry_id),
                        "container_id": r.container_id,
                        "resource": resource,
                    },
                    "qualification": {"report": {}, "sha256": "qualification"},
                    "stopped": [],
                },
            },
        )
        self.current = self.endpoint
        self.key = Ed25519PrivateKey.generate()
        self.signer = GPUAdmissionTokenSigner(active_kid="test", _private_key=self.key)
        self.calls = []
        self.health_mutations = [{}, {}]
        self.health_pause = None
        self.health_resume = None
        self.promotion_failure = False

        async def no_alias(*args):
            return None

        async def prepare(
            data, backend_id, *, gpu_resource_id, membership_epoch, readiness_demoter
        ):
            self.calls.append(
                ("prepare", backend_id, gpu_resource_id, membership_epoch)
            )
            return GPULegacyAckPreparation(
                action="issue",
                backend_id=backend_id,
                resource_id=gpu_resource_id,
                membership_epoch=membership_epoch,
                boot_id=r.boot_id,
                runtime_epoch="runtime-1",
                control_epoch="7",
                token_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                proof_ready=False,
            )

        monkeypatch.setattr(membership_activation, "_lifecycle_alias_reason", no_alias)
        monkeypatch.setattr(
            membership_activation, "prepare_gpu_backend_legacy_ack", prepare
        )

    def residency(self):
        r = self.request
        return {
            "state": "unloaded",
            "gpu_loaded": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "evictable": False,
            "generation": None,
            "pools": {
                p: {"resident": False, "device": None, "provider": None}
                for p in r.pool_ids
            },
            "boot_id": r.boot_id,
            "lifecycle_gate": "legacy",
            "control_epoch": "7",
            "identity": {
                "audience": "aap-gpu-lifecycle",
                "backend_registry_id": str(r.registry_id),
                "gpu_resource_id": r.resource_id,
            },
        }

    async def open_owned(self, registry_id, *, request_id, purpose, binding):
        self.calls.append(("open", purpose, registry_id))
        return self.current

    async def step(self, operation, payload):
        self.calls.append((operation, payload))
        assert operation == "membership_candidates"
        return {
            "candidates": [
                {"backend_id": str(self.request.registry_id), "membership_epoch": 3}
            ]
        }

    async def health_meta(
        self, endpoint, *, request_id, gpu_health_challenge, timeout_seconds
    ):
        index = sum(call[0] == "health" for call in self.calls)
        self.calls.append(("health", gpu_health_challenge, endpoint))
        if index == 1 and self.health_pause is not None:
            self.health_pause.set()
            await self.health_resume.wait()
        metadata = {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": self.residency(),
        }
        mutation = self.health_mutations[index]
        metadata["residency"].update(mutation.get("residency", {}))
        metadata.update(mutation.get("metadata", {}))
        return mutation.get("healthy", True), metadata

    @asynccontextmanager
    async def control_binding(self, backend_id, *, request_id, operation):
        assert backend_id == self.request.registry_id and operation == "mode"
        yield object()

    async def lifecycle_mode(
        self, endpoint, request, admission_token, *, request_id, timeout_seconds
    ):
        claims = verify_admission_token(
            admission_token, keyring={"test": self.key.public_key()}
        )
        self.calls.append(("mode", claims, request))
        if self.promotion_failure:
            raise RuntimeError("mode failed")
        return LifecycleModeResponse.model_validate(
            {
                "ok": True,
                "gate": "legacy",
                "control_epoch": "7",
                "residency": self.residency(),
            }
        )

    async def demote(self, resource_id):
        self.calls.append(("demote", resource_id))

    async def activate(self):
        return await activate_gpu_installation(
            self.request,
            data=self,
            signer_factory=lambda: self.signer,
            endpoint_source=self,
            backend_transport=self,
            readiness_demoter=self.demote,
        )


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


@pytest.mark.asyncio
async def test_prepared_installation_receives_signed_promotion_and_fresh_challenges(
    harness,
):
    reply = await harness.activate()
    r = harness.request
    assert (
        reply.registry_id,
        reply.resource_id,
        reply.endpoint,
        reply.boot_id,
        reply.control_epoch,
        reply.pool_ids,
    ) == (
        r.registry_id,
        r.resource_id,
        r.endpoint,
        r.boot_id,
        "7",
        r.pool_ids,
    )
    mode = next(call for call in harness.calls if call[0] == "mode")
    claims = mode[1]
    assert (
        claims.backend_registry_id,
        claims.gpu_resource_id,
        claims.boot_id,
        claims.control_epoch,
    ) == (
        str(r.registry_id),
        r.resource_id,
        r.boot_id,
        "7",
    )
    assert claims.operation == "mode:legacy:7"
    assert mode[2].gate.value == "legacy"
    events = [call[0] for call in harness.calls]
    assert (
        events.index("health")
        < events.index("prepare")
        < events.index("mode")
        < len(events) - 1
    )
    challenges = [call[1] for call in harness.calls if call[0] == "health"]
    assert len(challenges) == 2 and challenges[0] != challenges[1]
    assert all(len(challenge) == 64 for challenge in challenges)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "endpoint",
        "resource",
        "registry",
        "container",
        "stopped_container",
        "stopped_boot",
        "qualification",
    ],
)
async def test_rejects_changed_or_stopped_prepared_identity_before_promotion(
    harness, change
):
    r, endpoint = harness.request, harness.endpoint
    lifecycle = endpoint.extra_params["plugin_lifecycle"]
    if change == "endpoint":
        endpoint.url = "http://other:8000"
    elif change == "resource":
        endpoint.gpu_resource_id = "node/GPU-other"
    elif change == "registry":
        lifecycle["launch"]["registry_id"] = str(uuid4())
    elif change == "container":
        lifecycle["launch"]["container_id"] = "other"
    elif change == "stopped_container":
        lifecycle["stopped"] = [{"container_id": r.container_id, "boot_id": "old"}]
    elif change == "stopped_boot":
        lifecycle["stopped"] = [{"container_id": "old", "boot_id": r.boot_id}]
    else:
        lifecycle["qualification"] = None
    with pytest.raises(GPUArbiterDispatchError):
        await harness.activate()
    assert not any(call[0] in {"health", "prepare", "mode"} for call in harness.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"healthy": False},
        {"metadata": {GPU_HEALTH_CHALLENGE_ECHO_MARKER: "replayed"}},
        {"residency": {"boot_id": "wrong-boot"}},
        {"residency": {"pools": {"models": {}}}},
    ],
)
async def test_initial_challenge_rejects_unhealthy_unauthenticated_or_wrong_process(
    harness, mutation
):
    harness.health_mutations[0] = mutation
    with pytest.raises(GPUArbiterDispatchError):
        await harness.activate()
    assert not any(call[0] in {"prepare", "mode"} for call in harness.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"healthy": False},
        {"metadata": {GPU_HEALTH_CHALLENGE_ECHO_MARKER: "replayed"}},
        {"residency": {"boot_id": "wrong-boot"}},
        {"residency": {"control_epoch": "8"}},
        {"residency": {"pools": {"models": {}}}},
        {
            "residency": {
                "identity": {
                    "audience": "aap-gpu-lifecycle",
                    "backend_registry_id": "wrong",
                    "gpu_resource_id": "node/GPU-a",
                }
            }
        },
        {
            "residency": {
                "identity": {
                    "audience": "aap-gpu-lifecycle",
                    "backend_registry_id": "correct",
                    "gpu_resource_id": "other",
                }
            }
        },
    ],
)
async def test_post_promotion_challenge_rejects_stale_or_wrong_live_identity(
    harness, mutation
):
    identity = mutation.get("residency", {}).get("identity", {})
    if identity.get("backend_registry_id") == "correct":
        identity["backend_registry_id"] = str(harness.request.registry_id)
    harness.health_mutations[1] = mutation
    with pytest.raises(GPUArbiterDispatchError):
        await harness.activate()
    assert any(call[0] == "mode" for call in harness.calls)


@pytest.mark.asyncio
async def test_replaying_first_authenticated_health_cannot_complete_promotion(harness):
    original_health = harness.health_meta
    first_challenge = None

    async def replay(endpoint, *, request_id, gpu_health_challenge, timeout_seconds):
        nonlocal first_challenge
        healthy, metadata = await original_health(
            endpoint,
            request_id=request_id,
            gpu_health_challenge=gpu_health_challenge,
            timeout_seconds=timeout_seconds,
        )
        if first_challenge is None:
            first_challenge = gpu_health_challenge
        else:
            metadata[GPU_HEALTH_CHALLENGE_ECHO_MARKER] = first_challenge
        return healthy, metadata

    harness.health_meta = replay
    with pytest.raises(GPUArbiterDispatchError):
        await harness.activate()
    assert any(call[0] == "mode" for call in harness.calls)


@pytest.mark.asyncio
async def test_signed_mode_failure_never_reports_activation(harness):
    harness.promotion_failure = True
    with pytest.raises(GPUArbiterDispatchError):
        await harness.activate()
    assert sum(call[0] == "health" for call in harness.calls) == 1


@pytest.mark.asyncio
async def test_endpoint_replacement_during_post_promotion_health_is_rejected(harness):
    harness.health_pause, harness.health_resume = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(harness.activate())
    await harness.health_pause.wait()
    harness.current = SimpleNamespace(endpoint_sha256="endpoint-2")
    harness.health_resume.set()
    with pytest.raises(GPUArbiterDispatchError):
        await task
    assert any(call[0] == "mode" for call in harness.calls)


@pytest.mark.asyncio
async def test_stopped_identity_racing_with_promotion_is_rejected(harness):
    harness.health_pause, harness.health_resume = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(harness.activate())
    await harness.health_pause.wait()
    harness.current = deepcopy(harness.endpoint)
    harness.current.extra_params["plugin_lifecycle"]["stopped"] = [
        {
            "container_id": harness.request.container_id,
            "boot_id": harness.request.boot_id,
        }
    ]
    # Owned endpoint digests include plugin lifecycle metadata.
    harness.current.endpoint_sha256 = "endpoint-with-stopped-identity"
    harness.health_resume.set()
    with pytest.raises(GPUArbiterDispatchError):
        await task
    assert any(call[0] == "mode" for call in harness.calls)

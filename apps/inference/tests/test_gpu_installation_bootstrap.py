from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from aap_protocol_v2.lifecycle import (
    LifecycleModeResponse, LifecycleResetResponse, verify_admission_token,
)

from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference.gpu.installation_bootstrap import bootstrap_gpu_installation
from aap_inference.gpu.signing import GPUAdmissionTokenSigner
from aap_inference_contract.gpu import GPUInstallationControlRequest


class Clock:
    def __init__(self):
        self.now = time.time()
        self.elapsed = 0.0

    def time(self):
        return self.now + self.elapsed

    def monotonic(self):
        return self.elapsed

    async def sleep(self, duration):
        self.elapsed += duration


class BootstrapHarness:
    def __init__(self):
        self.request = GPUInstallationControlRequest(
            registry_id=uuid4(), resource_id="node/GPU-a", endpoint="http://backend:8000",
            boot_id="boot-1", operation_id=uuid4(), container_id="container-1",
            pool_ids=["embeddings", "models"],
        )
        self.endpoint = SimpleNamespace(url=self.request.endpoint, gpu_resource_id=self.request.resource_id,
                                        endpoint_sha256="prepared-endpoint")
        self.clock = Clock()
        self.key = Ed25519PrivateKey.generate()
        self.signer = GPUAdmissionTokenSigner(active_kid="test", _private_key=self.key)
        self.events = []
        self.gate = "legacy"
        self.epoch = None
        self.horizon_responses = 0
        self.block_reason = None
        self.expired = False
        self.health_mutations = {}
        self.fail_phase = None
        self.replay_challenge = False
        self.complete = False
        self.demote_failure = False

    def residency(self):
        r = self.request
        return {
            "state": "unloaded", "gpu_loaded": False, "active_requests": 0,
            "builders": 0, "borrowers": 0, "draining": False, "evictable": False,
            "generation": None,
            "pools": {p: {"resident": False, "device": None, "provider": None} for p in r.pool_ids},
            "boot_id": r.boot_id, "lifecycle_gate": self.gate, "control_epoch": self.epoch,
            "identity": {"audience": "aap-gpu-lifecycle", "backend_registry_id": str(r.registry_id),
                         "gpu_resource_id": r.resource_id},
        }

    async def open_owned(self, registry_id, *, request_id, purpose, binding):
        self.events.append(("open", purpose))
        return self.endpoint

    async def health_meta(self, endpoint, *, request_id, gpu_health_challenge, timeout_seconds):
        index = sum(event[0] == "health" for event in self.events)
        self.events.append(("health", gpu_health_challenge))
        residency = self.residency()
        residency.update(self.health_mutations.get(index, {}))
        echo = "replayed" if self.replay_challenge else gpu_health_challenge
        return True, {GPU_HEALTH_CHALLENGE_ECHO_MARKER: echo, "residency": residency}

    @asynccontextmanager
    async def transaction(self):
        self.events.append(("begin",))
        try:
            yield self
        except BaseException:
            self.events.append(("rollback",))
            raise
        else:
            self.events.append(("commit",))

    async def step(self, operation, payload):
        assert operation == "installation_control"
        self.events.append(("native", payload))
        assert payload["request"]["registry_id"] == str(self.request.registry_id)
        assert payload["request"]["boot_id"] == self.request.boot_id
        assert datetime.fromisoformat(payload["probe_started_at"]).timestamp() <= self.clock.time()
        if self.block_reason:
            return {"status": "blocked", "reason": self.block_reason}
        if self.complete:
            return {"status": "complete", "preparation": {"control_epoch": "8"}}
        if self.horizon_responses:
            self.horizon_responses -= 1
            return {"status": "blocked", "reason": "control_token_horizon"}
        expiry = payload["probe_started_at"] if self.expired else payload["token_expires_at"]
        return {"status": "prepared", "preparation": {
            "control_epoch": "7" if payload["phase"] == "reset" else "8",
            "token_expires_at": expiry,
        }}

    @asynccontextmanager
    async def control_binding(self, registry_id, *, request_id, operation):
        self.events.append(("binding", operation))
        yield object()

    def verify(self, phase, request, token):
        claims = verify_admission_token(token, keyring={"test": self.key.public_key()})
        self.events.append((phase, claims))
        assert claims.backend_registry_id == str(self.request.registry_id)
        assert claims.gpu_resource_id == self.request.resource_id
        assert claims.boot_id == self.request.boot_id
        assert claims.control_epoch == request.control_epoch
        assert claims.scope.value == ("reset" if phase == "reset" else "mode")
        assert claims.operation == f"installation:{'reset' if phase == 'reset' else 'mode_enforce'}"
        assert claims.owner == f"installation:{self.request.operation_id}"
        assert claims.generation is None
        if self.fail_phase == phase:
            raise RuntimeError("control unavailable")
        self.epoch = request.control_epoch

    async def lifecycle_reset(self, endpoint, request, token, *, request_id, timeout_seconds):
        self.verify("reset", request, token)
        self.gate = "legacy"
        return LifecycleResetResponse.model_validate({
            "ok": True, "control_epoch": self.epoch, "unloaded": True,
            "unloaded_count": 0, "residency": self.residency(),
        })

    async def lifecycle_mode(self, endpoint, request, token, *, request_id, timeout_seconds):
        self.verify("mode", request, token)
        assert request.gate.value == "enforce"
        self.gate = "enforce"
        return LifecycleModeResponse.model_validate({
            "ok": True, "gate": "enforce", "control_epoch": self.epoch, "residency": self.residency(),
        })

    async def demote(self, resource_id):
        assert resource_id == self.request.resource_id
        self.events.append(("demote", resource_id))
        if self.demote_failure:
            raise RuntimeError("readiness demotion failed")

    async def bootstrap(self):
        return await bootstrap_gpu_installation(
            self.request, data=self, signer_factory=lambda: self.signer, endpoint_source=self,
            backend_transport=self, clock=self.clock, readiness_demoter=self.demote,
        )


@pytest.fixture
def harness():
    return BootstrapHarness()


@pytest.mark.asyncio
async def test_bootstrap_signs_reset_then_enforce_for_exact_inactive_installation(harness):
    assert await harness.bootstrap() == "8"
    events = [event[0] for event in harness.events]
    assert [event for event in events if event in {"health", "native", "reset", "mode"}] == [
        "health", "native", "reset", "health", "native", "mode", "health",
    ]
    assert [event for event in events if event in {"begin", "native", "demote", "commit", "reset", "mode"}] == [
        "begin", "native", "demote", "commit", "reset",
        "begin", "native", "demote", "commit", "mode",
    ]
    native = [event[1] for event in harness.events if event[0] == "native"]
    assert [item["phase"] for item in native] == ["reset", "mode_enforce"]
    assert native[1]["residency"]["control_epoch"] == "7"
    challenges = [event[1] for event in harness.events if event[0] == "health"]
    assert len(set(challenges)) == 3


@pytest.mark.asyncio
async def test_demotion_failure_rolls_back_preparation_before_control(harness):
    harness.demote_failure = True
    with pytest.raises(RuntimeError, match="readiness demotion failed"):
        await harness.bootstrap()
    events = [event[0] for event in harness.events]
    assert [event for event in events if event in {"begin", "native", "demote", "commit", "rollback", "reset", "mode"}] == [
        "begin", "native", "demote", "rollback",
    ]


def already_enforced_busy(harness):
    harness.complete = True
    harness.gate, harness.epoch = "enforce", "8"
    resident = {
        "state": "resident", "gpu_loaded": True, "generation": "12",
        "evictable": True, "active_requests": 1, "builders": 1, "borrowers": 1,
        "pools": {
            "embeddings": {"resident": True, "device": "cuda:0", "provider": None},
            "models": {"resident": False, "device": None, "provider": None},
        },
    }
    harness.health_mutations = {0: resident, 1: dict(resident)}


@pytest.mark.asyncio
async def test_native_complete_reuses_matching_busy_enforced_installation(harness):
    already_enforced_busy(harness)
    assert await harness.bootstrap() == "8"
    events = [event[0] for event in harness.events]
    assert [event for event in events if event in {"health", "native", "reset", "mode"}] == [
        "health", "native", "health",
    ]
    assert harness.events[events.index("native")][1]["residency"]["active_requests"] == 1
    assert "demote" not in events
    challenges = [event[1] for event in harness.events if event[0] == "health"]
    assert len(challenges) == 2 and challenges[0] != challenges[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"boot_id": "wrong"}, {"control_epoch": "9"}, {"lifecycle_gate": "legacy"},
    {"draining": True}, {"pools": {"models": {"resident": True}}},
    {"gpu_loaded": False}, {"identity": {"audience": "other"}},
])
async def test_native_complete_still_requires_coherent_fresh_same_identity(harness, mutation):
    already_enforced_busy(harness)
    harness.health_mutations[1].update(mutation)
    with pytest.raises(ValueError):
        await harness.bootstrap()
    assert not any(event[0] in {"reset", "mode"} for event in harness.events)


@pytest.mark.asyncio
async def test_horizon_wait_reprobes_before_authorizing_reset(harness):
    harness.horizon_responses = 2
    assert await harness.bootstrap() == "8"
    native = [event[1] for event in harness.events if event[0] == "native"]
    assert [item["phase"] for item in native] == ["reset", "reset", "reset", "mode_enforce"]
    assert [item["probe_started_at"] for item in native[:3]] == sorted({item["probe_started_at"] for item in native[:3]})
    challenges = [event[1] for event in harness.events if event[0] == "health"]
    assert len(challenges) == len(set(challenges)) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stale_health", "registry_active", "identity_changed"])
async def test_native_rejection_never_sends_signed_control(harness, reason):
    harness.block_reason = reason
    with pytest.raises(ValueError, match="control unavailable"):
        await harness.bootstrap()
    assert not any(event[0] in {"reset", "mode"} for event in harness.events)


@pytest.mark.asyncio
async def test_expired_native_preparation_is_not_signed(harness):
    harness.expired = True
    with pytest.raises(ValueError, match="token expired"):
        await harness.bootstrap()
    assert not any(event[0] in {"reset", "mode"} for event in harness.events)


@pytest.mark.asyncio
async def test_horizon_timeout_never_sends_control(harness):
    harness.horizon_responses = 1000
    with pytest.raises(ValueError, match="control unavailable"):
        await harness.bootstrap()
    assert harness.clock.elapsed == 240.0
    assert not any(event[0] in {"reset", "mode"} for event in harness.events)


@pytest.mark.asyncio
async def test_unmatched_challenge_never_reaches_native_preparation(harness):
    harness.replay_challenge = True
    with pytest.raises(ValueError, match="health challenge failed"):
        await harness.bootstrap()
    assert not any(event[0] in {"native", "reset", "mode"} for event in harness.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["reset", "mode"])
async def test_control_failure_does_not_report_bootstrap_success(harness, phase):
    harness.fail_phase = phase
    with pytest.raises(RuntimeError, match="control unavailable"):
        await harness.bootstrap()
    if phase == "reset":
        assert not any(event[0] == "mode" for event in harness.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"boot_id": "wrong"}, {"control_epoch": "7"}, {"lifecycle_gate": "legacy"},
    {"pools": {"models": {}}}, {"identity": {"audience": "other"}},
    {"state": "resident"}, {"gpu_loaded": True}, {"generation": "1"},
    {"evictable": True}, {"draining": True}, {"active_requests": 1},
    {"builders": 1}, {"borrowers": 1},
    {"pools": {"embeddings": {"resident": True}, "models": {"resident": False}}},
])
async def test_final_health_must_match_enforced_process_and_all_pools(harness, mutation):
    harness.health_mutations[2] = mutation
    with pytest.raises(ValueError, match="live identity mismatch"):
        await harness.bootstrap()
    assert any(event[0] == "mode" for event in harness.events)

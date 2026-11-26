"""D1 direct inference plugin registration, probe, GPU collision, asset bypass, and retirement."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from yoao_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from yoao_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    GPUResourceSettings,
)
from yoao_inference_contract.plugin import (
    ManagedModelCompatibility,
    PluginResourceRequirements,
    RegisterDirectPluginRequest,
)
from yoao_inference_contract.plugin_assets import (
    PluginAssetApprovalState,
    plugin_asset_reason,
    plugin_requires_asset_approval,
)
from yoao_inference_contract.routing import RoutingEpochView
from yoao_protocol_v2.version import PROTOCOL_VERSION

from app.config import settings
from app.services import training_adapters
from app.services.inference_topology_data import InferenceTopologyData
from app.services.plugin_installation_observation import (
    PluginLocalGPUCollision,
    PluginObservationRejected,
    derive_direct_plugin_observation,
)

DIRECT_URL = "/api/v1/admin/ml-integrations/plugins/direct"
INSTALLATIONS_URL = "/api/v1/admin/ml-integrations/plugins/installations"


def _direct_health_and_setup(
    *,
    challenge: str,
    device: str = "cpu",
    device_uuid: str | None = None,
    boot_id: str = "direct-boot-1",
    compat: list[dict] | None = None,
    managed_evaluation: bool = False,
    terminal_drain: bool = True,
    asset_manifest_sha256: str | None = None,
):
    logical_device = "cuda:0" if device == "cuda" else "cpu"
    profile_device = "gpu" if device == "cuda" else "cpu"
    health = {
        GPU_HEALTH_CHALLENGE_ECHO_MARKER: challenge,
        "compute": {
            "configured_device": logical_device,
            "effective_device": logical_device if device == "cuda" else None,
            "cpu_fallback_supported": False,
        },
        "gpu_info": {"device_uuid": device_uuid} if device == "cuda" else None,
        "pool_topology": {"default": {"cap": 2}},
        "residency": {
            "state": "unloaded",
            "gpu_loaded": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "evictable": False,
            "generation": None,
            "pools": {"default": {"resident": False, "device": None, "provider": None}},
            "boot_id": boot_id,
            "lifecycle_gate": "legacy",
            "control_epoch": None,
            "identity": None,
        },
    }
    setup = {
        "protocol_version": PROTOCOL_VERSION,
        "plugin_identity": {"plugin_id": "custom.segmenter", "version": "2.1.0"},
        "runtime_info": {"device": device},
        "managed_model_compatibility": compat or [],
        "managed_evaluation": managed_evaluation,
        "asset_manifest_sha256": asset_manifest_sha256,
        "pool_topology": {"default": {"cap": 2}},
        "terminal_lifecycle": (
            {"terminal_drain_endpoint": "/lifecycle/terminal-drain"}
            if terminal_drain
            else {}
        ),
        "models": [
            {
                "id": "seg-v1",
                "task": "segment",
                "is_interactive": True,
                "supported_prompts": ["point", "bbox"],
                "supported_inputs": ["full_image"],
                "supported_geometric_outputs": ["mask"],
                "resource_profile": {"device": profile_device},
            }
        ],
    }
    return health, setup


def test_direct_manifest_derivation_approves_subset_and_blocks_local_gpu_collision():
    challenge = "a" * 64
    local_uuid = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    remote_uuid = "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    gpu_cfg = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.ENFORCE,
        gpu_arbiter_rollout_enabled=True,
        gpu_arbiter_resources={
            "node-1/gpu-0": GPUResourceSettings(
                resource_id="node-1/gpu-0",
                node_id="node-1",
                physical_device_token=local_uuid,
                allocatable_mb=16000,
                mode=GPUArbiterMode.ENFORCE,
            )
        },
    )
    compat_advertised = [
        {"framework": "custom_seg", "format": None, "runtime_device": "cuda"},
        {"framework": "other_fw", "format": "onnx", "runtime_device": "cuda"},
    ]
    health, setup = _direct_health_and_setup(
        challenge=challenge,
        device="cuda",
        device_uuid=remote_uuid,
        compat=compat_advertised,
        managed_evaluation=True,
    )
    req = RegisterDirectPluginRequest(
        name="Remote Seg GPU",
        url="https://gpu-plugin.example.test:8443",
        auth_token="direct-secret-" + "x" * 32,
        source_tier="official",
        display_name="Remote Segmenter",
        approved_managed_model_compatibility=[
            ManagedModelCompatibility(
                framework="custom_seg", format=None, runtime_device="cuda"
            )
        ],
        approved_managed_evaluation=True,
    )
    evidence = derive_direct_plugin_observation(
        req,
        endpoint=req.url,
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup,
        gpu_config=gpu_cfg,
    )
    manifest = evidence["manifest"]
    assert manifest["execution_mode"] == "direct"
    assert manifest["artifact"] is None
    assert manifest["asset_manifest_sha256"] is None
    assert manifest["resource"]["device"] == "cuda"
    assert manifest["resource"]["gpu_memory_mb"] == 0
    assert evidence["resource"] == {
        "gpu_resource_id": None,
        "physical_gpu_uuid": None,
        "logical_device": "cuda:0",
    }
    assert set(manifest["capabilities"]) == {
        "inference",
        "segmentation",
        "interactive",
        "managed_models",
        "managed_evaluation",
    }
    assert manifest["managed_model_compatibility"] == [
        {"framework": "custom_seg", "format": None, "runtime_device": "cuda"}
    ]

    extra_params = {
        "plugin": {
            "installation_id": str(uuid4()),
            "manifest": manifest,
            "resource": evidence["resource"],
            "status": "active",
            "revision": 2,
            "execution_mode": "direct",
            "source_tier": "official",
        }
    }
    assert plugin_requires_asset_approval(extra_params) is False
    assert (
        plugin_asset_reason(
            extra_params,
            PluginAssetApprovalState(revision=0, decision=None).model_dump(mode="json"),
            "seg-v1",
            {},
        )
        is None
    )

    backend_obj = SimpleNamespace(
        extra_params=extra_params,
        health_meta={
            "capabilities": {
                "managed_evaluation": True,
                "managed_model_compatibility": compat_advertised,
            }
        },
    )
    assert (
        training_adapters.supports_managed_evaluation("custom_seg", backend=backend_obj)
        is True
    )
    assert (
        training_adapters.supports_managed_evaluation("other_fw", backend=backend_obj)
        is False
    )

    colliding_health = deepcopy(health)
    colliding_health["gpu_info"]["device_uuid"] = local_uuid
    with pytest.raises(PluginLocalGPUCollision):
        derive_direct_plugin_observation(
            req,
            endpoint=req.url,
            challenge=challenge,
            healthy=True,
            health=colliding_health,
            setup=setup,
            gpu_config=gpu_cfg,
        )

    unadvertised_eval_setup = deepcopy(setup)
    unadvertised_eval_setup["managed_evaluation"] = False
    with pytest.raises(PluginObservationRejected):
        derive_direct_plugin_observation(
            req,
            endpoint=req.url,
            challenge=challenge,
            healthy=True,
            health=health,
            setup=unadvertised_eval_setup,
            gpu_config=gpu_cfg,
        )

    asset_sha = "d" * 64
    req_with_asset = req.model_copy(update={"asset_manifest_sha256": asset_sha})
    with pytest.raises(PluginObservationRejected):
        derive_direct_plugin_observation(
            req_with_asset,
            endpoint=req.url,
            challenge=challenge,
            healthy=True,
            health=health,
            setup=setup,
            gpu_config=gpu_cfg,
        )
    setup_with_asset = {**setup, "asset_manifest_sha256": asset_sha}
    derived_with_asset = derive_direct_plugin_observation(
        req_with_asset,
        endpoint=req.url,
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup_with_asset,
        gpu_config=gpu_cfg,
    )
    assert derived_with_asset["manifest"]["asset_manifest_sha256"] == asset_sha
    assert derived_with_asset["terminal_drain_supported"] is True


@pytest.mark.asyncio
async def test_direct_plugin_api_lifecycle_ssrf_rollback_and_read_only_probe(
    httpx_client, super_admin, db_session, monkeypatch
):
    from app.api.v1 import admin_plugin_installations as routes
    from app.services import plugin_installation_observation as obs_mod
    from app.services import trained_models

    _, admin_jwt = super_admin
    super_admin_headers = {"Authorization": f"Bearer {admin_jwt}"}
    client = httpx_client
    token = "direct-token-" + "z" * 40

    blocked_meta = await client.post(
        DIRECT_URL,
        headers=super_admin_headers,
        json={
            "name": "Metadata SSRF",
            "url": "http://169.254.169.254:8000",
            "auth_token": token,
        },
    )
    assert blocked_meta.status_code == 422
    assert (
        blocked_meta.json()["detail"]["error_code"] == "plugin_installation_host_denied"
    )
    assert token not in blocked_meta.text

    monkeypatch.setattr(settings, "inference_plugin_loopback_host_allowlist", [])
    blocked_loopback = await client.post(
        DIRECT_URL,
        headers=super_admin_headers,
        json={
            "name": "Loopback Blocked",
            "url": "http://127.0.0.1:9911",
            "auth_token": token,
        },
    )
    assert blocked_loopback.status_code == 422
    assert (
        blocked_loopback.json()["detail"]["error_code"]
        == "plugin_installation_host_denied"
    )

    bad_validation = await client.post(
        DIRECT_URL,
        headers=super_admin_headers,
        json={
            "name": "Short Token",
            "url": "http://127.0.0.1:9911",
            "auth_token": "too-short-secret-value",
        },
    )
    assert bad_validation.status_code == 422
    assert "too-short-secret-value" not in bad_validation.text

    monkeypatch.setattr(
        settings, "inference_plugin_loopback_host_allowlist", ["127.0.0.1"]
    )
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        json.dumps(
            {
                "node-a/GPU-99999999-9999-9999-9999-999999999999": {
                    "node_id": "node-a",
                    "physical_device_token": "GPU-99999999-9999-9999-9999-999999999999",
                    "allocatable_mb": 16000,
                    "mode": "enforce",
                }
            }
        ),
    )

    async def allow_reconcile(self, operation, body, *, request_id=None):
        return RoutingEpochView(
            control_id=UUID(body["control_id"]),
            registry_id=UUID(body["registry_id"]),
            phase="completed",
            authority_epoch=uuid4(),
            backend_incarnation=uuid4(),
        )

    monkeypatch.setattr(routes.RoutingAuthority, "execute", allow_reconcile)

    state = {
        "device": "cuda",
        "device_uuid": "GPU-99999999-9999-9999-9999-999999999999",
        "boot_id": "boot-gpu-1",
        "terminal_drain": False,
        "compat": [
            {"framework": "custom_seg", "format": None, "runtime_device": "cuda"}
        ],
        "managed_evaluation": True,
        "healthy": True,
    }

    class FakeHealthClient:
        def __init__(self, _db, _registry):
            self._setup = None

        async def health_meta(self, *, gpu_health_challenge=None):
            if not state["healthy"]:
                return False, None
            health, setup = _direct_health_and_setup(
                challenge=gpu_health_challenge,
                device=state["device"],
                device_uuid=state["device_uuid"],
                boot_id=state["boot_id"],
                compat=state["compat"],
                managed_evaluation=state["managed_evaluation"],
                terminal_drain=state["terminal_drain"],
            )
            self._setup = setup
            return True, health

        async def setup(self):
            if self._setup is None:
                raise RuntimeError("setup unavailable")
            return self._setup

    monkeypatch.setattr(obs_mod, "HealthObservationClient", FakeHealthClient)

    reusable_url = f"http://127.0.0.1:{9100 + (uuid4().int % 500)}"
    collided = await client.post(
        DIRECT_URL,
        headers=super_admin_headers,
        json={
            "name": "Local GPU Spoof",
            "url": reusable_url,
            "auth_token": token,
            "source_tier": "third_party",
        },
    )
    assert collided.status_code == 409
    assert (
        collided.json()["detail"]["error_code"] == "plugin_direct_gpu_local_collision"
    )
    leftover = await db_session.scalar(
        text("SELECT count(*) FROM public.ml_backend_registry WHERE url = :url"),
        {"url": reusable_url},
    )
    assert leftover == 0

    state["device_uuid"] = "GPU-77777777-7777-7777-7777-777777777777"
    registered = await client.post(
        DIRECT_URL,
        headers=super_admin_headers,
        json={
            "name": "Remote Direct GPU",
            "url": reusable_url,
            "auth_token": token,
            "source_tier": "official",
            "display_name": "Remote Direct GPU",
            "description": "Unarbitrated remote CUDA service",
            "approved_managed_model_compatibility": [
                {"framework": "custom_seg", "format": None, "runtime_device": "cuda"}
            ],
            "approved_managed_evaluation": True,
        },
    )
    assert registered.status_code == 200, registered.text
    body = registered.json()
    inst_id = body["installation"]["installation_id"]
    assert body["installation"]["status"] == "active"
    assert body["installation"]["execution_mode"] == "direct"
    assert body["installation"]["source_tier"] == "official"
    assert body["installation"]["revision"] == 2
    assert body["summary"]["extension_point"] == "inference"
    assert body["summary"]["execution_mode"] == "direct"
    assert body["summary"]["source_tier"] == "official"
    assert body["summary"]["locally_arbitrated_gpu"] is False
    assert body["summary"]["device"] == "cuda"

    row = (
        await db_session.execute(
            text(
                "SELECT gpu_resource_id, vram_budget_mb, extra_params "
                "FROM public.ml_backend_registry WHERE id = :id"
            ),
            {"id": UUID(inst_id)},
        )
    ).one()
    assert row.gpu_resource_id is None
    assert row.vram_budget_mb is None
    plugin_before_probe = deepcopy(row.extra_params["plugin"])

    probed = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/probe",
        headers=super_admin_headers,
    )
    assert probed.status_code == 200, probed.text
    probe_body = probed.json()
    assert probe_body["healthy"] is True
    assert probe_body["matches_manifest"] is True
    assert probe_body["observed_managed_evaluation"] is True
    assert probe_body["terminal_drain_supported"] is False
    plugin_after_probe = await db_session.scalar(
        text(
            "SELECT extra_params->'plugin' FROM public.ml_backend_registry WHERE id = :id"
        ),
        {"id": UUID(inst_id)},
    )
    assert plugin_after_probe == plugin_before_probe

    listed = await client.get(INSTALLATIONS_URL, headers=super_admin_headers)
    assert listed.status_code == 200
    assert any(
        item["installation"]["installation_id"] == inst_id
        and item["summary"]["display_name"] == "Remote Direct GPU"
        for item in listed.json()
    )

    disabled = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/disable",
        headers=super_admin_headers,
        json={"expected_revision": 2},
    )
    assert disabled.status_code == 200
    assert disabled.json()["installation"]["status"] == "disabled"
    assert disabled.json()["installation"]["revision"] == 3

    reactivated = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/activate",
        headers=super_admin_headers,
        json={"expected_revision": 3},
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["installation"]["status"] == "active"
    assert reactivated.json()["installation"]["revision"] == 4

    disabled_again = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/disable",
        headers=super_admin_headers,
        json={"expected_revision": 4},
    )
    assert disabled_again.status_code == 200
    assert disabled_again.json()["installation"]["revision"] == 5

    quiesced = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/quiesce",
        headers=super_admin_headers,
        json={"expected_revision": 5},
    )
    assert quiesced.status_code == 200, quiesced.text
    q_body = quiesced.json()
    assert q_body["safe_to_stop"] is True
    assert q_body["launch"] is None
    assert q_body["installation"]["revision"] == 6

    async def busy_usage(_db, _registry_id):
        return SimpleNamespace(active_releases=1, frozen_executions=0)

    monkeypatch.setattr(trained_models, "registry_usage", busy_usage)
    blocked_retire = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/retire",
        headers=super_admin_headers,
        json={"expected_revision": 6},
    )
    assert blocked_retire.status_code == 409
    assert blocked_retire.json()["detail"]["error_code"] == "plugin_installation_in_use"

    async def empty_usage(_db, _registry_id):
        return SimpleNamespace(active_releases=0, frozen_executions=0)

    monkeypatch.setattr(trained_models, "registry_usage", empty_usage)
    retired = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/retire",
        headers=super_admin_headers,
        json={"expected_revision": 6},
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["installation"]["status"] == "retired"
    retired_token = await db_session.scalar(
        text("SELECT auth_token FROM public.ml_backend_registry WHERE id = :id"),
        {"id": UUID(inst_id)},
    )
    assert (
        retired_token
        == "retired-sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    )

    class FailIfProbedClient:
        def __init__(self, _db, _registry):
            raise AssertionError("retired plugin must not open HealthObservationClient")

    monkeypatch.setattr(obs_mod, "HealthObservationClient", FailIfProbedClient)
    retired_probe = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/probe",
        headers=super_admin_headers,
    )
    assert retired_probe.status_code == 409
    assert retired_probe.json()["detail"]["error_code"] == "plugin_installation_retired"


@pytest.mark.asyncio
async def test_direct_plugin_quiesce_with_terminal_drain_endpoint(
    httpx_client, super_admin, db_session, monkeypatch
):
    from app.api.v1 import admin_plugin_installations as routes
    from app.services import plugin_installation_observation as obs_mod
    from app.services import trained_models

    _, admin_jwt = super_admin
    super_admin_headers = {"Authorization": f"Bearer {admin_jwt}"}
    client = httpx_client

    monkeypatch.setattr(
        settings, "inference_plugin_loopback_host_allowlist", ["127.0.0.1"]
    )

    async def allow_reconcile(self, operation, body, *, request_id=None):
        return RoutingEpochView(
            control_id=UUID(body["control_id"]),
            registry_id=UUID(body["registry_id"]),
            phase="completed",
            authority_epoch=uuid4(),
            backend_incarnation=uuid4(),
        )

    monkeypatch.setattr(routes.RoutingAuthority, "execute", allow_reconcile)

    class FakeHealthClient:
        def __init__(self, _db, _registry):
            self._setup = None

        async def health_meta(self, *, gpu_health_challenge=None):
            health, setup = _direct_health_and_setup(
                challenge=gpu_health_challenge,
                device="cpu",
                boot_id="cpu-boot-99",
                terminal_drain=True,
            )
            self._setup = setup
            return True, health

        async def setup(self):
            return self._setup

    monkeypatch.setattr(obs_mod, "HealthObservationClient", FakeHealthClient)

    drain_calls = []

    class FakeControl:
        async def terminal_drain(self, registry_id, *, boot_id, operation_id):
            drain_calls.append((registry_id, boot_id, operation_id))
            return {
                "registry_id": str(registry_id),
                "boot_id": boot_id,
                "operation_id": str(operation_id),
                "control_epoch": None,
                "generation": None,
                "pool_ids": ["default"],
                "unloaded": True,
            }

    monkeypatch.setattr(routes, "backend_control_service", lambda _db: FakeControl())

    url = f"http://127.0.0.1:{9600 + (uuid4().int % 300)}"
    reg = await client.post(
        DIRECT_URL,
        headers=super_admin_headers,
        json={
            "name": "Local CPU Direct",
            "url": url,
            "auth_token": "cpu-secret-" + "a" * 32,
        },
    )
    assert reg.status_code == 200, reg.text
    inst_id = reg.json()["installation"]["installation_id"]

    dis = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/disable",
        headers=super_admin_headers,
        json={"expected_revision": 2},
    )
    assert dis.status_code == 200

    q = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/quiesce",
        headers=super_admin_headers,
        json={"expected_revision": 3},
    )
    assert q.status_code == 200, q.text
    assert len(drain_calls) == 1
    assert q.json()["launch"] is None

    reactivate_same_boot = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/activate",
        headers=super_admin_headers,
        json={"expected_revision": 4},
    )
    assert reactivate_same_boot.status_code == 409
    await db_session.rollback()

    async def empty_usage(_db, _registry_id):
        return SimpleNamespace(active_releases=0, frozen_executions=0)

    monkeypatch.setattr(trained_models, "registry_usage", empty_usage)
    ret = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/retire",
        headers=super_admin_headers,
        json={"expected_revision": 4},
    )
    assert ret.status_code == 200
    assert ret.json()["installation"]["status"] == "retired"


@pytest.mark.asyncio
async def test_sql_0513_security_invariants_uses_gpu_and_container_guards_preserved(
    db_session,
):
    from uuid import uuid5

    for sig in (
        "yoao_inference_topology_v1.prepare_plugin_installation(text)",
        "yoao_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)",
        "yoao_inference_topology_v1.authorize_plugin_routing_replacement(uuid,integer,uuid,text,text,text)",
        "yoao_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text)",
        "yoao_inference_routing_v1.prediction_scope_reserve(text)",
        "yoao_inference_routing_v1.prediction_test_scope_reserve(text)",
    ):
        row = (
            await db_session.execute(
                text(
                    "SELECT prosecdef, proconfig, "
                    "has_function_privilege('public', oid, 'EXECUTE') AS public_exec "
                    "FROM pg_proc WHERE oid = CAST(:sig AS regprocedure)"
                ),
                {"sig": sig},
            )
        ).one()
        assert row.prosecdef is True, sig
        assert row.public_exec is False, sig
        assert "search_path=pg_catalog, pg_temp" in (row.proconfig or []), sig

    topology = InferenceTopologyData(db_session)
    direct_id = uuid4()
    direct_req = RegisterDirectPluginRequest(
        installation_id=direct_id,
        name=f"Direct SQL {direct_id}",
        url=f"http://direct-{direct_id}.invalid",
        auth_token="sql-direct-" + "b" * 32,
        source_tier="official",
    )
    reserved = await topology.prepare_direct_plugin_installation(direct_id, direct_req)
    assert reserved.installation.execution_mode == "direct"
    assert reserved.installation.source_tier == "official"

    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await topology.command_plugin_installation(
                direct_id,
                1,
                "record_launch",
                {
                    "installation_id": str(direct_id),
                    "registry_id": str(direct_id),
                    "artifact": "example/x@sha256:" + "a" * 64,
                    "image_id": "sha256:" + "b" * 64,
                    "container_id": "c" * 64,
                    "resource": reserved.installation.resource.model_dump(mode="json"),
                },
            )

    remote_manifest = reserved.installation.manifest.model_copy(
        update={
            "plugin_id": "remote.gpu.plugin",
            "version": "1.0.0",
            "resource": PluginResourceRequirements(
                device="cuda", memory_mb=None, gpu_memory_mb=0
            ),
        }
    )
    activated = await topology.command_plugin_installation(
        direct_id,
        1,
        "activate",
        {
            "endpoint": direct_req.url,
            "boot_id": "remote-gpu-boot-1",
            "control_epoch": None,
            "terminal_drain_supported": True,
            "manifest": remote_manifest.model_dump(mode="json"),
            "resource": {
                "gpu_resource_id": None,
                "physical_gpu_uuid": None,
                "logical_device": "cuda:0",
            },
        },
    )
    assert activated.installation.status == "active"
    assert activated.installation.manifest.resource.device == "cuda"

    uses_gpu = await db_session.scalar(
        text(
            "SELECT gpu_resource_id IS NOT NULL "
            "OR vram_budget_mb IS NOT NULL "
            "OR (COALESCE(extra_params #>> '{plugin,execution_mode}', 'container') <> 'direct' "
            "AND COALESCE(extra_params #>> '{plugin,manifest,resource,device}', 'cpu') <> 'cpu') "
            "FROM public.ml_backend_registry WHERE id = :id"
        ),
        {"id": direct_id},
    )
    assert uses_gpu is False

    disabled = await topology.command_plugin_installation(direct_id, 2, "disable")
    assert disabled.installation.status == "disabled"

    with pytest.raises(ValueError, match="direct plugin requires terminal drain proof"):
        async with db_session.begin_nested():
            await topology.command_plugin_installation(
                direct_id,
                3,
                "quiesce",
                {
                    "endpoint": direct_req.url,
                    "boot_id": "remote-gpu-boot-1",
                    "control_epoch": None,
                    "generation": None,
                    "operation_id": str(uuid4()),
                    "pool_ids": ["default"],
                    "terminal_drain_supported": False,
                },
            )

    with pytest.raises(ValueError, match="pool member must be drained"):
        async with db_session.begin_nested():
            await topology.command_plugin_installation(
                direct_id,
                3,
                "quiesce",
                {
                    "endpoint": direct_req.url,
                    "boot_id": "remote-gpu-boot-2",
                    "control_epoch": None,
                    "generation": None,
                    "operation_id": str(uuid4()),
                    "pool_ids": ["default"],
                    "terminal_drain_supported": True,
                },
            )

    pool = await topology.pool_for_registry(direct_id)
    assert pool is not None
    assert await topology.drain_member(pool.id, direct_id) is not None

    quiesced_after_restart = await topology.command_plugin_installation(
        direct_id,
        3,
        "quiesce",
        {
            "endpoint": direct_req.url,
            "boot_id": "remote-gpu-boot-2",
            "control_epoch": None,
            "generation": None,
            "operation_id": str(uuid4()),
            "pool_ids": ["default"],
            "terminal_drain_supported": True,
        },
    )
    assert quiesced_after_restart.installation.revision == 4
    stopped_boots = await db_session.scalar(
        text(
            "SELECT extra_params #> '{plugin_lifecycle,stopped}' "
            "FROM public.ml_backend_registry WHERE id = :id"
        ),
        {"id": direct_id},
    )
    assert {entry["boot_id"] for entry in stopped_boots} == {
        "remote-gpu-boot-1",
        "remote-gpu-boot-2",
    }

    old_incarnation = uuid4()
    await db_session.execute(
        text(
            "UPDATE yoao_inference_routing_v1.registry_state "
            "SET backend_incarnation = :inc, continuity = 'ready' "
            "WHERE registry_id = :id"
        ),
        {"inc": old_incarnation, "id": direct_id},
    )
    stopped_control_id = uuid5(
        direct_id, "plugin:bind-routing:direct:remote-gpu-boot-1"
    )
    with pytest.raises(ValueError, match="replacement_unproven"):
        await topology.authorize_plugin_routing_replacement(
            direct_id,
            4,
            stopped_control_id,
            endpoint=direct_req.url,
            container_id="direct",
            boot_id="remote-gpu-boot-1",
        )

    new_control_id = uuid5(direct_id, "plugin:bind-routing:direct:remote-gpu-boot-3")
    await topology.authorize_plugin_routing_replacement(
        direct_id,
        4,
        new_control_id,
        endpoint=direct_req.url,
        container_id="direct",
        boot_id="remote-gpu-boot-3",
    )
    control_row = (
        await db_session.execute(
            text(
                "SELECT phase, backend_incarnation "
                "FROM yoao_inference_routing_v1.controls WHERE id = :cid"
            ),
            {"cid": new_control_id},
        )
    ).one()
    assert control_row.phase == "prepared"
    assert control_row.backend_incarnation is None

    fn_def = await db_session.scalar(
        text(
            "SELECT pg_get_functiondef("
            "'yoao_inference_routing_v1.prediction_scope_reserve(text)'::regprocedure)"
        )
    )
    assert "execution_mode" in fn_def
    assert "'direct'" in fn_def
    test_fn_def = await db_session.scalar(
        text(
            "SELECT pg_get_functiondef("
            "'yoao_inference_routing_v1.prediction_test_scope_reserve(text)'::regprocedure)"
        )
    )
    assert "execution_mode" in test_fn_def
    assert "'direct'" in test_fn_def


@pytest.mark.asyncio
async def test_direct_plugin_audit_regressions(
    httpx_client, super_admin, db_session, monkeypatch
):
    from fastapi import HTTPException
    from yoao_inference_contract.routing import RoutingUnavailable
    from app.api.v1 import admin_plugin_installations as routes
    from app.services import plugin_installation_observation as obs_mod

    _, admin_jwt = super_admin
    headers = {"Authorization": f"Bearer {admin_jwt}"}
    client = httpx_client
    monkeypatch.setattr(
        settings, "inference_plugin_loopback_host_allowlist", ["127.0.0.1"]
    )

    state = {
        "boot_id": "drain-boot-1",
        "terminal_drain": True,
        "fail_503": False,
        "host_denied": False,
    }

    class FakeHealthClient:
        def __init__(self, _db, _registry):
            self._setup = None

        async def health_meta(self, *, gpu_health_challenge=None):
            if state["fail_503"]:
                raise HTTPException(
                    status_code=503,
                    detail={"error_code": "inference_health_observation_unavailable"},
                )
            if state["host_denied"]:
                raise RuntimeError("inference_host_denied: blocked by policy")
            health, setup = _direct_health_and_setup(
                challenge=gpu_health_challenge,
                device="cpu",
                boot_id=state["boot_id"],
                terminal_drain=state["terminal_drain"],
            )
            self._setup = setup
            return True, health

        async def setup(self):
            return self._setup

    monkeypatch.setattr(obs_mod, "HealthObservationClient", FakeHealthClient)

    reconcile_attempts = {"count": 0}

    async def flaky_reconcile(self, operation, body, *, request_id=None):
        reconcile_attempts["count"] += 1
        if reconcile_attempts["count"] == 1:
            raise RoutingUnavailable()
        return RoutingEpochView(
            control_id=UUID(body["control_id"]),
            registry_id=UUID(body["registry_id"]),
            phase="completed",
            authority_epoch=uuid4(),
            backend_incarnation=uuid4(),
        )

    monkeypatch.setattr(routes.RoutingAuthority, "execute", flaky_reconcile)

    reusable_url = f"http://127.0.0.1:{9300 + (uuid4().int % 250)}"
    token = "audit-direct-" + "k" * 32
    first = await client.post(
        DIRECT_URL,
        headers=headers,
        json={"name": "Flaky Direct", "url": reusable_url, "auth_token": token},
    )
    assert first.status_code == 503

    retried = await client.post(
        DIRECT_URL,
        headers=headers,
        json={"name": "Flaky Direct Retry", "url": reusable_url, "auth_token": token},
    )
    assert retried.status_code == 200, retried.text
    inst_id = retried.json()["installation"]["installation_id"]

    state["host_denied"] = True
    denied_probe = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/probe", headers=headers
    )
    assert denied_probe.status_code == 200
    assert denied_probe.json()["healthy"] is False
    assert "inference_host_denied" in (denied_probe.json()["mismatch_reason"] or "")
    state["host_denied"] = False

    dis = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/disable",
        headers=headers,
        json={"expected_revision": 2},
    )
    assert dis.status_code == 200

    state["host_denied"] = True
    denied_activate = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/activate",
        headers=headers,
        json={"expected_revision": 3},
    )
    assert denied_activate.status_code == 422
    assert (
        denied_activate.json()["detail"]["error_code"]
        == "plugin_installation_host_denied"
    )
    state["host_denied"] = False

    state["fail_503"] = True
    quiesce_503 = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/quiesce",
        headers=headers,
        json={"expected_revision": 3},
    )
    assert quiesce_503.status_code == 503
    state["fail_503"] = False

    drain_calls = []

    class FakeControl:
        async def terminal_drain(self, registry_id, *, boot_id, operation_id):
            drain_calls.append(boot_id)
            return {
                "registry_id": str(registry_id),
                "boot_id": boot_id,
                "operation_id": str(operation_id),
                "control_epoch": None,
                "generation": None,
                "pool_ids": ["default"],
                "unloaded": True,
            }

    monkeypatch.setattr(routes, "backend_control_service", lambda _db: FakeControl())
    state["boot_id"] = "drain-boot-restarted"
    quiesced = await client.post(
        f"{INSTALLATIONS_URL}/{inst_id}/quiesce",
        headers=headers,
        json={"expected_revision": 3},
    )
    assert quiesced.status_code == 200, quiesced.text
    assert drain_calls == ["drain-boot-restarted"]
    stopped_boots = await db_session.scalar(
        text(
            "SELECT extra_params #> '{plugin_lifecycle,stopped}' "
            "FROM public.ml_backend_registry WHERE id = :id"
        ),
        {"id": UUID(inst_id)},
    )
    assert {entry["boot_id"] for entry in stopped_boots} == {
        "drain-boot-1",
        "drain-boot-restarted",
    }

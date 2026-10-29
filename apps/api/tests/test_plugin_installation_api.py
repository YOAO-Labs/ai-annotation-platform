"""Installation API authorization, durable reservation and live-fact validation."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference_contract.plugin import (
    PluginInstallationResponse,
    PluginLaunchReceipt,
)
from aap_inference_contract.routing import RoutingEpochView, RoutingUnavailable
from aap_protocol_v2.lifecycle import ManagedLifecycleCapabilities
from aap_protocol_v2.version import PROTOCOL_VERSION
from app.services.plugin_installation_observation import (
    PluginObservationRejected,
    verify_endpoint_observation,
)

pytestmark = pytest.mark.asyncio
BASE = "/api/v1/admin/ml-integrations/plugins/installations"


def allow_routing_binding(monkeypatch):
    from app.api.v1 import admin_plugin_installations as routes

    async def reconcile(self, operation, body, *, request_id=None):
        return RoutingEpochView(
            control_id=UUID(body["control_id"]),
            registry_id=UUID(body["registry_id"]),
            phase="completed",
            authority_epoch=uuid4(),
            backend_incarnation=uuid4(),
        )

    monkeypatch.setattr(routes.RoutingAuthority, "execute", reconcile)


def payload(installation_id):
    return {
        "installation_id": str(installation_id),
        "name": "Test detector",
        "url": "http://detector.example.test:8000",
        "auth_token": "secret-token-" + "x" * 40,
        "manifest": {
            "plugin_id": "test.detector",
            "version": "1.0",
            "publisher": "example",
            "license": "MIT",
            "artifact": "example.test/detector@sha256:" + "a" * 64,
            "sdk_compatibility": "1",
            "entrypoint": ["detector"],
            "capabilities": ["inference", "detection"],
            "resource": {"device": "cpu", "memory_mb": 512, "gpu_memory_mb": 0},
            "asset_manifest_sha256": "b" * 64,
            "managed_model_compatibility": [],
            "allowed_asset_sources": [],
        },
        "resource": {
            "gpu_resource_id": None,
            "physical_gpu_uuid": None,
            "logical_device": "cpu",
        },
    }


def observation():
    ident = uuid4()
    data = payload(ident)
    installation = PluginInstallationResponse.model_validate(
        {
            "registry_id": ident,
            "installation": {
                "installation_id": ident,
                "manifest": data["manifest"],
                "status": "prepared",
                "revision": 1,
                "resource": data["resource"],
            },
        }
    )
    receipt = PluginLaunchReceipt.model_validate(
        {
            "installation_id": ident,
            "registry_id": ident,
            "artifact": data["manifest"]["artifact"],
            "image_id": "sha256:" + "c" * 64,
            "container_id": "d" * 64,
            "resource": data["resource"],
        }
    )
    challenge = "e" * 64
    health = {
        GPU_HEALTH_CHALLENGE_ECHO_MARKER: challenge,
        "compute": {
            "configured_device": "cpu",
            "effective_device": None,
            "cpu_fallback_supported": False,
        },
        "gpu_info": {"device_uuid": None},
        "residency": {
            "state": "unloaded",
            "gpu_loaded": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "draining": False,
            "evictable": False,
            "generation": None,
            "pools": {"models": {"resident": False, "device": None, "provider": None}},
            "boot_id": "runtime-boot",
            "lifecycle_gate": "legacy",
            "control_epoch": None,
            "identity": None,
        },
    }
    setup = {
        "protocol_version": PROTOCOL_VERSION,
        "plugin_identity": {"plugin_id": "test.detector", "version": "1.0"},
        "registry_id": str(ident),
        "gpu_resource_id": None,
        "physical_gpu_uuid": None,
        "runtime_info": {"device": "cpu"},
        "managed_model_compatibility": [],
        "managed_lifecycle": ManagedLifecycleCapabilities().model_dump(mode="json"),
        "terminal_lifecycle": {"terminal_drain_endpoint": "/lifecycle/terminal-drain"},
        "models": [
            {
                "id": "detect",
                "task": "detect",
                "is_interactive": False,
                "supported_inputs": ["full_image", "crop"],
                "supported_geometric_outputs": ["bbox"],
                "resource_profile": {"device": "cpu"},
            }
        ],
    }
    return installation, receipt, challenge, health, setup


async def test_endpoint_observation_rejects_changed_or_untrusted_facts():
    installation, receipt, challenge, health, setup = observation()
    kwargs = dict(
        endpoint="http://detector.example.test:8000",
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup,
    )
    evidence = verify_endpoint_observation(installation, receipt, **kwargs)
    assert evidence["boot_id"] == "runtime-boot"
    assert evidence["container_id"] == receipt.container_id
    assert "auth_token" not in evidence

    for mutate in (
        lambda h, s: h.update({GPU_HEALTH_CHALLENGE_ECHO_MARKER: "f" * 64}),
        lambda h, s: h["compute"].update({"effective_device": "cuda:0"}),
        lambda h, s: h["residency"].update({"state": "unknown"}),
        lambda h, s: s["plugin_identity"].update({"version": "other"}),
        lambda h, s: s["models"][0].update({"supported_inputs": ["full_image"]}),
        lambda h, s: s.update({"physical_gpu_uuid": "GPU-unapproved"}),
        lambda h, s: s.update({"terminal_lifecycle": {}}),
    ):
        changed_health, changed_setup = deepcopy(health), deepcopy(setup)
        mutate(changed_health, changed_setup)
        with pytest.raises(PluginObservationRejected):
            verify_endpoint_observation(
                installation,
                receipt,
                **{**kwargs, "health": changed_health, "setup": changed_setup},
            )
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation,
            receipt.model_copy(
                update={"container_id": receipt.container_id, "artifact": "wrong"}
            ),
            **kwargs,
        )


async def test_prepare_requires_super_admin_and_replay_matches_intent(
    httpx_client, super_admin, project_admin, db_session
):
    _, admin_token = super_admin
    _, project_token = project_admin
    body = payload(uuid4())
    forbidden = await httpx_client.post(
        BASE, json=body, headers={"Authorization": f"Bearer {project_token}"}
    )
    assert forbidden.status_code == 403
    headers = {"Authorization": f"Bearer {admin_token}"}
    first = await httpx_client.post(BASE, json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["installation"]["status"] == "prepared"
    assert body["auth_token"] not in first.text
    replay = await httpx_client.post(
        BASE, json={**body, "url": body["url"] + "/"}, headers=headers
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    different = {**body, "auth_token": "changed-" + "y" * 40}
    conflict = await httpx_client.post(BASE, json=different, headers=headers)
    assert conflict.status_code == 409
    assert body["auth_token"] not in conflict.text
    await db_session.rollback()
    listing = await httpx_client.get(BASE, headers=headers)
    assert listing.status_code == 200 and any(
        item["registry_id"] == first.json()["registry_id"] for item in listing.json()
    )
    fetched = await httpx_client.get(
        BASE + "/" + body["installation_id"], headers=headers
    )
    assert fetched.status_code == 200 and fetched.json() == first.json()


async def test_gpu_activation_checks_observed_physical_device_and_runtime():
    import hashlib
    import json
    from aap_backend_runtime.lifecycle_evidence import (
        REQUIRED_CONTRACT_CHECKS,
        build_managed_lifecycle_evidence,
        memory_cycle_evidence,
    )
    from aap_inference_contract.plugin import PluginLifecycleQualification
    from training_contract.inference_runtime import LIBRE_CUDA_RUNTIME

    original, receipt, challenge, health, setup = observation()
    model = original.model_dump(mode="json")
    manifest = model["installation"]["manifest"]
    manifest.update(
        plugin_id="libreyolo",
        capabilities=["inference", "detection", "managed_models"],
        resource={"device": "cuda", "memory_mb": 1024, "gpu_memory_mb": 512},
        managed_model_compatibility=[
            {
                "framework": "libreyolo",
                "format": "libreyolo-native-state-dict-1.0",
                "runtime_device": "cuda",
            }
        ],
    )
    resource = {
        "gpu_resource_id": "node/GPU-123",
        "physical_gpu_uuid": "GPU-123",
        "logical_device": "cuda:0",
    }
    model["installation"]["resource"] = resource
    installation = PluginInstallationResponse.model_validate(model)
    receipt = PluginLaunchReceipt.model_validate(
        {**receipt.model_dump(mode="json"), "resource": resource}
    )
    health["compute"]["configured_device"] = "cuda:0"
    health["gpu_info"].update(
        device_uuid="GPU-123", driver_version="driver-1", total_memory_mb=24576
    )
    health["residency"].update(
        lifecycle_gate="enforce",
        control_epoch="1",
        identity={
            "audience": "aap-gpu-lifecycle",
            "backend_registry_id": str(installation.registry_id),
            "gpu_resource_id": resource["gpu_resource_id"],
        },
    )
    setup.update(
        plugin_identity={"plugin_id": "libreyolo", "version": "1.0"},
        physical_gpu_uuid="GPU-123",
        runtime_info={
            **LIBRE_CUDA_RUNTIME,
            "precision": None,
            "half": None,
            "amp": None,
            "driver_version": "driver-1",
            "total_memory_mb": 24576,
        },
        managed_model_compatibility=manifest["managed_model_compatibility"],
    )
    setup["gpu_resource_id"] = resource["gpu_resource_id"]
    setup["terminal_lifecycle"] = {
        "terminal_drain_signed_endpoint": "/lifecycle/terminal-drain-signed"
    }
    setup["models"][0]["resource_profile"]["device"] = "gpu"
    runtime_versions = {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in LIBRE_CUDA_RUNTIME.items()
    }
    runtime_versions.update(
        plugin_id="libreyolo",
        plugin_version="1.0",
        artifact=receipt.artifact,
        asset_manifest_sha256=manifest["asset_manifest_sha256"],
        registry_id=str(installation.registry_id),
        gpu_resource_id=resource["gpu_resource_id"],
        driver_version="driver-1",
    )
    cycles = [
        memory_cycle_evidence(
            cycle=n,
            generation=str(n),
            context_samples_mb=[100] * 5,
            loaded_samples_mb=[1000] * 5,
            unloaded_samples_mb=[100] * 5,
        )
        for n in (1, 2)
    ]
    report = build_managed_lifecycle_evidence(
        backend_name="libreyolo",
        deployment={
            "git_commit": "abc",
            "image_id": receipt.image_id,
            "runtime_versions": runtime_versions,
            "pool_topology": {
                "registry_id": str(installation.registry_id),
                "gpu_resource_id": resource["gpu_resource_id"],
                "pools": {"models": {"cap": 1}},
            },
        },
        artifacts=[
            {
                "kind": kind,
                "name": kind,
                "size_bytes": 5,
                "sha256": "a" * 64,
                "approval_ref": "approved",
            }
            for kind in ("weight", "fixture")
        ],
        gpu={
            "uuid": "GPU-123",
            "total_memory_mb": 24576,
            "driver_version": "driver-1",
            "runtime_version": LIBRE_CUDA_RUNTIME["cuda_build"],
            "visible_device_count": 1,
        },
        cycles=cycles,
        contract_checks={key: True for key in REQUIRED_CONTRACT_CHECKS},
        final_residency=health["residency"],
        runtime_ephemera_clean=True,
    )
    digest = hashlib.sha256(
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    qualification = PluginLifecycleQualification(report=report, sha256=digest)
    kwargs = dict(
        endpoint="http://detector.example.test:8000",
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup,
        qualification=qualification,
    )
    verify_endpoint_observation(installation, receipt, **kwargs)
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation, receipt, **{**kwargs, "qualification": None}
        )
    unqualified_setup = deepcopy(setup)
    unqualified_setup.pop("managed_lifecycle")
    unqualified_setup["runtime_info"]["torch"] = "unqualified"
    verify_endpoint_observation(
        installation,
        receipt,
        **{
            **kwargs,
            "setup": unqualified_setup,
            "qualification": None,
            "for_drain": True,
        },
    )
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation,
            receipt,
            **{
                **kwargs,
                "setup": unqualified_setup,
                "qualification": None,
            },
        )
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation,
            receipt,
            **{
                **kwargs,
                "qualification": qualification.model_copy(update={"sha256": "0" * 64}),
            },
        )
    bad_setup = deepcopy(setup)
    bad_setup["runtime_info"]["driver_version"] = "different-driver"
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation, receipt, **{**kwargs, "setup": bad_setup}
        )
    health["gpu_info"]["device_uuid"] = "GPU-other"
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(installation, receipt, **kwargs)


async def test_prepare_validation_never_echoes_token(httpx_client, super_admin):
    _, token = super_admin
    body = payload(uuid4())
    body["auth_token"] = "secret-too-short"
    result = await httpx_client.post(
        BASE, json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert result.status_code == 422
    assert body["auth_token"] not in result.text


async def test_activate_and_disable_are_revision_fenced(
    httpx_client, super_admin, monkeypatch, db_session
):
    from app.api.v1 import admin_plugin_installations as routes

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = payload(uuid4())
    prepared = await httpx_client.post(BASE, json=body, headers=headers)
    assert prepared.status_code == 200, prepared.text
    record = prepared.json()
    receipt = {
        "installation_id": body["installation_id"],
        "registry_id": record["registry_id"],
        "artifact": body["manifest"]["artifact"],
        "image_id": "sha256:" + "c" * 64,
        "container_id": "d" * 64,
        "resource": body["resource"],
    }

    async def verified_endpoint(db, reserved, launch, qualification):
        return {
            **launch.model_dump(mode="json"),
            "endpoint": body["url"],
            "boot_id": "boot-1",
            "control_epoch": None,
        }

    monkeypatch.setattr(routes, "observe_plugin_endpoint", verified_endpoint)
    path = BASE + "/" + body["installation_id"]
    mismatch = {**receipt, "artifact": "example.test/other@sha256:" + "a" * 64}
    rejected = await httpx_client.post(
        path + "/activate",
        headers=headers,
        json={"expected_revision": 1, "receipt": mismatch},
    )
    assert rejected.status_code == 409

    async def unavailable_routing(self, operation, body, *, request_id=None):
        raise RoutingUnavailable()

    monkeypatch.setattr(routes.RoutingAuthority, "execute", unavailable_routing)
    unavailable = await httpx_client.post(
        path + "/activate",
        headers=headers,
        json={"expected_revision": 1, "receipt": receipt},
    )
    assert unavailable.status_code == 503
    await db_session.rollback()
    retained = await httpx_client.get(path, headers=headers)
    assert retained.status_code == 200
    assert retained.json()["installation"]["status"] == "prepared"
    allow_routing_binding(monkeypatch)
    active = await httpx_client.post(
        path + "/activate",
        headers=headers,
        json={
            "expected_revision": retained.json()["installation"]["revision"],
            "receipt": receipt,
        },
    )
    assert active.status_code == 200, active.text
    assert active.json()["installation"]["status"] == "active"
    revision = active.json()["installation"]["revision"]
    stale = await httpx_client.post(
        path + "/disable", headers=headers, json={"expected_revision": revision - 1}
    )
    assert stale.status_code == 409
    await db_session.rollback()
    disabled = await httpx_client.post(
        path + "/disable", headers=headers, json={"expected_revision": revision}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["installation"]["status"] == "disabled"


async def test_failed_activation_retains_recorded_launch(
    httpx_client, super_admin, monkeypatch
):
    from app.api.v1 import admin_plugin_installations as routes

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = payload(uuid4())
    created = await httpx_client.post(BASE, json=body, headers=headers)
    assert created.status_code == 200, created.text
    path = BASE + "/" + body["installation_id"]
    receipt = {
        "installation_id": body["installation_id"],
        "registry_id": created.json()["registry_id"],
        "artifact": body["manifest"]["artifact"],
        "image_id": "sha256:" + "c" * 64,
        "container_id": "d" * 64,
        "resource": body["resource"],
    }

    async def failed_probe(*args):
        raise PluginObservationRejected("endpoint is not ready")

    monkeypatch.setattr(routes, "observe_plugin_endpoint", failed_probe)
    response = await httpx_client.post(
        path + "/activate",
        headers=headers,
        json={"expected_revision": 1, "receipt": receipt},
    )
    assert response.status_code == 409
    current = await httpx_client.get(path, headers=headers)
    assert current.json()["installation"]["status"] == "prepared"
    assert current.json()["installation"]["revision"] > 1
    other = {**receipt, "container_id": "e" * 64}
    replacement = await httpx_client.post(
        path + "/activate",
        headers=headers,
        json={
            "expected_revision": current.json()["installation"]["revision"],
            "receipt": other,
        },
    )
    assert replacement.status_code == 409

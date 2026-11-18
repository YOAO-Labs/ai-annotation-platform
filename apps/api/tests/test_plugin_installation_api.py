"""Installation API authorization, durable reservation and live-fact validation."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from yoao_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from yoao_inference_contract.plugin import (
    PluginInstallationResponse,
    PluginLaunchReceipt,
)
from yoao_inference_contract.routing import RoutingEpochView, RoutingUnavailable
from yoao_protocol_v2.lifecycle import ManagedLifecycleCapabilities
from yoao_protocol_v2.version import PROTOCOL_VERSION
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
            "models": [
                {
                    "id": "detect",
                    "task": "detect",
                    "is_interactive": False,
                    "supported_prompts": ["none"],
                    "supported_inputs": ["full_image", "crop"],
                    "supported_geometric_outputs": ["bbox"],
                }
            ],
            "pools": {"models": {"cap": 1}},
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
        "pool_topology": {"models": {"cap": 1}},
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
        "pool_topology": {"models": {"cap": 1}},
        "managed_lifecycle": ManagedLifecycleCapabilities().model_dump(mode="json"),
        "terminal_lifecycle": {"terminal_drain_endpoint": "/lifecycle/terminal-drain"},
        "models": [
            {
                "id": "detect",
                "task": "detect",
                "is_interactive": False,
                "supported_prompts": ["none"],
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
        lambda h, s: h["residency"].update({"gpu_loaded": True}),
        lambda h, s: h["residency"].update({"control_epoch": "1"}),
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


async def test_old_image_can_only_drain_without_new_topology_declarations():
    installation, receipt, challenge, health, setup = observation()
    health.pop("pool_topology")
    setup.pop("pool_topology")
    setup.pop("models")
    kwargs = dict(
        endpoint="http://detector.example.test",
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup,
    )
    assert (
        verify_endpoint_observation(installation, receipt, **kwargs, for_drain=True)[
            "boot_id"
        ]
        == "runtime-boot"
    )
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(installation, receipt, **kwargs)
    health["residency"]["pools"] = {
        "other": {"resident": False, "device": None, "provider": None}
    }
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(installation, receipt, **kwargs, for_drain=True)


async def test_multi_model_cpu_admission_requires_exact_approved_capabilities_and_pools():
    installation, receipt, challenge, health, setup = observation()
    data = installation.model_dump(mode="json")
    models = [
        {
            "id": "ocr",
            "task": "ocr",
            "is_interactive": False,
            "supported_prompts": ["none"],
            "supported_inputs": ["crop"],
            "supported_geometric_outputs": ["polygon"],
        },
        {
            "id": "classify",
            "task": "classify",
            "is_interactive": False,
            "supported_prompts": ["none"],
            "supported_inputs": ["crop"],
            "supported_geometric_outputs": [],
        },
    ]
    pools = {"engines": {"cap": 2}, "classifier": {"cap": 1}}
    data["installation"]["manifest"].update(models=models, pools=pools)
    installation = PluginInstallationResponse.model_validate(data)
    setup["models"] = [
        {**model, "resource_profile": {"device": "cpu"}} for model in models
    ]
    setup["pool_topology"] = pools
    health["pool_topology"] = deepcopy(pools)
    health["gpu_info"] = None
    health["compute"]["cpu_fallback_supported"] = True
    health["residency"]["pools"] = {
        name: {"resident": False, "device": None, "provider": None} for name in pools
    }
    kwargs = dict(
        endpoint="http://cpu.example.test",
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup,
    )
    assert (
        verify_endpoint_observation(installation, receipt, **kwargs)["boot_id"]
        == "runtime-boot"
    )
    from hashlib import sha256
    from yoao_inference_contract.plugin import PluginLifecycleQualification

    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation,
            receipt,
            **kwargs,
            qualification=PluginLifecycleQualification(
                report={}, sha256=sha256(b"{}").hexdigest()
            ),
        )
    for change in (
        lambda h, s: s["models"][0].update(supported_geometric_outputs=["bbox"]),
        lambda h, s: s["models"][0].update(supported_inputs=["unknown"]),
        lambda h, s: s["pool_topology"]["engines"].update(cap=1),
        lambda h, s: h["residency"]["pools"].pop("classifier"),
        lambda h, s: s["models"].pop(),
    ):
        changed_health, changed_setup = deepcopy(health), deepcopy(setup)
        change(changed_health, changed_setup)
        with pytest.raises(PluginObservationRejected):
            verify_endpoint_observation(
                installation,
                receipt,
                **{**kwargs, "health": changed_health, "setup": changed_setup},
            )


async def test_libreyolo_cpu_admission_keeps_runtime_and_fallback_pins():
    from training_contract.model_return import LIBRE_FORMAT, LIBRE_RUNTIME

    installation, receipt, challenge, health, setup = observation()
    data = installation.model_dump(mode="json")
    compatibility = [
        {"framework": "libreyolo", "format": LIBRE_FORMAT, "runtime_device": "cpu"}
    ]
    data["installation"]["manifest"].update(
        plugin_id="libreyolo",
        capabilities=["inference", "detection", "managed_models"],
        managed_model_compatibility=compatibility,
    )
    installation = PluginInstallationResponse.model_validate(data)
    setup.update(
        plugin_identity={"plugin_id": "libreyolo", "version": "1.0"},
        managed_model_compatibility=compatibility,
        runtime_info=dict(LIBRE_RUNTIME),
    )
    kwargs = dict(
        endpoint="http://cpu.example.test",
        challenge=challenge,
        healthy=True,
        health=health,
        setup=setup,
    )
    verify_endpoint_observation(installation, receipt, **kwargs)
    for change in (
        lambda h, s: s["runtime_info"].update(torch="different"),
        lambda h, s: h["compute"].update(cpu_fallback_supported=True),
    ):
        changed_health, changed_setup = deepcopy(health), deepcopy(setup)
        change(changed_health, changed_setup)
        with pytest.raises(PluginObservationRejected):
            verify_endpoint_observation(
                installation,
                receipt,
                **{**kwargs, "health": changed_health, "setup": changed_setup},
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


async def test_gpu_activation_checks_observed_physical_device_and_runtime(monkeypatch):
    import hashlib
    import json
    from yoao_backend_runtime.lifecycle_evidence import build_managed_lifecycle_evidence
    from yoao_lifecycle_contract import memory_cycle_evidence
    from yoao_inference_contract.plugin import PluginLifecycleQualification
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
        contract_checks=dict.fromkeys(
            (
                "managed_lifecycle_advertised",
                "real_inference",
                "provider_or_device_gpu",
                "busy_unload_rejected",
                "cancel_accounting",
                "stale_generation_rejected",
                "token_replay_rejected",
                "partial_headers_rejected",
                "full_cleanup",
            ),
            True,
        ),
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
    multi = deepcopy(model)
    pools = {"pipeline": {"cap": 1}, "detector": {"cap": 1}}
    multi["installation"]["manifest"]["pools"] = pools
    multi_installation = PluginInstallationResponse.model_validate(multi)
    multi_setup, multi_health, multi_report = (
        deepcopy(setup),
        deepcopy(health),
        deepcopy(report),
    )
    multi_setup["pool_topology"] = pools
    multi_health["pool_topology"] = deepcopy(pools)
    multi_health["residency"]["pools"] = {
        name: {"resident": False, "device": None, "provider": None} for name in pools
    }
    multi_report["deployment"]["pool_topology"]["pools"] = pools
    multi_report["final_residency"]["pools"] = deepcopy(
        multi_health["residency"]["pools"]
    )

    def signed_report(value):
        digest = hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return PluginLifecycleQualification(report=value, sha256=digest)

    multi_kwargs = {
        **kwargs,
        "setup": multi_setup,
        "health": multi_health,
        "qualification": signed_report(multi_report),
    }
    verify_endpoint_observation(multi_installation, receipt, **multi_kwargs)
    for mutate in (
        lambda value: value["deployment"]["pool_topology"]["pools"]["pipeline"].update(
            cap=2
        ),
        lambda value: value["final_residency"]["pools"].pop("detector"),
        lambda value: value["deployment"]["pool_topology"].update(
            registry_id=str(uuid4())
        ),
        lambda value: value["final_residency"]["identity"].update(
            gpu_resource_id="other"
        ),
    ):
        bad_report = deepcopy(multi_report)
        mutate(bad_report)
        with pytest.raises(PluginObservationRejected):
            verify_endpoint_observation(
                multi_installation,
                receipt,
                **{**multi_kwargs, "qualification": signed_report(bad_report)},
            )
    ort_data = deepcopy(multi)
    ort_data["installation"]["manifest"].update(
        plugin_id="onnxtools",
        capabilities=["inference", "detection"],
        managed_model_compatibility=[],
    )
    ort_installation = PluginInstallationResponse.model_validate(ort_data)
    ort_setup, ort_health, ort_report = (
        deepcopy(multi_setup),
        deepcopy(multi_health),
        deepcopy(multi_report),
    )
    ort_setup.update(
        infra="onnx",
        plugin_identity={"plugin_id": "onnxtools", "version": "1.0"},
        managed_model_compatibility=[],
    )
    ort_setup["runtime_info"]["runtime_versions"] = {"onnxruntime": "1.22"}
    ort_report["backend_name"] = "onnxtools"
    ort_report["deployment"]["runtime_versions"].update(
        plugin_id="onnxtools", onnxruntime="1.22"
    )
    expected = {"pipeline": 2, "detector": 1}
    ort_setup["runtime_info"]["ort_session_counts"] = deepcopy(expected)
    ort_health["ort_session_counts"] = deepcopy(expected)
    ort_report["deployment"]["pool_topology"]["session_counts"] = deepcopy(expected)
    session = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    observed = {
        name: [list(session) for _ in range(count)] for name, count in expected.items()
    }
    for cycle in ort_report["cycles"]:
        cycle["session_observations"] = deepcopy(observed)
    ort_kwargs = {
        **multi_kwargs,
        "setup": ort_setup,
        "health": ort_health,
        "qualification": signed_report(ort_report),
    }
    verify_endpoint_observation(ort_installation, receipt, **ort_kwargs)
    # Exercise the owner transport's real HTTP projection at the admission boundary.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from yoao_inference.health_observation_manager import _default_client_factory

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            pass

        def do_GET(self):
            request = urlsplit(self.path)
            if (
                request.path != "/health"
                or self.headers.get("Authorization") != "Bearer test-token"
                or self.headers.get("X-YOAO-GPU-Health-Challenge") != challenge
                or parse_qs(request.query).get("yoao_gpu_health_challenge")
                != [challenge]
            ):
                self.send_response(403)
                self.end_headers()
                return
            body = json.dumps(self.server.health).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-YOAO-GPU-Health-Challenge", challenge)
            self.end_headers()
            self.wfile.write(body)

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        transport = _default_client_factory(
            SimpleNamespace(
                url=endpoint,
                auth_method="token",
                auth_token="test-token",
                id=None,
                gpu_resource_id=resource["gpu_resource_id"],
                vram_budget_mb=None,
                extra_params={},
                health_meta=None,
            ),
            3,
        )
        for change in (
            None,
            "missing_pool",
            "pool_drift",
            "missing_counts",
            "count_drift",
        ):
            server.health = deepcopy(ort_health)
            server.health.pop(GPU_HEALTH_CHALLENGE_ECHO_MARKER)
            if change == "missing_pool":
                server.health.pop("pool_topology")
            elif change == "pool_drift":
                server.health["pool_topology"]["pipeline"]["cap"] = 2
            elif change == "missing_counts":
                server.health.pop("ort_session_counts")
            elif change == "count_drift":
                server.health["ort_session_counts"]["pipeline"] = 1
            healthy, observed_health = await transport.health_meta(
                gpu_health_challenge=challenge
            )
            live_kwargs = {
                **ort_kwargs,
                "endpoint": endpoint,
                "healthy": healthy,
                "health": observed_health,
            }
            if change is None:
                verify_endpoint_observation(ort_installation, receipt, **live_kwargs)
            else:
                with pytest.raises(PluginObservationRejected):
                    verify_endpoint_observation(
                        ort_installation, receipt, **live_kwargs
                    )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    for mutate in (
        lambda value: value["cycles"][0]["session_observations"]["pipeline"].pop(),
        lambda value: value["cycles"][1]["session_observations"]["detector"][0].clear(),
        lambda value: value["cycles"][0]["session_observations"]["detector"][
            0
        ].__setitem__(0, "CPUExecutionProvider"),
        lambda value: value["cycles"][0]["session_observations"].pop("detector"),
        lambda value: value["cycles"][1].pop("session_observations"),
    ):
        bad_report = deepcopy(ort_report)
        mutate(bad_report)
        with pytest.raises(PluginObservationRejected):
            verify_endpoint_observation(
                ort_installation,
                receipt,
                **{**ort_kwargs, "qualification": signed_report(bad_report)},
            )
    truncated = deepcopy(ort_report)
    for cycle in truncated["cycles"]:
        cycle["session_observations"]["pipeline"].pop()
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            ort_installation,
            receipt,
            **{**ort_kwargs, "qualification": signed_report(truncated)},
        )
    wrong_counts = deepcopy(ort_health)
    wrong_counts["ort_session_counts"]["pipeline"] = 1
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            ort_installation, receipt, **{**ort_kwargs, "health": wrong_counts}
        )
    wrong_setup_counts = deepcopy(ort_setup)
    wrong_setup_counts["runtime_info"]["ort_session_counts"]["detector"] = True
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            ort_installation, receipt, **{**ort_kwargs, "setup": wrong_setup_counts}
        )
    wrong_runtime = deepcopy(ort_setup)
    wrong_runtime["runtime_info"]["runtime_versions"]["onnxruntime"] = "different"
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            ort_installation, receipt, **{**ort_kwargs, "setup": wrong_runtime}
        )
    with pytest.raises(PluginObservationRejected):
        verify_endpoint_observation(
            installation, receipt, **{**kwargs, "qualification": None}
        )
    unqualified_setup = deepcopy(setup)
    unqualified_setup.pop("managed_lifecycle")
    unqualified_setup.pop("pool_topology")
    unqualified_setup.pop("models")
    unqualified_health = deepcopy(health)
    unqualified_health.pop("pool_topology")
    unqualified_setup["runtime_info"]["torch"] = "unqualified"
    verify_endpoint_observation(
        installation,
        receipt,
        **{
            **kwargs,
            "setup": unqualified_setup,
            "health": unqualified_health,
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


async def test_prepare_rejects_unknown_protocol_capabilities(httpx_client, super_admin):
    _, token = super_admin
    body = payload(uuid4())
    body["manifest"]["models"][0]["supported_geometric_outputs"] = ["invented"]
    result = await httpx_client.post(
        BASE, json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert result.status_code == 422
    assert (
        result.json()["detail"]["error_code"]
        == "plugin_installation_invalid_capabilities"
    )


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


async def test_asset_decisions_require_admin_and_compare_revision(
    httpx_client, super_admin, project_admin, monkeypatch, db_session
):
    from yoao_inference_contract.plugin_assets import PluginAssetDescriptor
    from app.api.v1 import admin_plugin_installations as routes

    owner, admin_token = super_admin
    owner_id = owner.id
    _, project_token = project_admin
    headers = {"Authorization": f"Bearer {admin_token}"}
    body = payload(uuid4())
    path = BASE + "/" + body["installation_id"]
    assert (
        await httpx_client.post(BASE, json=body, headers=headers)
    ).status_code == 200
    receipt = {
        "installation_id": body["installation_id"],
        "registry_id": body["installation_id"],
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
    allow_routing_binding(monkeypatch)
    active = await httpx_client.post(
        path + "/activate",
        headers=headers,
        json={"expected_revision": 1, "receipt": receipt},
    )
    assert active.status_code == 200, active.text
    descriptor = PluginAssetDescriptor.model_validate(
        {
            "schema_version": 1,
            "plugin_id": body["manifest"]["plugin_id"],
            "plugin_version": body["manifest"]["version"],
            "artifact": body["manifest"]["artifact"],
            "asset_manifest_sha256": body["manifest"]["asset_manifest_sha256"],
            "selections": [
                {
                    "model_id": "detect",
                    "variants": {"size": "small"},
                    "asset_free": True,
                    "assets": [],
                },
                {
                    "model_id": "detect",
                    "variants": {"size": "large"},
                    "asset_free": True,
                    "assets": [],
                },
            ],
        }
    )
    decision = {
        "expected_revision": 0,
        "expected_installation_revision": active.json()["installation"]["revision"],
        "descriptor": descriptor.model_dump(mode="json"),
        "descriptor_sha256": descriptor.sha256(),
        "approved": [{"model_id": "detect", "variants": {"size": "small"}}],
        "inventory": [],
        "container_id": receipt["container_id"],
        "image_id": receipt["image_id"],
    }
    denied = await httpx_client.put(
        path + "/assets",
        json=decision,
        headers={"Authorization": f"Bearer {project_token}"},
    )
    assert denied.status_code == 403
    empty = await httpx_client.get(path + "/assets", headers=headers)
    assert empty.json() == {"revision": 0, "decision": None}
    accepted = await httpx_client.put(path + "/assets", json=decision, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["revision"] == 1
    stale = await httpx_client.put(path + "/assets", json=decision, headers=headers)
    assert stale.status_code == 409
    await db_session.rollback()
    revoked = await httpx_client.put(
        path + "/assets",
        json={**decision, "expected_revision": 1, "approved": []},
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revision"] == 2
    assert (await httpx_client.get(path + "/assets", headers=headers)).json()[
        "decision"
    ]["approved"] == []

    from datetime import datetime, timezone
    from sqlalchemy import func, select
    from app.db.models.async_job import AsyncJob
    from app.db.models.ml_backend_pool import MLBackendServicePool
    from app.db.models.ml_backend_registry import (
        MLBackendRegistry,
        ProjectMLBackendPool,
    )
    from app.services.inference_topology_data import InferenceTopologyData
    from app.workers.batch_prediction_dispatch import CeleryBatchPredictionDispatcher
    from tests.test_staged_preannotate_endpoint import _seed

    project, _, _, batch = await _seed(db_session, owner_id)
    registry = await db_session.get(MLBackendRegistry, UUID(body["installation_id"]))
    registry.state = "connected"
    registry.last_checked_at = datetime.now(timezone.utc)
    registry.health_meta = {
        "capabilities": {
            "models": [
                {
                    "id": "detect",
                    "task": "detect",
                    "is_interactive": False,
                    "supported_prompts": ["none"],
                    "supported_inputs": ["full_image"],
                    "supported_geometric_outputs": ["bbox"],
                    "resource_profile": {"device": "cpu", "batchable": True},
                    "supported_variants": [
                        {
                            "key": "size",
                            "variants": [{"value": "small"}, {"value": "large"}],
                        }
                    ],
                    "default_variants": {"size": "small"},
                }
            ]
        }
    }
    pool = await InferenceTopologyData(db_session).pool_for_registry(registry.id)
    pool_row = await db_session.get(MLBackendServicePool, pool.id)
    pool_row.enabled = True
    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    await db_session.commit()
    reapplied = await httpx_client.put(
        path + "/assets", json={**decision, "expected_revision": 2}, headers=headers
    )
    assert reapplied.status_code == 200, reapplied.text
    catalog = await httpx_client.get(
        f"/api/v1/projects/{project.id}/model-catalog", headers=headers
    )
    assert catalog.status_code == 200, catalog.text
    entries = {
        entry["model_variants"]["size"]: entry
        for entry in catalog.json()["items"]
        if any(
            placement["registry_id"] == str(registry.id)
            for placement in entry["placements"]
        )
    }
    assert entries["small"]["operations"]["preannotate"]["allowed"]
    assert (
        entries["large"]["operations"]["preannotate"]["reason_code"]
        == "plugin_asset_unapproved"
    )
    revoked_again = await httpx_client.put(
        path + "/assets",
        json={**decision, "expected_revision": 3, "approved": []},
        headers=headers,
    )
    assert revoked_again.status_code == 200, revoked_again.text
    dispatched = []
    monkeypatch.setattr(
        CeleryBatchPredictionDispatcher,
        "dispatch",
        lambda *a, **kw: dispatched.append(True),
    )
    before = await db_session.scalar(
        select(func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.project_id == project.id)
    )
    request_body = {
        "ml_backend_id": str(registry.id),
        "batch_id": str(batch.id),
        "model_id": "detect",
    }
    for variants in (None, {"size": "large"}):
        response = await httpx_client.post(
            f"/api/v1/projects/{project.id}/preannotate",
            headers=headers,
            json={**request_body, "model_variants": variants},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error_code"] == "plugin_asset_unapproved"
    after = await db_session.scalar(
        select(func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.project_id == project.id)
    )
    assert before == after
    assert not dispatched


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

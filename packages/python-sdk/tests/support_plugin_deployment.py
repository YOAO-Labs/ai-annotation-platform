from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from ai_annotation.errors import AAPError, APIStatusError
from ai_annotation.plugin_deployment import LocalPluginConfig
from aap_inference_contract.plugin import (
    PluginInstallationResponse,
    PluginQuiesceResponse,
)


def config():
    return LocalPluginConfig.model_validate(
        {
            "name": "Libre detector",
            "url": "http://plugin.example:9080",
            "gpu_resource_id": "node/gpu",
            "physical_gpu_uuid": "GPU-12345678-1234-1234-1234-123456789abc",
            "network": "approved",
            "host_port": 9080,
            "cpus": 2,
            "gpu_verify_keyring": {"key1": "A" * 43},
            "image_hosts": ["assets.example"],
            "manifest": {
                "plugin_id": "libreyolo",
                "version": "1",
                "publisher": "example",
                "license": "Apache-2.0",
                "artifact": "example.invalid/plugin@sha256:" + "b" * 64,
                "sdk_compatibility": ">=1",
                "entrypoint": [
                    "aap-libreyolo",
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                ],
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
                "resource": {
                    "device": "cuda",
                    "memory_mb": 4096,
                    "gpu_memory_mb": 2048,
                },
                "asset_manifest_sha256": "a" * 64,
                "allowed_asset_sources": ["https://assets.example"],
            },
        }
    )


def report_file(tmp_path):
    from aap_backend_runtime.lifecycle_evidence import build_managed_lifecycle_evidence
    from aap_lifecycle_contract import memory_cycle_evidence
    from training_contract.inference_runtime import LIBRE_CUDA_RUNTIME

    receipt = json.loads((tmp_path / "state" / "receipt.json").read_text())
    manifest = receipt["config"]["manifest"]
    registry = receipt["registry_id"]
    resource = receipt["config"]["gpu_resource_id"]
    versions = {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in LIBRE_CUDA_RUNTIME.items()
    }
    versions.update(
        plugin_id=manifest["plugin_id"],
        plugin_version=manifest["version"],
        artifact=manifest["artifact"],
        asset_manifest_sha256=manifest["asset_manifest_sha256"],
        registry_id=registry,
        gpu_resource_id=resource,
        driver_version="580.65",
    )
    cycles = [
        memory_cycle_evidence(
            cycle=i,
            generation=str(i * 2),
            context_samples_mb=[100] * 5,
            loaded_samples_mb=[1100] * 3,
            unloaded_samples_mb=[120] * 5,
        )
        for i in (1, 2)
    ]
    report = build_managed_lifecycle_evidence(
        backend_name="libreyolo",
        deployment={
            "git_commit": "a" * 40,
            "image_id": "sha256:" + "d" * 64,
            "runtime_versions": versions,
            "pool_topology": {
                "registry_id": registry,
                "gpu_resource_id": resource,
                "pools": {"models": {"cap": 1}},
            },
        },
        artifacts=[
            {
                "kind": kind,
                "name": kind,
                "size_bytes": 1,
                "sha256": "a" * 64,
                "approval_ref": "通过",
            }
            for kind in ("weight", "fixture")
        ],
        gpu={
            "uuid": receipt["config"]["physical_gpu_uuid"],
            "total_memory_mb": 24576,
            "driver_version": "580.65",
            "runtime_version": "13.2",
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
        final_residency={
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "pools": {"models": {"resident": False}},
        },
        runtime_ephemera_clean=True,
        generated_at="2026-07-30T12:00:00Z",
    )
    path = tmp_path / "gpu-evidence.json"
    path.write_text(json.dumps(report, ensure_ascii=False))
    return path


class Core:
    def __init__(self):
        self.result = None
        self.requests = []
        self.events = []
        self.safe = True
        self.dependent = False
        self.proven_revision = None
        self.launch = None

    def prepare(self, request):
        self.requests.append(request)
        if self.result is None:
            self.result = PluginInstallationResponse.model_validate(
                {
                    "registry_id": str(uuid4()),
                    "installation": {
                        "installation_id": str(request.installation_id),
                        "manifest": request.manifest.model_dump(mode="json"),
                        "status": "prepared",
                        "revision": 1,
                        "resource": request.resource.model_dump(mode="json"),
                    },
                }
            )
        return self.result

    def get(self, installation_id):
        assert str(self.result.installation.installation_id) == installation_id
        return self.result

    def _transition(self, status):
        self.proven_revision = None
        payload = self.result.model_dump(mode="json")
        payload["installation"]["status"] = status
        payload["installation"]["revision"] += 1
        self.result = PluginInstallationResponse.model_validate(payload)
        return self.result

    def activate(self, installation_id, request):
        assert request.expected_revision == self.result.installation.revision
        assert request.receipt.registry_id == self.result.registry_id
        assert (
            request.receipt.installation_id == self.result.installation.installation_id
        )
        assert request.receipt.resource == self.result.installation.resource
        assert request.receipt.artifact == self.result.installation.manifest.artifact
        self.launch = request.receipt.model_dump(mode="json")
        assert request.qualification is not None
        canonical = json.dumps(
            request.qualification.report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        assert (
            request.qualification.sha256
            == hashlib.sha256(canonical.encode()).hexdigest()
        )
        self.events.append("activate")
        return self._transition("active")

    def disable(self, installation_id, revision):
        self.events.append("disable")
        return self._transition("disabled")

    def quiesce(self, installation_id, revision):
        assert revision == self.result.installation.revision
        assert self.result.installation.status in {"prepared", "disabled"}
        self.events.append("quiesce")
        if self.launch is None:
            raise APIStatusError(409, {"error_code": "plugin_launch_unrecorded"})
        if self.safe and self.proven_revision != revision:
            self._transition("disabled")
            self.proven_revision = self.result.installation.revision
        return PluginQuiesceResponse.model_validate(
            {
                **self.result.model_dump(mode="json"),
                "safe_to_stop": self.safe,
                "launch": self.launch,
            }
        )

    def retire(self, installation_id, revision):
        self.events.append("retire")
        assert revision == self.result.installation.revision
        if self.dependent:
            raise AAPError("active dependency")
        if self.proven_revision != revision:
            raise AAPError("quiescence proof required")
        return self._transition("retired")

    def cancel_prepared(self, installation_id, revision):
        assert self.result.installation.status == "prepared" and self.launch is None
        assert revision == self.result.installation.revision
        self.events.append("cancel")
        return self._transition("retired")


class LocalDocker:
    def __init__(self, events):
        self.events = events
        self.running = False
        self.container_id = "c" * 64
        self.started_at = "boot-1"
        self.created = False
        self.candidate_token = None

    def image_id(self, config):
        return "sha256:" + "d" * 64

    def ensure_started(self, config):
        self.events.append("start")
        self.running = True
        return {
            "id": self.container_id,
            "image_id": "sha256:" + "d" * 64,
            "started_at": self.started_at,
        }

    def verify_running(self, config):
        if not self.running:
            raise AAPError("owned container is not running")
        return {
            "id": self.container_id,
            "image_id": "sha256:" + "d" * 64,
            "started_at": self.started_at,
        }

    def wait_ready(self, config, container_id):
        self.verify_running(config)
        if container_id != self.container_id:
            raise AAPError("Container ID differs from the owned container")

    def exists(self, config):
        return self.running

    def stop_and_remove(self, config, proven_id, started_at):
        if self.running and proven_id != self.container_id:
            raise AAPError("Container ID differs from the proven container")
        if self.running and (not started_at or started_at != self.started_at):
            raise AAPError(
                "Container process identity differs from the quiescence proof"
            )
        self.events.append("stop")
        self.running = False
        self.created = False
        self.candidate_token = None

    def prepare_assets(self, config):
        self.events.append("assets")

    def create_candidate(self, config):
        if self.created and self.candidate_token != config.candidate_token:
            raise AAPError("unknown container")
        if not self.created:
            self.container_id = "e" * 64 if self.container_id != "e" * 64 else "f" * 64
            self.created = True
            self.candidate_token = config.candidate_token
            self.events.append("create")
        return {"id": self.container_id}

    def start_candidate(self, config, container_id, *, may_start):
        if (
            not self.created
            or self.candidate_token != config.candidate_token
            or self.container_id != container_id
        ):
            raise AAPError("Candidate container ID differs from the recorded identity")
        if not self.running:
            if not may_start:
                raise AAPError("Candidate process exited")
            self.events.append("start")
            self.running = True
            self.started_at = "boot-" + self.container_id[0]
        return self.verify_running(config)

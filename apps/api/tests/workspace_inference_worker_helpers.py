"""Installed Inference owner composition for cold Workspace worker acceptance."""

from __future__ import annotations

import json
import secrets

from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

_ROUTING_OWNER_PORT = 8768


class WorkspaceInferenceCeleryWorker(WorkspaceCeleryWorker):
    """Give the worker HTTP capabilities, keeping private LOGINs in the owner."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._routing_dsn = self._role_dsn(
            "AAP_MODULE_DATA_INFERENCE_ROUTING_URL", "yoao_inference_routing_client"
        )
        self._routing_token = secrets.token_urlsafe(32)
        self._routing_owner_url = f"http://127.0.0.1:{_ROUTING_OWNER_PORT}"
        self._routing_owner_served = False
        self._inference_tokens = {
            capability: secrets.token_urlsafe(32)
            for capability in (
                "MODEL_EXECUTION",
                "BACKEND_CONTROL",
                "HEALTH_OBSERVATION",
            )
        }
        profiles = (
            (
                "BACKEND_ENDPOINT_SETUP",
                "ENDPOINT_SETUP",
                "yoao_inference_endpoint_setup_client",
            ),
            (
                "BACKEND_ENDPOINT_HEALTH_OBSERVATION",
                "ENDPOINT_HEALTH_OBSERVATION",
                "yoao_inference_endpoint_health_client",
            ),
            (
                "BACKEND_ENDPOINT_CATALOG_FALLBACK",
                "ENDPOINT_CATALOG_FALLBACK",
                "yoao_inference_endpoint_catalog_client",
            ),
            (
                "BACKEND_ENDPOINT_STATS",
                "ENDPOINT_STATS",
                "yoao_inference_endpoint_stats_client",
            ),
            (
                "BACKEND_ENDPOINT_BACKEND_READ",
                "ENDPOINT_BACKEND_READ",
                "yoao_inference_endpoint_read_client",
            ),
            (
                "BACKEND_ENDPOINT_LIFECYCLE",
                "ENDPOINT_LIFECYCLE",
                "yoao_inference_endpoint_lifecycle_client",
            ),
            (
                "BACKEND_ENDPOINT_EXECUTION",
                "ENDPOINT_EXECUTION",
                "yoao_inference_endpoint_execution_client",
            ),
            ("BACKEND_STATS", "BACKEND_STATS", "yoao_inference_backend_stats_client"),
            ("GPU", "GPU", "yoao_inference_gpu_authority_client"),
            (
                "GPU_ENDPOINT_HEALTH_OBSERVATION",
                "GPU_HEALTH_ENDPOINT",
                "yoao_inference_gpu_health_endpoint_client",
            ),
            (
                "GPU_ENDPOINT_GPU_CONTROL",
                "GPU_CONTROL_ENDPOINT",
                "yoao_inference_gpu_control_endpoint_client",
            ),
            ("HEALTH_DATA", "HEALTH_DATA", "yoao_inference_health_data_client"),
            (
                "HEALTH_OBSERVATION",
                "HEALTH_OBSERVATION",
                "yoao_inference_health_observation_client",
            ),
        )
        owner_databases = {
            f"INFERENCE_{runtime}_DATABASE_URL": self._role_dsn(
                f"AAP_MODULE_DATA_INFERENCE_{source}_URL", role
            ).render_as_string(hide_password=False)
            for runtime, source, role in profiles
        }
        self._worker_environment.update(
            {
                "INFERENCE_ROUTING_URL": self._routing_owner_url,
                "INFERENCE_ROUTING_TOKEN": self._routing_token,
                "INFERENCE_ROUTING_TOKEN_FILE": "",
                "INFERENCE_ROUTING_TIMEOUT_SECONDS": "20",
                "ML_BACKEND_ROUTER_MODE": "off",
                "ML_BACKEND_ROUTER_LEASE_TTL_SECONDS": "30",
                "ML_BACKEND_ROUTER_HEARTBEAT_INTERVAL_SECONDS": "1",
            }
        )
        for capability, token in self._inference_tokens.items():
            self._worker_environment.update(
                {
                    f"INFERENCE_{capability}_URL": self._routing_owner_url,
                    f"INFERENCE_{capability}_TOKEN": token,
                    f"INFERENCE_{capability}_TOKEN_FILE": "",
                }
            )
        self._inference_owner_environment = {
            **owner_databases,
            **{
                f"INFERENCE_{capability}_TOKEN": token
                for capability, token in self._inference_tokens.items()
            },
            "INFERENCE_GPU_REDIS_URL": self._redis_url,
            "GPU_ARBITER_MODE": "off",
            "GPU_ARBITER_RESOURCES_JSON": "{}",
            "INFERENCE_REQUIRED_CAPABILITIES": "routing,execution,backend_control,health_observation",
            "INFERENCE_ROUTING_DATABASE_URL": self._routing_dsn.render_as_string(
                hide_password=False
            ),
            "INFERENCE_ROUTING_TOKEN": self._routing_token,
            "INFERENCE_ROUTING_LEASE_TTL_SECONDS": "30",
            "INFERENCE_HOST": "127.0.0.1",
            "INFERENCE_PORT": str(_ROUTING_OWNER_PORT),
        }

    def start(self) -> None:
        super().start()
        self._await_routing_owner()

    def restart(self) -> None:
        super().restart()
        self._routing_owner_served = False
        self._await_routing_owner()

    def _serve_routing_owner(self) -> None:
        if self._routing_owner_served:
            return
        environment_file = self.secret_directory / "routing-owner.env"
        self._write_env_file(environment_file, self._inference_owner_environment)
        self._docker(
            "exec",
            "--detach",
            "--env-file",
            str(environment_file),
            self.container_id,
            "/opt/venv/bin/python",
            "-m",
            "yoao_inference",
        )
        self._routing_owner_served = True

    def _await_routing_owner(self) -> None:
        self._serve_routing_owner()
        code = """import json, sys, time

import httpx

observed = None
deadline = time.monotonic() + 60
with httpx.Client(trust_env=False, timeout=2) as probe:
    while time.monotonic() < deadline:
        try:
            reply = probe.get(sys.argv[1] + '/health')
        except httpx.HTTPError:
            observed = {'unavailable': True}
        else:
            try:
                observed = reply.json()
            except ValueError:
                observed = {'http_status': reply.status_code}
        if observed == {'status': 'ready'}:
            break
        time.sleep(0.1)
print(json.dumps(observed, sort_keys=True, separators=(',', ':')))
"""
        observed = json.loads(
            self._docker(
                "exec",
                self.container_id,
                "/opt/venv/bin/python",
                "-c",
                code,
                self._routing_owner_url,
                timeout=120,
            )
        )
        if observed != {"status": "ready"}:
            raise RuntimeError(f"Owned routing owner is not ready: {observed!r}")

"""Cold installed Inference owner on the retained Compose service and owned network."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _verify_postgres,
)
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url

ROOT = Path(__file__).resolve().parents[3]
_COMPOSE = ROOT / "docker-compose.inference.yml"
_ACCEPTANCE_LABEL = "org.aap.acceptance"
_IDENTITY_LABEL = "org.aap.acceptance-id"
_SOURCE_LABEL = "org.aap.source-revision"
_DATABASE_PROFILES = {
    "PIPELINES": ("PIPELINES", "yoao_inference_pipelines_client"),
    "BACKEND_PROBE": ("BACKEND_PROBE", "yoao_inference_backend_probe_client"),
    "HEALTH_OBSERVATION": (
        "HEALTH_OBSERVATION",
        "yoao_inference_health_observation_client",
    ),
    "REGISTRY_INGRESS": ("REGISTRY_INGRESS", "yoao_inference_registry_ingress_client"),
    "ROUTING": ("ROUTING", "yoao_inference_routing_client"),
    "HEALTH_DATA": ("HEALTH_DATA", "yoao_inference_health_data_client"),
    "BACKEND_STATS": ("BACKEND_STATS", "yoao_inference_backend_stats_client"),
    "GPU": ("GPU", "yoao_inference_gpu_authority_client"),
    "GPU_COLLECTOR": ("GPU_COLLECTOR", "yoao_inference_gpu_collector_client"),
    "BACKEND_ENDPOINT_SETUP": (
        "ENDPOINT_SETUP",
        "yoao_inference_endpoint_setup_client",
    ),
    "BACKEND_ENDPOINT_HEALTH_OBSERVATION": (
        "ENDPOINT_HEALTH_OBSERVATION",
        "yoao_inference_endpoint_health_client",
    ),
    "BACKEND_ENDPOINT_CATALOG_FALLBACK": (
        "ENDPOINT_CATALOG_FALLBACK",
        "yoao_inference_endpoint_catalog_client",
    ),
    "BACKEND_ENDPOINT_STATS": (
        "ENDPOINT_STATS",
        "yoao_inference_endpoint_stats_client",
    ),
    "BACKEND_ENDPOINT_BACKEND_READ": (
        "ENDPOINT_BACKEND_READ",
        "yoao_inference_endpoint_read_client",
    ),
    "BACKEND_ENDPOINT_LIFECYCLE": (
        "ENDPOINT_LIFECYCLE",
        "yoao_inference_endpoint_lifecycle_client",
    ),
    "BACKEND_ENDPOINT_EXECUTION": (
        "ENDPOINT_EXECUTION",
        "yoao_inference_endpoint_execution_client",
    ),
    "GPU_ENDPOINT_HEALTH_OBSERVATION": (
        "GPU_HEALTH_ENDPOINT",
        "yoao_inference_gpu_health_endpoint_client",
    ),
    "GPU_ENDPOINT_GPU_CONTROL": (
        "GPU_CONTROL_ENDPOINT",
        "yoao_inference_gpu_control_endpoint_client",
    ),
}
_BEARER_SELECTORS = {
    "pipelines": "CONTROL_TOKEN",
    "backend_probe": "BACKEND_PROBE_CONTROL_TOKEN",
    "health_observation": "HEALTH_OBSERVATION_TOKEN",
    "registry_ingress": "REGISTRY_INGRESS_TOKEN",
    "routing": "ROUTING_TOKEN",
    "execution": "MODEL_EXECUTION_TOKEN",
    "backend_control": "BACKEND_CONTROL_TOKEN",
    "gpu": "GPU_TOKEN",
}
_CPU_ENVIRONMENT = {
    "GPU_ARBITER_MODE": "off",
    "GPU_ARBITER_ROLLOUT_ENABLED": "false",
    "GPU_ARBITER_RESOURCES_JSON": "{}",
    "GPU_LIFECYCLE_ACTIVE_SIGNING_KID": "acceptance",
}
inference_owner_live = pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable module-data boundary verifier",
)


def _image_evidence():
    target = os.environ.get("AAP_INFERENCE_OWNER_IMAGE")
    if not target:
        pytest.fail(
            "build and select AAP_INFERENCE_OWNER_IMAGE (Inference runtime guide)"
        )
    image = _inspect_docker(target)
    assert image["Config"]["Cmd"] == ["python", "-m", "yoao_inference"]
    labels = image["Config"]["Labels"]
    assert labels[_ACCEPTANCE_LABEL] == "module-data-boundary"
    assert re.fullmatch(r"[0-9a-f]{24}", labels[_IDENTITY_LABEL])
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, timeout=5
    ).strip()
    assert labels[_SOURCE_LABEL] == revision
    return {
        "image_id": image["Id"],
        "image_acceptance_id": labels[_IDENTITY_LABEL],
        "source_commit": revision,
    }


@asynccontextmanager
async def _cpu_ledger_redis(identity):
    url = _verify_redis()
    container = os.environ["AAP_MODULE_DATA_REDIS_CONTAINER_NAME"]
    username = f"inference-cpu-{identity}"
    password = secrets.token_urlsafe(32)
    async with Redis.from_url(url.render_as_string(hide_password=False)) as admin:
        await admin.execute_command(
            "ACL",
            "SETUSER",
            username,
            "reset",
            "on",
            ">" + password,
            "-@all",
            "+ping",
            "+select",
            "+client|setinfo",
        )
        try:
            yield (
                container,
                url.set(
                    username=username, password=password, host=container, port=6379
                ),
            )
        finally:
            _verify_redis()
            assert await admin.acl_deluser(username) == 1


@dataclass
class InferenceOwner:
    container_id: str
    gateway: str
    tokens: dict[str, str] = field(repr=False)
    evidence: dict = field(repr=False)
    origin: str = ""

    def inspect(self):
        state = _inspect_docker(self.container_id)
        labels = state["Config"]["Labels"]
        assert state["Id"] == self.container_id
        assert state["Image"] == self.evidence["image_id"]
        assert labels[_ACCEPTANCE_LABEL] == "module-data-boundary"
        assert labels[_IDENTITY_LABEL] == self.evidence["identity"]
        assert labels["com.docker.compose.project"] == self.evidence["project"]
        assert labels["com.docker.compose.service"] == "inference"
        return state

    async def ready(self):
        published = self.inspect()["NetworkSettings"]["Ports"]["8768/tcp"]
        assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
        self.origin = f"http://127.0.0.1:{int(published[0]['HostPort'])}"
        async with (
            asyncio.timeout(45),
            httpx.AsyncClient(
                base_url=self.origin, trust_env=False, timeout=8
            ) as client,
        ):
            while True:
                try:
                    response = await client.get("/health")
                    if response.status_code == 200:
                        assert response.json() == {"status": "ready"}
                        return
                except httpx.HTTPError:
                    pass
                assert self.inspect()["State"]["Running"], "Inference owner exited"
                await asyncio.sleep(0.1)

    async def restart(self):
        started = self.inspect()["State"]["StartedAt"]
        self.evidence["origin_before_restart"] = self.origin
        _docker("restart", "--time", "5", self.container_id)
        await self.ready()
        assert self.inspect()["State"]["StartedAt"] != started
        self.evidence["origin_after_restart"] = self.origin
        self.evidence["restart_verified"] = True

    def stop(self):
        assert self.inspect()["State"]["Running"]
        _docker("stop", "--time", "5", self.container_id)
        state = self.inspect()["State"]
        assert not state["Running"] and not state["OOMKilled"]


async def assert_ingress_capability_isolation(owner):
    routes = {
        "/v1/backends/probe": "backend_probe",
        "/v1/backends/health-observation": "health_observation",
        "/v1/registry/env-sync": "registry_ingress",
        "/v1/registry/env-sync-status": "registry_ingress",
    }
    async with httpx.AsyncClient(base_url=owner.origin, trust_env=False) as client:
        for path, expected in routes.items():
            assert (await client.post(path, json={})).status_code == 401
            for capability, token in owner.tokens.items():
                response = await client.post(
                    path, json={}, headers={"Authorization": f"Bearer {token}"}
                )
                assert response.status_code == (400 if capability == expected else 401)

    policies = {
        "BACKEND_PROBE": "yoao_inference_backend_probe_v1.backend_probe_policy()",
        "HEALTH_OBSERVATION": (
            "yoao_inference_health_observation_v1.health_observation_policy()"
        ),
        "REGISTRY_INGRESS": "yoao_inference_registry_ingress_v1.registry_ingress_policy()",
    }
    for profile, policy in policies.items():
        label, role = _DATABASE_PROFILES[profile]
        login = make_url(_role_url(f"AAP_MODULE_DATA_INFERENCE_{label}_URL", role))
        connection = await asyncpg.connect(
            login.set(drivername="postgresql").render_as_string(hide_password=False),
            timeout=5,
            command_timeout=5,
        )
        try:
            assert await connection.fetchval("SELECT session_user") == role
            assert await connection.fetchval(f"SELECT {policy}") is not None
            for unrelated in policies.values():
                if unrelated != policy:
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.fetchval(f"SELECT {unrelated}")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetchval(
                    "SELECT auth_token FROM public.ml_backend_registry LIMIT 1"
                )
        finally:
            await connection.close()
    owner.evidence["bearer_and_login_isolation_verified"] = True


def _mount_owner_secrets(directory, environment, postgres_name, test_url, redis_url):
    mount_sources = {}
    redactions = {redis_url.password}

    def secret(selector, name, value):
        path = directory / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o444)
        environment.setenv(f"INFERENCE_RUNTIME_{selector}_FILE", str(path))
        mount_sources[f"/run/secrets/{name}"] = path
        redactions.add(value)

    for name, (label, role) in _DATABASE_PROFILES.items():
        url = make_url(_role_url(f"AAP_MODULE_DATA_INFERENCE_{label}_URL", role))
        assert (url.port, url.database) == (test_url.port, test_url.database)
        assert url.password
        filename = (
            "gpu_arbiter_collector_database_url"
            if name == "GPU_COLLECTOR"
            else f"inference_{name.lower()}_database_url"
        )
        mounted_url = url.set(host=postgres_name, port=5432)
        secret(
            f"{name}_DATABASE_URL",
            filename,
            mounted_url.render_as_string(hide_password=False),
        )
        redactions.add(url.password)
    tokens = {capability: secrets.token_urlsafe(32) for capability in _BEARER_SELECTORS}
    for capability, selector in _BEARER_SELECTORS.items():
        secret(selector, f"inference_{selector.lower()}", tokens[capability])
    secret(
        "GPU_REDIS_URL",
        "inference_gpu_redis_url",
        redis_url.render_as_string(hide_password=False),
    )
    signing_key = secrets.token_urlsafe(32)
    secret(
        "GPU_SIGNING_KEYS",
        "gpu_lifecycle_signing_keys",
        json.dumps({"acceptance": signing_key}),
    )
    redactions.add(signing_key)
    return mount_sources, tokens, redactions


def _verify_runtime(owner, project, network, mount_sources):
    state = owner.inspect()
    config, host = state["Config"], state["HostConfig"]
    assert config["User"] == "10008:10008"
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert {"nodev", "noexec", "nosuid"} <= set(host["Tmpfs"]["/tmp"].split(","))
    assert not host["DeviceRequests"] and not host["Devices"]
    assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (
        256 * 1024 * 1024,
        1_000_000_000,
        64,
    )
    mounts = [
        mount
        for mount in state["Mounts"]
        if (mount["Type"], mount["Destination"]) != ("tmpfs", "/tmp")
    ]
    assert len(mounts) == len(mount_sources) and all(
        mount["Type"] == "bind" and not mount["RW"] for mount in mounts
    )
    assert {
        mount["Destination"]: Path(mount["Source"]) for mount in mounts
    } == mount_sources
    networks = state["NetworkSettings"]["Networks"]
    assert set(networks) == {project}
    assert networks[project]["NetworkID"] == network
    gateway = ip_address(networks[project]["Gateway"])
    assert gateway.version == 4 and not gateway.is_loopback
    owner.gateway = str(gateway)
    app_env = dict(value.split("=", 1) for value in config["Env"])
    assert all(app_env[key] == value for key, value in _CPU_ENVIRONMENT.items())
    assert set(app_env["INFERENCE_REQUIRED_CAPABILITIES"].split(",")) == set(
        owner.tokens
    )


def _capture_logs(owner, destination, redactions):
    owner.evidence["oom_killed"] = owner.inspect()["State"]["OOMKilled"]
    output = subprocess.run(
        ["docker", "logs", "--tail", "160", owner.container_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    log = output.stdout + output.stderr
    for value in sorted(filter(None, redactions), key=len, reverse=True):
        log = log.replace(value, "<redacted>")
    destination.touch(mode=0o600)
    destination.write_text(log, encoding="utf-8")
    owner.evidence["log_file"] = destination.name
    assert not owner.evidence["oom_killed"]


@asynccontextmanager
async def inference_owner_container(test_engine, monkeypatch):
    await assert_owned_test_database(test_engine)
    image = _image_evidence()
    identity = secrets.token_hex(12)
    project = f"aap-inference-{identity}"
    postgres_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    postgres_name = os.environ["AAP_MODULE_DATA_PG_CONTAINER_NAME"]
    evidence_dir = ROOT / ".worktree"
    evidence_dir.mkdir(exist_ok=True)
    evidence = {
        **image,
        "identity": identity,
        "project": project,
        "compose_sha256": sha256(_COMPOSE.read_bytes()).hexdigest(),
        "database_roles": {
            name: role for name, (_, role) in _DATABASE_PROFILES.items()
        },
        "gpu_arbiter_mode": "off",
    }
    async with _cpu_ledger_redis(identity) as (redis_container, redis_url):
        with (
            tempfile.TemporaryDirectory(
                prefix=f"{project}-", dir=evidence_dir
            ) as temporary,
            monkeypatch.context() as environment,
        ):
            directory = Path(temporary)
            mount_sources, tokens, redactions = _mount_owner_secrets(
                directory, environment, postgres_name, test_engine.url, redis_url
            )
            for key, value in _CPU_ENVIRONMENT.items():
                environment.setenv(key, value)
            override = directory / "compose.test.yml"
            override.write_text(
                f"""services:
  inference:
    image: {image["image_id"]}
    build: !reset null
    ports: !override ["127.0.0.1::8768"]
    labels:
      {_ACCEPTANCE_LABEL}: module-data-boundary
      {_IDENTITY_LABEL}: {identity}
networks:
  default:
    external: true
    name: {project}
""",
                encoding="utf-8",
            )
            compose = (
                "compose",
                "--project-name",
                project,
                "--project-directory",
                str(directory),
                "-f",
                str(_COMPOSE),
                "-f",
                str(override),
            )
            network = _docker(
                "network",
                "create",
                "--label",
                f"{_ACCEPTANCE_LABEL}=module-data-boundary",
                "--label",
                f"{_IDENTITY_LABEL}={identity}",
                project,
            )
            connected = []
            owner = None
            try:
                _verify_postgres(test_engine.url)
                _verify_redis()
                for container in (postgres_id, redis_container):
                    _docker(
                        "network", "connect", "--gw-priority", "-1", network, container
                    )
                    connected.append(container)
                _docker(*compose, "up", "--detach", "--no-build", "--pull", "never")
                container_id = _docker(*compose, "ps", "--all", "--quiet", "inference")
                assert re.fullmatch(r"[0-9a-f]{64}", container_id)
                owner = InferenceOwner(container_id, "", tokens, evidence)
                _verify_runtime(owner, project, network, mount_sources)
                await owner.ready()
                evidence.update(
                    container_id=container_id,
                    network_id=network,
                    restrictions_verified=True,
                    required_capabilities=sorted(tokens),
                    gpu_devices=False,
                )
                yield owner
            finally:
                try:
                    if owner is not None:
                        _capture_logs(
                            owner, evidence_dir / f"{project}.log", redactions
                        )
                finally:
                    try:
                        _docker(*compose, "down")
                        evidence["container_removed"] = True
                    finally:
                        if postgres_id in connected:
                            _verify_postgres(test_engine.url)
                            _docker("network", "disconnect", network, postgres_id)
                        if redis_container in connected:
                            _verify_redis()
                            _docker("network", "disconnect", network, redis_container)
                        _docker("network", "rm", network)
                        (evidence_dir / f"{project}-runtime.json").write_text(
                            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
                        )

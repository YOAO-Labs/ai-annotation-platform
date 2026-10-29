"""Validate ONNXTools' managed lifecycle with approved models on a real GPU.

The gate is intentionally fail-closed. Model and fixture files must match approved
SHA-256 digests. Positive, empty, and edge-of-image vehicle fixtures must exercise
real inference; evidence is emitted to stdout without changing deployment flags.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import gc
import hashlib
import json
import math
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from statistics import median
from typing import Any

import httpx
import jwt
import uvicorn
from aap_backend_runtime.backend_auth import backend_auth_headers
from aap_backend_runtime import (
    artifact_evidence,
    build_managed_lifecycle_evidence,
    exercise_lifecycle_fault_matrix,
)
from aap_backend_runtime.gpu import observed_cuda_runtime_version
from aap_lifecycle_contract import REQUIRED_CONTRACT_CHECKS, memory_cycle_evidence
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    encode_ed25519_public_key,
    sign_admission_token,
)
from aap_protocol_v2.routing import (
    ROUTE_ADMISSION_AUDIENCE,
    ROUTE_ADMISSION_HEADER,
    ROUTE_ADMISSION_PROTOCOL,
    ROUTE_CONTROL_PATH,
    ROUTE_CONTROL_PROTOCOL,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


CONTROL_SCOPES = {
    AdmissionScope.DRAIN,
    AdmissionScope.UNLOAD,
    AdmissionScope.RESUME,
    AdmissionScope.MODE,
    AdmissionScope.RESET,
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _approved_artifact(
    *,
    label: str,
    path: Path,
    expected_sha_env: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{label} file does not exist: {path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"{label} file is empty: {path}")
    expected_sha = _required_env(expected_sha_env)
    if SHA256_PATTERN.fullmatch(expected_sha) is None:
        raise RuntimeError(f"{expected_sha_env} must be a lowercase SHA-256 digest")
    actual_sha = _sha256(path)
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"{label} SHA-256 is not approved: expected={expected_sha}, actual={actual_sha}"
        )
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": actual_sha,
    }


def _validate_artifacts() -> dict[str, Any]:
    approval_ref = _required_env("VALIDATION_MODEL_APPROVAL_REF")
    model_dir = (
        Path(_required_env("AAP_PLUGIN_DATA_DIR")) / "models"
        if os.environ.get("AAP_PLUGIN_TOKEN_FILE")
        else Path(os.getenv("ONNXTOOLS_MODEL_DIR", "/app/models"))
    )
    det_path = model_dir / os.getenv("ONNXTOOLS_DET_MODEL", "rtdetr-2024080100.onnx")
    va_path = model_dir / os.getenv("ONNXTOOLS_VA_MODEL", "va_260612.onnx")
    images = {
        name: _approved_artifact(
            label=f"{name} fixture",
            path=Path(_required_env(path_env)),
            expected_sha_env=sha_env,
        )
        for name, path_env, sha_env in (
            ("positive", "VALIDATION_IMAGE_PATH", "VALIDATION_IMAGE_SHA256"),
            ("empty", "VALIDATION_EMPTY_IMAGE_PATH", "VALIDATION_EMPTY_IMAGE_SHA256"),
            (
                "boundary",
                "VALIDATION_BOUNDARY_IMAGE_PATH",
                "VALIDATION_BOUNDARY_IMAGE_SHA256",
            ),
        )
    }
    if len({item["sha256"] for item in images.values()}) != len(images):
        raise RuntimeError("positive, empty and boundary fixtures must be distinct")

    det = _approved_artifact(
        label="detector model",
        path=det_path,
        expected_sha_env="VALIDATION_DET_SHA256",
    )
    va = _approved_artifact(
        label="vehicle-attribute model",
        path=va_path,
        expected_sha_env="VALIDATION_VA_SHA256",
    )
    if det["sha256"] == va["sha256"]:
        raise RuntimeError("detector and vehicle-attribute models must be distinct")
    return {
        "approval_ref": approval_ref,
        "detector": det,
        "vehicle_attribute": va,
        "fixtures": images,
    }


def _gpu_query(fields: str) -> list[list[str]]:
    output = subprocess.check_output(
        [
            "nvidia-smi",
            f"--query-gpu={fields}",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    return [
        [part.strip() for part in line.split(",")]
        for line in output.splitlines()
        if line.strip()
    ]


def _gpu_row(target_uuid: str | None) -> list[str]:
    rows = _gpu_query("uuid,memory.used,memory.total,driver_version")
    if len(rows) != 1:
        raise RuntimeError("qualification requires exactly one visible GPU")
    if target_uuid and rows[0][0] != target_uuid:
        raise RuntimeError(f"GPU UUID {target_uuid!r} is not the sole visible GPU")
    return rows[0]


def _gpu_metadata(target_uuid: str | None) -> dict[str, Any]:
    rows = _gpu_query("uuid,memory.used,memory.total,driver_version")
    row = _gpu_row(target_uuid)
    return {
        "uuid": row[0],
        "total_memory_mb": int(row[2]),
        "driver_version": row[3],
        "visible_device_count": len(rows),
    }


def _gpu_memory_mb(target_uuid: str | None) -> int:
    return int(_gpu_row(target_uuid)[1])


def _gpu_total_memory_mb(target_uuid: str | None) -> int:
    return int(_gpu_row(target_uuid)[2])


def _gpu_compute_processes(target_uuid: str | None) -> list[dict[str, int | str]]:
    output = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    processes: list[dict[str, int | str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        gpu_uuid, pid, used = (part.strip() for part in line.split(",", 2))
        if target_uuid is None or gpu_uuid == target_uuid:
            processes.append(
                {"gpu_uuid": gpu_uuid, "pid": int(pid), "used_memory_mb": int(used)}
            )
    return processes


def _gpu_memory_samples(
    target_uuid: str | None,
    *,
    count: int = 5,
) -> list[int]:
    samples: list[int] = []
    for _ in range(count):
        samples.append(_gpu_memory_mb(target_uuid))
        time.sleep(0.5)
    return samples


def _assert_isolated_before_start(target_uuid: str | None) -> dict[str, Any]:
    processes = _gpu_compute_processes(target_uuid)
    if processes:
        raise RuntimeError(
            f"validation GPU is not isolated before backend startup: {processes}"
        )
    return {
        "used_memory_mb": _gpu_memory_mb(target_uuid),
        "compute_processes": processes,
    }


def _initialize_cuda_context() -> ctypes.CDLL:
    """Establish the no-model CUDA baseline in this operator process."""
    library = ctypes.CDLL("libcudart.so.12")
    library.cudaSetDevice.argtypes = [ctypes.c_int]
    library.cudaSetDevice.restype = ctypes.c_int
    library.cudaFree.argtypes = [ctypes.c_void_p]
    library.cudaFree.restype = ctypes.c_int
    library.cudaDeviceSynchronize.argtypes = []
    library.cudaDeviceSynchronize.restype = ctypes.c_int
    for name, call in (
        ("cudaSetDevice", lambda: library.cudaSetDevice(0)),
        ("cudaFree", lambda: library.cudaFree(None)),
        ("cudaDeviceSynchronize", library.cudaDeviceSynchronize),
    ):
        result = call()
        if result != 0:
            raise RuntimeError(f"{name} failed with CUDA status {result}")
    return library


def _assert_unloaded(
    *,
    residency: dict[str, Any],
    snapshot: dict[str, Any],
    samples: list[int],
) -> None:
    assert residency["state"] == "unloaded"
    assert residency["gpu_loaded"] is False
    assert residency["evictable"] is False
    assert residency["active_requests"] == 0
    assert residency["builders"] == 0
    assert residency["borrowers"] == 0
    assert all(pool["resident"] is False for pool in residency["pools"].values())
    assert snapshot["current_size"] == 0
    assert snapshot["builders"] == 0
    assert snapshot["borrowers"] == 0
    assert snapshot["waiters"] == 0
    assert snapshot["cleanup_in_progress"] is False
    assert snapshot["cleanup_failed"] is False
    assert snapshot["gpu_resident"] is False
    if max(samples) - min(samples) > 64:
        raise AssertionError(f"unloaded GPU memory did not stabilize: {samples}")


def _assert_memory_recovery(memory: dict[str, Any]) -> None:
    context = memory["context_baseline_mb"]
    first = memory["cycle_1_unloaded_mb"]
    second = memory["cycle_2_unloaded_mb"]
    if abs(median(first) - median(second)) > 64:
        raise AssertionError(
            "unloaded GPU baseline drifted between generations: "
            f"cycle_1={first}, cycle_2={second}"
        )
    context_median = median(context)
    slack_mb = max(512, int(memory["gpu_total_mb"] * 0.02))
    for cycle in (1, 2):
        loaded = memory[f"cycle_{cycle}_loaded_mb"]
        unloaded = memory[f"cycle_{cycle}_unloaded_mb"]
        loaded_median = median(loaded)
        unloaded_median = median(unloaded)
        if unloaded_median > context_median + slack_mb:
            raise AssertionError(
                f"cycle {cycle} did not return to the ORT context baseline: "
                f"context={context}, unloaded={unloaded}, slack_mb={slack_mb}"
            )
        working_set = loaded_median - context_median
        recovered = loaded_median - unloaded_median
        if working_set <= 0 or recovered / working_set < 0.90:
            raise AssertionError(
                f"cycle {cycle} recovered less than 90% of its model working set: "
                f"context={context_median}, loaded={loaded}, unloaded={unloaded}"
            )


def _assert_predict_results(model_id: str, payload: dict[str, Any]) -> None:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise AssertionError(f"{model_id} did not return exactly one task result")
    items = results[0].get("result")
    if not isinstance(items, list) or not items:
        raise AssertionError(
            f"{model_id} produced no result; use a representative vehicle image"
        )
    if model_id in {"vehicle-attr", "vehicle-attr-classify"} and not any(
        isinstance(item.get("attributes"), dict)
        and all(
            isinstance(item["attributes"].get(key), str)
            and bool(item["attributes"][key])
            for key in ("vehicle_type", "color")
        )
        for item in items
    ):
        raise AssertionError(f"{model_id} did not execute vehicle classification")


def _assert_empty_result(model_id: str, payload: dict[str, Any]) -> None:
    results = payload.get("results")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or results[0].get("result") != []
    ):
        raise AssertionError(f"{model_id} detected an object in the empty fixture")


def _assert_boundary_result(model_id: str, payload: dict[str, Any]) -> None:
    _assert_predict_results(model_id, payload)
    touches_edge = False
    for item in payload["results"][0]["result"]:
        box = item.get("value")
        if not isinstance(box, dict):
            raise AssertionError(f"{model_id} returned a malformed boundary box")
        values = [box.get(key) for key in ("x", "y", "width", "height")]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise AssertionError(f"{model_id} returned non-finite boundary coordinates")
        x, y, width, height = values
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > 100
            or y + height > 100
        ):
            raise AssertionError(f"{model_id} returned an out-of-bounds boundary box")
        labels = box.get("rectanglelabels")
        touches_edge |= (
            isinstance(labels, list)
            and "car" in labels
            and min(x, y, 100 - x - width, 100 - y - height) <= 5
        )
    if not touches_edge:
        raise AssertionError(f"{model_id} did not detect the edge-of-image vehicle")


async def _provider_evidence(
    main_module: Any, expected_sessions: dict[str, int]
) -> dict[str, list[dict[str, Any]]]:
    from predictor import inspect_handle_sessions

    assert main_module._handle_pool is not None  # noqa: SLF001
    evidence: dict[str, list[dict[str, Any]]] = {}
    for name, expected_count in expected_sessions.items():
        async with main_module._handle_pool.borrow(name) as lease:  # noqa: SLF001
            sessions = inspect_handle_sessions(name, lease.handle)
            if (
                sessions is None
                or len(sessions) != expected_count
                or any(s is None for s in sessions)
            ):
                raise AssertionError(f"{name} business session evidence is incomplete")
            observed = []
            for session in sessions:
                providers = session.get_providers()
                if not providers or providers[0] != "CUDAExecutionProvider":
                    raise AssertionError(
                        f"{name} silently fell back from CUDA: {providers}"
                    )
                options = session.get_provider_options()
                device_id = options.get("CUDAExecutionProvider", {}).get("device_id")
                if device_id != "0":
                    raise AssertionError(
                        f"{name} did not report CUDA device 0: {device_id!r}"
                    )
                observed.append({"providers": list(providers), "device_id": device_id})
            evidence[name] = observed
    return evidence


@asynccontextmanager
async def _http_client(app: Any):
    """Serve the qualified app on an ephemeral loopback port in this process."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("qualification HTTP server exited before startup")
            await asyncio.sleep(0.01)
        plugin_token_path = os.environ.get("AAP_PLUGIN_TOKEN_FILE")
        headers = (
            {"Authorization": "Bearer " + Path(plugin_token_path).read_text().strip()}
            if plugin_token_path
            else backend_auth_headers("onnxtools")
        )
        async with httpx.AsyncClient(
            headers=headers,
            base_url=f"http://127.0.0.1:{port}",
            timeout=600,
        ) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            await server_task
        finally:
            sock.close()


async def _bind_operator_routes(
    client: httpx.AsyncClient, *, registry_id: str, route_secret: str
) -> None:
    """Bind an isolated operator authority through the actual HTTP control endpoint."""
    signing_key = secrets.token_bytes(32)
    command = {
        "schema": ROUTE_CONTROL_PROTOCOL,
        "command_id": str(uuid.uuid4()),
        "registry_id": registry_id,
        "authority_epoch": str(uuid.uuid4()),
        "retired_epoch": None,
        "challenge": secrets.token_hex(32),
        "action": "drain",
        "signing_key": None,
    }
    headers = {"Authorization": f"Bearer {route_secret}"}
    drained = await client.post(ROUTE_CONTROL_PATH, json=command, headers=headers)
    drained.raise_for_status()
    observed = drained.json()
    if (
        observed.get("registry_id") != registry_id
        or observed.get("command_id") != command["command_id"]
        or observed.get("challenge") != command["challenge"]
        or observed.get("authority_epoch") != command["authority_epoch"]
        or observed.get("phase") != "drained"
        or observed.get("active_requests") != 0
    ):
        raise RuntimeError("operator route drain did not bind the reserved registry")
    command["action"] = "bind"
    command["signing_key"] = base64.urlsafe_b64encode(signing_key).decode().rstrip("=")
    bound = await client.post(ROUTE_CONTROL_PATH, json=command, headers=headers)
    bound.raise_for_status()
    reply = bound.json()
    if (
        reply.get("registry_id") != registry_id
        or reply.get("command_id") != command["command_id"]
        or reply.get("challenge") != command["challenge"]
        or reply.get("authority_epoch") != command["authority_epoch"]
        or reply.get("backend_incarnation") != observed.get("backend_incarnation")
        or reply.get("phase") != "bound"
    ):
        raise RuntimeError("operator route bind did not match the drained backend")
    incarnation = reply["backend_incarnation"]

    async def authorize(request: httpx.Request) -> None:
        if request.method == "GET":
            return
        operation = "gpu:unload" if request.url.path == "/unload" else "validation"
        now = int(time.time())
        claims = {
            "schema": ROUTE_ADMISSION_PROTOCOL,
            "aud": ROUTE_ADMISSION_AUDIENCE,
            "registry_id": registry_id,
            "backend_incarnation": incarnation,
            "authority_epoch": command["authority_epoch"],
            "scope_id": str(uuid.uuid4()),
            "attempt_id": str(uuid.uuid4()),
            "operation": operation,
            "method": request.method,
            "path": request.url.path,
            "iat": now,
            "exp": now + 60,
        }
        request.headers[ROUTE_ADMISSION_HEADER] = jwt.encode(
            claims, signing_key, algorithm="HS256", headers={"typ": "JWT"}
        )

    client.event_hooks["request"].append(authorize)


async def _run_with_authority(
    private_dir: Path, progress: dict[str, Any]
) -> dict[str, Any]:
    git_commit = _required_env("VALIDATION_GIT_COMMIT")
    progress["deployment"]["git_commit"] = git_commit
    image_id = _required_env("VALIDATION_IMAGE_ID")
    progress["deployment"]["image_id"] = image_id
    registry_id = _required_env("VALIDATION_BACKEND_REGISTRY_ID")
    resource_id = _required_env("VALIDATION_GPU_RESOURCE_ID")
    fixture_approval_ref = _required_env("VALIDATION_FIXTURE_APPROVAL_REF")
    progress["stage"] = "deployment_identity"
    plugin_id = _required_env("AAP_PLUGIN_ID")
    progress["backend_name"] = plugin_id
    plugin_version = _required_env("AAP_PLUGIN_VERSION")
    artifact = _required_env("AAP_PLUGIN_OCI_ARTIFACT")
    asset_manifest_sha256 = _required_env("AAP_PLUGIN_ASSET_MANIFEST_SHA256")
    if SHA256_PATTERN.fullmatch(asset_manifest_sha256) is None:
        raise RuntimeError("AAP_PLUGIN_ASSET_MANIFEST_SHA256 must be a SHA-256 digest")
    if (
        _required_env("AAP_PLUGIN_REGISTRY_ID") != registry_id
        or _required_env("AAP_ROUTE_REGISTRY_ID") != registry_id
        or _required_env("AAP_PLUGIN_GPU_RESOURCE_ID") != resource_id
        or _required_env("AAP_PLUGIN_DEVICE") != "cuda:0"
    ):
        raise RuntimeError("qualification and plugin deployment identities differ")
    progress["stage"] = "artifact_validation"
    artifacts = _validate_artifacts()
    artifact_records = [
        artifact_evidence(
            artifacts["detector"]["path"],
            kind="weight",
            approval_ref=artifacts["approval_ref"],
        ),
        artifact_evidence(
            artifacts["vehicle_attribute"]["path"],
            kind="weight",
            approval_ref=artifacts["approval_ref"],
        ),
        *(
            artifact_evidence(
                item["path"], kind="fixture", approval_ref=fixture_approval_ref
            )
            for item in artifacts["fixtures"].values()
        ),
    ]
    approved_inputs = [
        artifacts["detector"],
        artifacts["vehicle_attribute"],
        *artifacts["fixtures"].values(),
    ]
    if any(
        record["sha256"] != approved["sha256"]
        or record["size_bytes"] != approved["size"]
        for record, approved in zip(artifact_records, approved_inputs)
    ):
        raise RuntimeError("approved artifact changed during evidence collection")
    progress["artifacts"] = artifact_records
    progress["stage"] = "hardware_identity"
    _required_env("AAP_PLUGIN_TOKEN_FILE")
    requested_uuid = _required_env("VALIDATION_GPU_UUID")
    if _required_env("AAP_PLUGIN_GPU_UUID") != requested_uuid:
        raise RuntimeError("qualification and plugin GPU identities differ")
    gpu = _gpu_metadata(requested_uuid)
    progress["gpu"] = {**gpu, "runtime_version": None}
    target_uuid = gpu["uuid"]
    cold_baseline = _assert_isolated_before_start(target_uuid)
    print(
        json.dumps(
            {"event": "cold_card_baseline", "gpu_uuid": target_uuid, **cold_baseline},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    cuda_version = observed_cuda_runtime_version()
    progress["gpu"] = {**gpu, "runtime_version": cuda_version}

    private_key = Ed25519PrivateKey.generate()
    route_secret = secrets.token_urlsafe(48)
    route_file = private_dir / "route-token"
    keyring_file = private_dir / "gpu-keyring.json"
    route_file.write_text(route_secret, encoding="utf-8")
    keyring_file.write_text(
        json.dumps({"validation": encode_ed25519_public_key(private_key.public_key())}),
        encoding="utf-8",
    )
    route_file.chmod(0o600)
    keyring_file.chmod(0o600)
    os.environ["AAP_ROUTE_CONTROL_TOKEN_FILE"] = str(route_file)
    os.environ["AAP_PLUGIN_GPU_KEYRING_FILE"] = str(keyring_file)
    os.environ["ONNXTOOLS_IDLE_UNLOAD_SECONDS"] = "0"
    os.environ["ONNXTOOLS_BUILD_TIMEOUT"] = "300"

    app_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(app_root))
    import main  # noqa: PLC0415
    from handle_pool import HANDLE_SESSION_COUNTS  # noqa: PLC0415

    expected_sessions = HANDLE_SESSION_COUNTS

    contract_checks = progress["contract_checks"]
    progress["deployment"] = {
        "git_commit": git_commit,
        "image_id": image_id,
        "runtime_versions": {
            "python": platform.python_version(),
            "onnxruntime": str(__import__("onnxruntime").__version__),
            "cuda": cuda_version,
            "cuda_image": _required_env("CUDA_VERSION"),
            "backend": str(main.BACKEND_VERSION),
            "plugin_id": plugin_id,
            "plugin_version": plugin_version,
            "artifact": artifact,
            "asset_manifest_sha256": asset_manifest_sha256,
            "registry_id": registry_id,
            "gpu_resource_id": resource_id,
            "driver_version": gpu["driver_version"],
            "qualification_authority": "operator-ephemeral",
        },
        "pool_topology": {
            "registry_id": registry_id,
            "gpu_resource_id": resource_id,
            "pools": {name: {"cap": 1} for name in expected_sessions},
            "session_counts": dict(expected_sessions),
        },
    }
    progress["stage"] = "plugin_startup"
    memory: dict[str, Any] = {
        "cold_card_baseline": cold_baseline,
        "gpu_total_mb": _gpu_total_memory_mb(target_uuid),
    }
    image_path = artifacts["fixtures"]["positive"]["path"]

    async with main.lifespan(main.app):
        assert main._gpu_lifecycle is not None  # noqa: SLF001
        assert main._handle_pool is not None  # noqa: SLF001
        # This loopback-only operator process measures the real lifecycle before
        # a report exists. Normal plugin startup never receives this authority.
        main._gpu_lifecycle = main.OnnxToolsGpuLifecycle(  # noqa: SLF001
            main._handle_pool,  # noqa: SLF001
            verify_keyring=main.load_verify_keyring(keyring_file.read_text()),
            evictable_verified=True,
            **main.plugin_lifecycle_options(),
        )
        main.app.state.lifecycle_verified = True
        boot_id = main._gpu_lifecycle.boot_id  # noqa: SLF001

        def token(
            scope: AdmissionScope,
            *,
            generation: str | None = None,
            control_epoch: str = "2",
            owner: str = "validation",
            operation: str = "validation",
            jti: str | None = None,
        ) -> str:
            control = scope in CONTROL_SCOPES
            claims = AdmissionTokenClaims(
                backend_registry_id=registry_id,
                gpu_resource_id=resource_id,
                boot_id=boot_id,
                generation=generation,
                control_epoch=control_epoch,
                scope=scope,
                jti=jti or f"validation-{scope.value}-{uuid.uuid4().hex}",
                exp=int(time.time()) + 1800,
                owner=owner if control else None,
                operation=operation if control else None,
            )
            return sign_admission_token(
                claims,
                private_key=private_key,
                kid="validation",
            )

        def workload_headers(scope: AdmissionScope, generation: str) -> dict[str, str]:
            return {
                GPU_GENERATION_HEADER: generation,
                GPU_ADMISSION_TOKEN_HEADER: token(scope, generation=generation),
            }

        async def checked_post(
            client: httpx.AsyncClient,
            path: str,
            *,
            json_body: dict[str, Any],
            request_headers: dict[str, str],
        ) -> dict[str, Any]:
            response = await client.post(
                path,
                json=json_body,
                headers=request_headers,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"{path} returned {response.status_code}: {response.text[:1000]}"
                )
            return response.json()

        async def checked_health(client: httpx.AsyncClient) -> dict[str, Any]:
            challenge = secrets.token_hex(32)
            response = await client.get(
                "/health",
                headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
                params={GPU_HEALTH_CHALLENGE_QUERY_PARAM: challenge},
            )
            response.raise_for_status()
            if response.headers.get(GPU_HEALTH_CHALLENGE_HEADER) != challenge:
                raise AssertionError("health challenge was not echoed")
            health = response.json()
            if health["residency"]["boot_id"] != boot_id:
                raise AssertionError("health boot identity changed")
            if health.get("ort_session_counts") != expected_sessions:
                raise AssertionError("live ORT session counts differ from factories")
            return health

        progress["stage"] = "route_and_lifecycle_admission"
        async with _http_client(main.app) as client:
            await _bind_operator_routes(
                client, registry_id=registry_id, route_secret=route_secret
            )
            reset = await checked_post(
                client,
                "/lifecycle/reset",
                json_body={"control_epoch": "1"},
                request_headers={
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.RESET,
                        control_epoch="1",
                        operation="reset-1",
                    )
                },
            )
            assert reset["residency"]["gpu_loaded"] is False
            gc.collect()
            progress["stage"] = "cuda_context_baseline"
            _cuda_context = _initialize_cuda_context()
            memory["context_baseline_mb"] = _gpu_memory_samples(target_uuid)
            context_processes = _gpu_compute_processes(target_uuid)
            if len(context_processes) != 1:
                raise RuntimeError(
                    "validation requires exactly one no-model CUDA context process: "
                    f"{context_processes}"
                )
            memory["context_compute_processes"] = context_processes
            print(
                json.dumps(
                    {
                        "event": "cuda_context_baseline",
                        "gpu_uuid": target_uuid,
                        "memory_samples_mb": memory["context_baseline_mb"],
                        "compute_processes": context_processes,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )

            mode = await checked_post(
                client,
                "/lifecycle/mode",
                json_body={"gate": "enforce", "control_epoch": "2"},
                request_headers={
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.MODE,
                        control_epoch="2",
                        operation="mode-2",
                    )
                },
            )
            assert mode["gate"] == "enforce"
            setup_response = await client.get("/setup")
            setup_response.raise_for_status()
            setup = setup_response.json()
            if (
                setup.get("registry_id") != registry_id
                or setup.get("gpu_resource_id") != resource_id
                or setup.get("physical_gpu_uuid") != target_uuid
                or setup.get("runtime_info", {}).get("ort_session_counts")
                != expected_sessions
                or setup.get("pool_topology")
                != {name: {"cap": 1} for name in expected_sessions}
                or setup.get("runtime_info", {}).get("driver_version")
                != gpu["driver_version"]
                or setup.get("runtime_info", {}).get("total_memory_mb")
                != gpu["total_memory_mb"]
                or setup.get("runtime_info", {}).get("cuda_build") != cuda_version
            ):
                raise AssertionError("live plugin identity or factory topology differs")
            if "managed_lifecycle" not in setup:
                raise AssertionError("managed lifecycle capability was not advertised")
            contract_checks["managed_lifecycle_advertised"] = True

            progress["stage"] = "business_inference"
            for model_id in (
                "vehicle-attr",
                "vehicle-detect",
                "vehicle-attr-classify",
            ):
                input_path = (
                    artifacts["fixtures"]["boundary"]["path"]
                    if model_id == "vehicle-attr-classify"
                    else image_path
                )
                payload = await checked_post(
                    client,
                    "/predict",
                    json_body={
                        "tasks": [{"id": model_id, "file_path": input_path}],
                        "context": {"model_id": model_id},
                    },
                    request_headers=workload_headers(AdmissionScope.PREDICT, "1"),
                )
                _assert_predict_results(model_id, payload)
            for model_id in ("vehicle-attr", "vehicle-detect"):
                for fixture, check in (
                    ("empty", _assert_empty_result),
                    ("boundary", _assert_boundary_result),
                ):
                    payload = await checked_post(
                        client,
                        "/predict",
                        json_body={
                            "tasks": [
                                {
                                    "id": fixture,
                                    "file_path": artifacts["fixtures"][fixture]["path"],
                                }
                            ],
                            "context": {"model_id": model_id},
                        },
                        request_headers=workload_headers(AdmissionScope.PREDICT, "1"),
                    )
                    check(model_id, payload)
            contract_checks["real_inference"] = True

            provider_evidence = await _provider_evidence(main, expected_sessions)
            print(
                json.dumps(
                    {
                        "event": "onnx_session_device_options",
                        "cycle": 1,
                        "gpu_uuid": target_uuid,
                        "pools": provider_evidence,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            snapshot = await main._handle_pool.snapshot()  # noqa: SLF001
            assert snapshot["current_size"] == 3
            assert snapshot["session_count"] == 4
            assert snapshot["provider"] == "CUDAExecutionProvider"
            health = await checked_health(client)
            residency = health["residency"]
            assert health["compute"]["configured_device"] == "cuda:0"
            assert health["compute"]["effective_device"] == "cuda:0"
            assert health["compute"]["cpu_fallback_supported"] is False
            assert health["compute"]["effective_provider"] == "CUDAExecutionProvider"
            assert residency["state"] == "resident"
            assert residency["gpu_loaded"] is True
            assert residency["evictable"] is True
            assert all(
                residency["pools"][name]["resident"] is True
                and residency["pools"][name]["provider"] == "CUDAExecutionProvider"
                for name in expected_sessions
            )
            contract_checks["provider_or_device_gpu"] = True
            progress["stage"] = "first_unload"
            memory["cycle_1_loaded_mb"] = _gpu_memory_samples(target_uuid, count=3)

            drain_owner = "cycle-1"
            await checked_post(
                client,
                "/drain",
                json_body={"generation": "2"},
                request_headers={
                    GPU_GENERATION_HEADER: "2",
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.DRAIN,
                        generation="2",
                        operation=drain_owner,
                    ),
                },
            )
            unloaded = await checked_post(
                client,
                "/unload",
                json_body={"generation": "2"},
                request_headers={
                    GPU_GENERATION_HEADER: "2",
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.UNLOAD,
                        generation="2",
                        operation=drain_owner,
                    ),
                },
            )
            assert unloaded["unloaded_count"] == 3
            memory["cycle_1_unloaded_mb"] = _gpu_memory_samples(target_uuid)
            first_cycle = memory_cycle_evidence(
                cycle=1,
                generation="2",
                context_samples_mb=memory["context_baseline_mb"],
                loaded_samples_mb=memory["cycle_1_loaded_mb"],
                unloaded_samples_mb=memory["cycle_1_unloaded_mb"],
            )
            first_cycle["session_observations"] = {
                name: [session["providers"] for session in sessions]
                for name, sessions in provider_evidence.items()
            }
            progress["cycles"].append(first_cycle)
            _assert_unloaded(
                residency=unloaded["residency"],
                snapshot=await main._handle_pool.snapshot(),  # noqa: SLF001
                samples=memory["cycle_1_unloaded_mb"],
            )
            progress["stage"] = "second_load_and_faults"

            for model_id in (
                "vehicle-attr",
                "vehicle-detect",
                "vehicle-attr-classify",
            ):
                await checked_post(
                    client,
                    "/warmup",
                    json_body={"model_id": model_id},
                    request_headers=workload_headers(AdmissionScope.WARMUP, "3"),
                )
            second_providers = await _provider_evidence(main, expected_sessions)
            print(
                json.dumps(
                    {
                        "event": "onnx_session_device_options",
                        "cycle": 2,
                        "gpu_uuid": target_uuid,
                        "pools": second_providers,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            if second_providers != provider_evidence:
                raise AssertionError(
                    "provider chains changed across load generations: "
                    f"first={provider_evidence}, second={second_providers}"
                )
            second_health = await checked_health(client)
            if not all(
                second_health["residency"]["pools"][name]["resident"] is True
                and second_health["residency"]["pools"][name]["provider"]
                == "CUDAExecutionProvider"
                for name in expected_sessions
            ):
                raise AssertionError("second cold load did not restore every CUDA pool")
            memory["cycle_2_loaded_mb"] = _gpu_memory_samples(target_uuid, count=3)

            warmup_body = {"model_id": "vehicle-detect"}
            drain_owner = "cycle-2"
            async with main._handle_pool.borrow("pipeline") as lease:  # noqa: SLF001
                contract_checks.update(
                    await exercise_lifecycle_fault_matrix(
                        client=client,
                        lifecycle=main._gpu_lifecycle,  # noqa: SLF001
                        token=token,
                        workload_scope=AdmissionScope.WARMUP,
                        drain_scope=AdmissionScope.DRAIN,
                        unload_scope=AdmissionScope.UNLOAD,
                        workload_path="/warmup",
                        workload_body=warmup_body,
                        current_generation="3",
                        stale_generation="1",
                        next_generation="4",
                        drain_owner=drain_owner,
                    )
                )
                busy_unload = await client.post(
                    "/unload",
                    json={"generation": "4"},
                    headers={
                        GPU_GENERATION_HEADER: "4",
                        GPU_ADMISSION_TOKEN_HEADER: token(
                            AdmissionScope.UNLOAD,
                            generation="4",
                            operation=drain_owner,
                        ),
                    },
                )
                if busy_unload.status_code != 409:
                    raise AssertionError(
                        "unload did not reject the active pipeline borrower"
                    )
                busy_health = await checked_health(client)
                if (
                    busy_health["residency"]["active_requests"] != 0
                    or busy_health["residency"]["borrowers"] != 1
                    or (await main._handle_pool.snapshot())["current_size"] != 3  # noqa: SLF001
                ):
                    raise AssertionError(
                        "busy unload did not preserve the borrowed handles"
                    )
                if main.inspect_handle_providers("pipeline", lease.handle) != [
                    session["providers"] for session in second_providers["pipeline"]
                ]:
                    raise AssertionError(
                        "busy unload changed the borrowed ORT sessions"
                    )
            # Exiting the borrow context does not clear the lease's strong handle
            # reference; release it before measuring actual ORT session teardown.
            del lease
            progress["stage"] = "second_unload"
            unloaded = await checked_post(
                client,
                "/unload",
                json_body={"generation": "4"},
                request_headers={
                    GPU_GENERATION_HEADER: "4",
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.UNLOAD,
                        generation="4",
                        operation=drain_owner,
                    ),
                },
            )
            assert unloaded["unloaded_count"] == 3
            final_health = await checked_health(client)
            progress["final_residency"] = final_health["residency"]
            memory["cycle_2_unloaded_mb"] = _gpu_memory_samples(target_uuid)
            second_cycle = memory_cycle_evidence(
                cycle=2,
                generation="4",
                context_samples_mb=memory["context_baseline_mb"],
                loaded_samples_mb=memory["cycle_2_loaded_mb"],
                unloaded_samples_mb=memory["cycle_2_unloaded_mb"],
            )
            second_cycle["session_observations"] = {
                name: [session["providers"] for session in sessions]
                for name, sessions in second_providers.items()
            }
            progress["cycles"].append(second_cycle)
            final_snapshot = await main._handle_pool.snapshot()  # noqa: SLF001
            _assert_unloaded(
                residency=final_health["residency"],
                snapshot=final_snapshot,
                samples=memory["cycle_2_unloaded_mb"],
            )
            contract_checks["full_cleanup"] = True

        print(
            json.dumps(
                {
                    "event": "onnx_memory_samples",
                    "gpu_uuid": target_uuid,
                    "memory_samples_mb": memory,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        progress["stage"] = "memory_recovery"
        _assert_memory_recovery(memory)
        progress["stage"] = "report_validation"
        if setup["runtime_info"]["runtime_versions"]["cuda"] != cuda_version:
            raise AssertionError("live CUDA runtime version differs from measurement")
        return build_managed_lifecycle_evidence(
            backend_name=progress["backend_name"],
            deployment=progress["deployment"],
            artifacts=progress["artifacts"],
            gpu=progress["gpu"],
            cycles=progress["cycles"],
            contract_checks=contract_checks,
            final_residency=progress["final_residency"],
            runtime_ephemera_clean=True,
        )


async def _run(progress: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="onnxtools-qualification-") as private_dir:
        return await _run_with_authority(Path(private_dir), progress)


def _initial_evidence() -> dict[str, Any]:
    return {
        "stage": "configuration",
        "backend_name": "onnxtools-backend",
        "deployment": {
            "git_commit": None,
            "image_id": None,
            "runtime_versions": {"python": platform.python_version()},
            "pool_topology": {},
        },
        "artifacts": [],
        "gpu": {
            "uuid": None,
            "total_memory_mb": None,
            "driver_version": None,
            "runtime_version": None,
            "visible_device_count": None,
        },
        "cycles": [],
        "contract_checks": {name: False for name in REQUIRED_CONTRACT_CHECKS},
        "final_residency": None,
    }


def _failed_evidence(exc: Exception, progress: dict[str, Any]) -> dict[str, Any]:
    return build_managed_lifecycle_evidence(
        backend_name=progress["backend_name"],
        deployment=progress["deployment"],
        artifacts=progress["artifacts"],
        gpu=progress["gpu"],
        cycles=progress["cycles"],
        contract_checks=progress["contract_checks"],
        final_residency=progress["final_residency"],
        runtime_ephemera_clean=False,
        blockers=[
            {
                "code": "validation_exception",
                "message": f"validation failed at {progress['stage']} with {type(exc).__name__}",
            }
        ],
    )


def main() -> None:
    progress = _initial_evidence()
    try:
        with redirect_stdout(sys.stderr):
            evidence = asyncio.run(_run(progress))
    except Exception as exc:  # noqa: BLE001 - emit fail-closed evidence before exit
        print(
            f"managed lifecycle validation failed at {progress['stage']}: {type(exc).__name__}",
            file=sys.stderr,
        )
        print(
            json.dumps(
                _failed_evidence(exc, progress),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
            end="",
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ),
        end="",
    )


if __name__ == "__main__":
    main()

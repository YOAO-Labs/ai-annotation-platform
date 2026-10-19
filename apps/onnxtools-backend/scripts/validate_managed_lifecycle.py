"""Validate ONNXTools' managed lifecycle with approved models on a real GPU.

The gate is intentionally fail-closed. Both model files must match separately
approved SHA-256 digests, and the input must contain a vehicle that the composite
pipeline can detect and classify. The script emits one JSON evidence document to
stdout and leaves the service's deployment flags unchanged.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from statistics import median
from typing import Any

import httpx
from aap_backend_runtime.backend_auth import backend_auth_headers
from aap_backend_runtime import (
    REQUIRED_CONTRACT_CHECKS,
    artifact_evidence,
    build_managed_lifecycle_evidence,
    exercise_lifecycle_fault_matrix,
    memory_cycle_evidence,
)
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    encode_ed25519_public_key,
    sign_admission_token,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


CONTROL_SCOPES = {
    AdmissionScope.DRAIN,
    AdmissionScope.UNLOAD,
    AdmissionScope.RESUME,
    AdmissionScope.MODE,
    AdmissionScope.RESET,
}
REGISTRY_ID = "onnxtools-validation"
RESOURCE_ID = "local-validation-gpu"
EXPECTED_SESSIONS = {"pipeline": 2, "detector": 1, "va": 1}
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
    model_dir = Path(os.getenv("ONNXTOOLS_MODEL_DIR", "/app/models"))
    det_path = model_dir / os.getenv("ONNXTOOLS_DET_MODEL", "rtdetr-2024080100.onnx")
    va_path = model_dir / os.getenv("ONNXTOOLS_VA_MODEL", "va_260612.onnx")
    image_path = Path(_required_env("VALIDATION_IMAGE_PATH"))
    if not image_path.is_file() or image_path.stat().st_size <= 0:
        raise RuntimeError(
            f"VALIDATION_IMAGE_PATH must be a non-empty file: {image_path}"
        )

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
        "validation_image": {
            "path": str(image_path),
            "size": image_path.stat().st_size,
            "sha256": _sha256(image_path),
        },
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
    if target_uuid:
        for row in rows:
            if row[0] == target_uuid:
                return row
        raise RuntimeError(f"GPU UUID {target_uuid!r} is not visible")
    if len(rows) != 1:
        raise RuntimeError(
            "VALIDATION_GPU_UUID is required when multiple GPUs are visible"
        )
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
        isinstance(item.get("attributes"), dict) for item in items
    ):
        raise AssertionError(f"{model_id} did not execute vehicle classification")


async def _provider_evidence(main_module: Any) -> dict[str, list[list[str]]]:
    assert main_module._handle_pool is not None  # noqa: SLF001
    evidence: dict[str, list[list[str]]] = {}
    for name, expected_count in EXPECTED_SESSIONS.items():
        async with main_module._handle_pool.borrow(name) as lease:  # noqa: SLF001
            chains = main_module.inspect_handle_providers(name, lease.handle)
        if chains is None or len(chains) != expected_count:
            raise AssertionError(
                f"{name} provider evidence is incomplete: expected "
                f"{expected_count}, got {chains}"
            )
        if any(not chain or chain[0] != "CUDAExecutionProvider" for chain in chains):
            raise AssertionError(f"{name} silently fell back from CUDA: {chains}")
        evidence[name] = chains
    return evidence


async def _run() -> dict[str, Any]:
    git_commit = _required_env("VALIDATION_GIT_COMMIT")
    image_id = _required_env("VALIDATION_IMAGE_ID")
    fixture_approval_ref = _required_env("VALIDATION_FIXTURE_APPROVAL_REF")
    artifacts = _validate_artifacts()
    requested_uuid = os.getenv("VALIDATION_GPU_UUID", "").strip() or None
    gpu = _gpu_metadata(requested_uuid)
    target_uuid = gpu["uuid"]
    cold_baseline = _assert_isolated_before_start(target_uuid)

    private_key = Ed25519PrivateKey.generate()
    os.environ["GPU_LIFECYCLE_VERIFY_KEYS_JSON"] = json.dumps(
        {"validation": encode_ed25519_public_key(private_key.public_key())}
    )
    os.environ["ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED"] = "1"
    os.environ["ONNXTOOLS_IDLE_UNLOAD_SECONDS"] = "0"
    os.environ["ONNXTOOLS_BUILD_TIMEOUT"] = "300"

    app_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(app_root))
    import main  # noqa: PLC0415

    contract_checks = {name: False for name in REQUIRED_CONTRACT_CHECKS}
    memory: dict[str, Any] = {
        "cold_card_baseline": cold_baseline,
        "gpu_total_mb": _gpu_total_memory_mb(target_uuid),
    }
    image_path = artifacts["validation_image"]["path"]

    async with main.lifespan(main.app):
        assert main._gpu_lifecycle is not None  # noqa: SLF001
        assert main._handle_pool is not None  # noqa: SLF001
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
                backend_registry_id=REGISTRY_ID,
                gpu_resource_id=RESOURCE_ID,
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

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            headers=backend_auth_headers("onnxtools"),
            base_url="http://onnxtools-validation",
            timeout=600,
        ) as client:
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
            memory["context_baseline_mb"] = _gpu_memory_samples(target_uuid)
            context_processes = _gpu_compute_processes(target_uuid)
            if len(context_processes) > 1:
                raise RuntimeError(
                    "validation process is not isolated after ORT provider probe: "
                    f"{context_processes}"
                )
            memory["context_compute_processes"] = context_processes

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
            setup = (await client.get("/setup")).json()
            if "managed_lifecycle" not in setup:
                raise AssertionError("managed lifecycle capability was not advertised")
            contract_checks["managed_lifecycle_advertised"] = True

            for model_id in (
                "vehicle-attr",
                "vehicle-detect",
                "vehicle-attr-classify",
            ):
                payload = await checked_post(
                    client,
                    "/predict",
                    json_body={
                        "tasks": [{"id": model_id, "file_path": image_path}],
                        "context": {"model_id": model_id},
                    },
                    request_headers=workload_headers(AdmissionScope.PREDICT, "1"),
                )
                _assert_predict_results(model_id, payload)
            contract_checks["real_inference"] = True

            provider_evidence = await _provider_evidence(main)
            snapshot = await main._handle_pool.snapshot()  # noqa: SLF001
            assert snapshot["current_size"] == 3
            assert snapshot["session_count"] == 4
            assert snapshot["provider"] == "CUDAExecutionProvider"
            health = (await client.get("/health")).json()
            residency = health["residency"]
            assert health["compute"]["configured_device"] == "cuda"
            assert health["compute"]["effective_provider"] == "CUDAExecutionProvider"
            assert residency["state"] == "resident"
            assert residency["gpu_loaded"] is True
            assert residency["evictable"] is True
            assert all(
                residency["pools"][name]["resident"] is True
                and residency["pools"][name]["provider"] == "CUDAExecutionProvider"
                for name in EXPECTED_SESSIONS
            )
            contract_checks["provider_or_device_gpu"] = True
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
            _assert_unloaded(
                residency=unloaded["residency"],
                snapshot=await main._handle_pool.snapshot(),  # noqa: SLF001
                samples=memory["cycle_1_unloaded_mb"],
            )

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
            second_providers = await _provider_evidence(main)
            if second_providers != provider_evidence:
                raise AssertionError(
                    "provider chains changed across load generations: "
                    f"first={provider_evidence}, second={second_providers}"
                )
            memory["cycle_2_loaded_mb"] = _gpu_memory_samples(target_uuid, count=3)

            warmup_body = {"model_id": "vehicle-detect"}
            drain_owner = "cycle-2"
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
            final_health = (await client.get("/health")).json()
            memory["cycle_2_unloaded_mb"] = _gpu_memory_samples(target_uuid)
            final_snapshot = await main._handle_pool.snapshot()  # noqa: SLF001
            _assert_unloaded(
                residency=final_health["residency"],
                snapshot=final_snapshot,
                samples=memory["cycle_2_unloaded_mb"],
            )
            contract_checks["full_cleanup"] = True

        _assert_memory_recovery(memory)
        approval_ref = artifacts["approval_ref"]
        artifact_records = [
            artifact_evidence(
                artifacts["detector"]["path"],
                kind="weight",
                approval_ref=approval_ref,
            ),
            artifact_evidence(
                artifacts["vehicle_attribute"]["path"],
                kind="weight",
                approval_ref=approval_ref,
            ),
            artifact_evidence(
                artifacts["validation_image"]["path"],
                kind="fixture",
                approval_ref=fixture_approval_ref,
            ),
        ]
        onnxruntime_version = str(__import__("onnxruntime").__version__)
        cuda_version = os.getenv("CUDA_VERSION", "").strip()
        if not cuda_version:
            raise RuntimeError("CUDA_VERSION is required from the deployment image")
        gpu["runtime_version"] = cuda_version
        cycles = [
            memory_cycle_evidence(
                cycle=1,
                generation="2",
                context_samples_mb=memory["context_baseline_mb"],
                loaded_samples_mb=memory["cycle_1_loaded_mb"],
                unloaded_samples_mb=memory["cycle_1_unloaded_mb"],
            ),
            memory_cycle_evidence(
                cycle=2,
                generation="4",
                context_samples_mb=memory["context_baseline_mb"],
                loaded_samples_mb=memory["cycle_2_loaded_mb"],
                unloaded_samples_mb=memory["cycle_2_unloaded_mb"],
            ),
        ]
        return build_managed_lifecycle_evidence(
            backend_name="onnxtools-backend",
            deployment={
                "git_commit": git_commit,
                "image_id": image_id,
                "runtime_versions": {
                    "python": platform.python_version(),
                    "onnxruntime": onnxruntime_version,
                    "cuda": cuda_version,
                    "backend": str(main.BACKEND_VERSION),
                },
                "pool_topology": {
                    name: {"handle_cap": 1, "expected_sessions": count}
                    for name, count in EXPECTED_SESSIONS.items()
                },
            },
            artifacts=artifact_records,
            gpu=gpu,
            cycles=cycles,
            contract_checks=contract_checks,
            final_residency=final_health["residency"],
            runtime_ephemera_clean=True,
        )


def _failed_evidence(exc: Exception) -> dict[str, Any]:
    return build_managed_lifecycle_evidence(
        backend_name="onnxtools-backend",
        deployment={
            "git_commit": os.getenv("VALIDATION_GIT_COMMIT") or None,
            "image_id": os.getenv("VALIDATION_IMAGE_ID") or None,
            "runtime_versions": {"python": platform.python_version()},
            "pool_topology": {},
        },
        artifacts=[],
        gpu={
            "uuid": os.getenv("VALIDATION_GPU_UUID") or None,
            "total_memory_mb": None,
            "driver_version": None,
            "runtime_version": None,
            "visible_device_count": None,
        },
        cycles=[],
        contract_checks={name: False for name in REQUIRED_CONTRACT_CHECKS},
        final_residency=None,
        runtime_ephemera_clean=False,
        blockers=[
            {
                "code": "validation_exception",
                "message": f"validation failed with {type(exc).__name__}",
            }
        ],
    )


def main() -> None:
    try:
        with redirect_stdout(sys.stderr):
            evidence = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - emit fail-closed evidence before exit
        print(f"managed lifecycle validation failed: {exc}", file=sys.stderr)
        print(json.dumps(_failed_evidence(exc), ensure_ascii=False, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

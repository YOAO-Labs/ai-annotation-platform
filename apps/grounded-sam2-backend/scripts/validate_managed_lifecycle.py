"""Validate Grounded-SAM2 image/video pools on the target deployment GPU."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import httpx
import numpy as np
from PIL import Image, ImageDraw
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
REGISTRY_ID = "grounded-sam2-validation"
RESOURCE_ID = "local-validation-gpu"


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


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


def _gpu_metadata(target_uuid: str | None) -> dict[str, Any]:
    rows = _gpu_query("uuid,memory.used,memory.total,driver_version")
    if target_uuid:
        matching = [row for row in rows if row[0] == target_uuid]
        if len(matching) != 1:
            raise RuntimeError(f"GPU UUID {target_uuid!r} is not visible")
        row = matching[0]
    elif len(rows) == 1:
        row = rows[0]
    else:
        raise RuntimeError(
            "VALIDATION_GPU_UUID is required when multiple GPUs are visible"
        )
    return {
        "uuid": row[0],
        "total_memory_mb": int(row[2]),
        "driver_version": row[3],
        "visible_device_count": len(rows),
    }


def _gpu_memory_mb(target_uuid: str) -> int:
    for row in _gpu_query("uuid,memory.used"):
        if row[0] == target_uuid:
            return int(row[1])
    raise RuntimeError(f"GPU UUID {target_uuid!r} is not visible")


def _gpu_memory_samples(target_uuid: str, *, count: int = 5) -> list[int]:
    samples = []
    for _ in range(count):
        samples.append(_gpu_memory_mb(target_uuid))
        time.sleep(0.5)
    return samples


def _gpu_compute_processes(target_uuid: str) -> list[dict[str, int | str]]:
    output = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    )
    processes = []
    for line in output.splitlines():
        if not line.strip():
            continue
        gpu_uuid, pid, used = (part.strip() for part in line.split(",", 2))
        if gpu_uuid == target_uuid:
            processes.append(
                {"gpu_uuid": gpu_uuid, "pid": int(pid), "used_memory_mb": int(used)}
            )
    return processes


def _torch_memory(torch_module: Any) -> dict[str, int]:
    torch_module.cuda.synchronize()
    return {
        "allocated_bytes": torch_module.cuda.memory_allocated(),
        "reserved_bytes": torch_module.cuda.memory_reserved(),
    }


def _initialize_context_baseline(torch_module: Any, target_uuid: str) -> list[int]:
    marker = torch_module.empty(1, device="cuda")
    torch_module.cuda.synchronize()
    del marker
    gc.collect()
    torch_module.cuda.empty_cache()
    torch_module.cuda.ipc_collect()
    if any(_torch_memory(torch_module).values()):
        raise AssertionError("context probe retained PyTorch allocator memory")
    return _gpu_memory_samples(target_uuid)


def _make_inputs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    image_path = root / "fixture.png"
    image = Image.new("RGB", (512, 512), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((96, 128, 352, 416), fill="red")
    image.save(image_path)
    video_path = root / "fixture.mp4"
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        4.0,
        (512, 512),
    )
    if not writer.isOpened():
        raise RuntimeError("failed to create validation video")
    try:
        for index in range(5):
            frame = np.full((512, 512, 3), 255, dtype=np.uint8)
            x = 80 + index * 16
            cv2.rectangle(frame, (x, 128), (x + 240, 400), (0, 0, 255), -1)
            writer.write(frame)
    finally:
        writer.release()
    return image_path, video_path


def _assert_unloaded(residency: dict[str, Any], snapshot: dict[str, Any]) -> None:
    assert residency["state"] == "unloaded"
    assert residency["gpu_loaded"] is False
    assert residency["evictable"] is False
    assert residency["active_requests"] == 0
    assert residency["builders"] == 0
    assert residency["borrowers"] == 0
    assert all(pool["resident"] is False for pool in residency["pools"].values())
    for pool in snapshot["pools"].values():
        assert pool["current_size"] == 0
        assert pool["builders"] == 0
        assert pool["borrowers"] == 0
        assert pool["waiters"] == 0
        assert pool["cleanup_in_progress"] is False
        assert pool["cleanup_failed"] is False
        assert pool["gpu_resident"] is False


def _assert_baseline_recovery(
    context: list[int], first: list[int], second: list[int], total_mb: int
) -> None:
    if abs(median(first) - median(second)) > 64:
        raise AssertionError("unloaded GPU baseline drifted between generations")
    slack_mb = max(512, int(total_mb * 0.02))
    if any(median(samples) > median(context) + slack_mb for samples in (first, second)):
        raise AssertionError("unloaded GPU memory did not return to context baseline")


def _approved_artifacts(
    *,
    checkpoint_dir: Path,
    filenames: list[str],
    approval_ref: str,
) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(_required_env("VALIDATION_WEIGHT_SHA256_JSON"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("VALIDATION_WEIGHT_SHA256_JSON must be valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != set(filenames):
        raise RuntimeError("weight manifest must contain exactly the validated weights")
    records = []
    for filename in filenames:
        record = artifact_evidence(
            checkpoint_dir / filename,
            kind="weight",
            approval_ref=approval_ref,
        )
        if record["sha256"] != manifest[filename]:
            raise RuntimeError(f"{filename} does not match the approved SHA-256")
        records.append(record)
    return records


async def _run() -> dict[str, Any]:
    git_commit = _required_env("VALIDATION_GIT_COMMIT")
    image_id = _required_env("VALIDATION_IMAGE_ID")
    model_approval_ref = _required_env("VALIDATION_MODEL_APPROVAL_REF")
    fixture_approval_ref = _required_env("VALIDATION_FIXTURE_APPROVAL_REF")

    private_key = Ed25519PrivateKey.generate()
    os.environ["GPU_LIFECYCLE_VERIFY_KEYS_JSON"] = json.dumps(
        {"validation": encode_ed25519_public_key(private_key.public_key())}
    )
    os.environ["GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED"] = "1"
    os.environ["IDLE_UNLOAD_SECONDS"] = "0"
    os.environ["VIDEO_IDLE_UNLOAD_SECONDS"] = "0"
    os.environ["MODEL_POOL_BUILD_TIMEOUT"] = "300"
    os.environ["VIDEO_MODEL_POOL_BUILD_TIMEOUT"] = "300"

    app_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(app_root))
    import main  # noqa: PLC0415

    validation_root = Path(tempfile.mkdtemp(prefix="gsam2_lifecycle_validation_"))
    image_path, video_path = _make_inputs(validation_root)
    gpu = _gpu_metadata(os.getenv("VALIDATION_GPU_UUID") or None)
    target_uuid = gpu["uuid"]
    memory: dict[str, Any] = {"gpu_total_mb": gpu["total_memory_mb"]}
    contract_checks = {name: False for name in REQUIRED_CONTRACT_CHECKS}

    try:
        await main._load_models()  # noqa: SLF001
        assert main._gpu_lifecycle is not None  # noqa: SLF001
        assert main._pool_domain is not None  # noqa: SLF001
        boot_id = main._gpu_lifecycle.boot_id  # noqa: SLF001
    except BaseException:
        try:
            await main._shutdown()  # noqa: SLF001
        finally:
            shutil.rmtree(validation_root, ignore_errors=True)
        raise

    def token(
        scope: AdmissionScope,
        *,
        generation: str | None = None,
        control_epoch: str = "3",
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
        return sign_admission_token(claims, private_key=private_key, kid="validation")

    def headers(scope: AdmissionScope, generation: str) -> dict[str, str]:
        return {
            GPU_GENERATION_HEADER: generation,
            GPU_ADMISSION_TOKEN_HEADER: token(scope, generation=generation),
        }

    async def checked_post(
        client: httpx.AsyncClient,
        path: str,
        body: dict[str, Any],
        request_headers: dict[str, str],
    ) -> dict[str, Any]:
        response = await client.post(path, json=body, headers=request_headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"{path} returned {response.status_code}: {response.text[:1000]}"
            )
        return response.json()

    image_predict_body = {
        "task": {"file_path": str(image_path)},
        "context": {
            "type": "text",
            "text": "red rectangle",
            "output": "both",
        },
    }
    video_predict_body = {
        "task": {"file_path": str(video_path)},
        "context": {
            "type": "video_tracker",
            "from_frame": 0,
            "to_frame": 4,
            "direction": "forward",
            "output_geometry": "bbox",
            "source_geometry": {
                "type": "bbox",
                "x": 0.16,
                "y": 0.25,
                "w": 0.47,
                "h": 0.53,
            },
        },
    }
    transport = httpx.ASGITransport(app=main.app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            headers=backend_auth_headers("gsam2"),
            base_url="http://grounded-sam2-validation",
            timeout=600,
        ) as client:
            await checked_post(
                client,
                "/lifecycle/reset",
                {"control_epoch": "1"},
                {
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.RESET,
                        control_epoch="1",
                        operation="reset-1",
                    )
                },
            )
            if _gpu_compute_processes(target_uuid):
                raise RuntimeError("validation GPU is not isolated before CUDA init")
            memory["cold_context_baseline_mb"] = _initialize_context_baseline(
                main.torch, target_uuid
            )
            if len(_gpu_compute_processes(target_uuid)) != 1:
                raise RuntimeError("validator is not the sole GPU compute process")
            # Prime both real inference roots once so the measured baseline includes
            # process-level CUDA/cuDNN state that survives a complete pool unload.
            await checked_post(client, "/predict", image_predict_body, {})
            await checked_post(client, "/predict", video_predict_body, {})
            await main._pool_domain.unload_all(  # noqa: SLF001
                reason="manual",
                force_cleanup=True,
            )
            primed_snapshot = await main._pool_domain.snapshot()  # noqa: SLF001
            if primed_snapshot["current_size"] != 0:
                raise AssertionError(
                    "Grounded-SAM2 context priming did not empty both pools"
                )
            primed_torch_memory = _torch_memory(main.torch)
            if any(primed_torch_memory.values()):
                raise AssertionError(
                    "Grounded-SAM2 context priming retained PyTorch memory: "
                    f"{primed_torch_memory}"
                )
            memory["context_baseline_mb"] = _gpu_memory_samples(target_uuid)
            # Legacy priming intentionally exercised both public inference roots,
            # which makes the lifecycle controller distrust its earlier empty
            # observation. Re-establish that trust with a fresh signed reset only
            # after both pools and PyTorch allocations have been proven empty.
            await checked_post(
                client,
                "/lifecycle/reset",
                {"control_epoch": "2"},
                {
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.RESET,
                        control_epoch="2",
                        operation="reset-2",
                    )
                },
            )
            await checked_post(
                client,
                "/lifecycle/mode",
                {"gate": "enforce", "control_epoch": "3"},
                {
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.MODE,
                        control_epoch="3",
                        operation="mode-3",
                    )
                },
            )
            setup = (await client.get("/setup")).json()
            if "managed_lifecycle" not in setup:
                raise AssertionError("managed lifecycle capability was not advertised")
            contract_checks["managed_lifecycle_advertised"] = True

            await checked_post(
                client,
                "/predict",
                image_predict_body,
                headers(AdmissionScope.PREDICT, "1"),
            )
            await checked_post(
                client,
                "/predict",
                video_predict_body,
                headers(AdmissionScope.PREDICT, "1"),
            )
            contract_checks["real_inference"] = True
            health = (await client.get("/health")).json()
            effective = health["compute"]["effective_device"]
            if not effective or not effective.startswith("cuda"):
                raise AssertionError(
                    f"Grounded-SAM2 did not remain on CUDA: {health['compute']}"
                )
            if health["residency"]["evictable"] is not True:
                raise AssertionError("resident Grounded-SAM2 pools were not evictable")
            if not all(
                health["residency"]["pools"][name]["resident"] is True
                for name in ("image", "video")
            ):
                raise AssertionError("image/video pools were not both resident")
            contract_checks["provider_or_device_gpu"] = True
            memory["cycle_1_loaded_mb"] = _gpu_memory_samples(target_uuid, count=3)

            await checked_post(
                client,
                "/drain",
                {"generation": "2"},
                {
                    GPU_GENERATION_HEADER: "2",
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.DRAIN,
                        generation="2",
                        operation="cycle-1",
                    ),
                },
            )
            first_unload = await checked_post(
                client,
                "/unload",
                {"generation": "2"},
                {
                    GPU_GENERATION_HEADER: "2",
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.UNLOAD,
                        generation="2",
                        operation="cycle-1",
                    ),
                },
            )
            memory["cycle_1_unloaded_mb"] = _gpu_memory_samples(target_uuid)
            _assert_unloaded(
                first_unload["residency"],
                await main._pool_domain.snapshot(),  # noqa: SLF001
            )

            image_warmup = {
                "task": "segmentation",
                "variants": {
                    "sam_variant": main.SAM_VARIANT,
                    "dino_variant": main.DINO_VARIANT,
                },
            }
            await checked_post(
                client,
                "/warmup",
                image_warmup,
                headers(AdmissionScope.WARMUP, "3"),
            )
            await checked_post(
                client,
                "/warmup",
                {
                    "task": "tracker",
                    "variants": {"sam_variant": main.SAM_VARIANT},
                },
                headers(AdmissionScope.WARMUP, "3"),
            )
            memory["cycle_2_loaded_mb"] = _gpu_memory_samples(target_uuid, count=3)
            contract_checks.update(
                await exercise_lifecycle_fault_matrix(
                    client=client,
                    lifecycle=main._gpu_lifecycle,  # noqa: SLF001
                    token=token,
                    workload_scope=AdmissionScope.WARMUP,
                    drain_scope=AdmissionScope.DRAIN,
                    unload_scope=AdmissionScope.UNLOAD,
                    workload_path="/warmup",
                    workload_body=image_warmup,
                    current_generation="3",
                    stale_generation="1",
                    next_generation="4",
                    drain_owner="cycle-2",
                )
            )
            second_unload = await checked_post(
                client,
                "/unload",
                {"generation": "4"},
                {
                    GPU_GENERATION_HEADER: "4",
                    GPU_ADMISSION_TOKEN_HEADER: token(
                        AdmissionScope.UNLOAD,
                        generation="4",
                        operation="cycle-2",
                    ),
                },
            )
            final_health = (await client.get("/health")).json()
            memory["cycle_2_unloaded_mb"] = _gpu_memory_samples(target_uuid)
            final_snapshot = await main._pool_domain.snapshot()  # noqa: SLF001
            _assert_unloaded(final_health["residency"], final_snapshot)
            if second_unload["unloaded_count"] != 2:
                raise AssertionError("managed unload did not clear both pools")
            if final_health["cache"]["size"] != 0:
                raise AssertionError("embedding cache remained populated")
            if any(_torch_memory(main.torch).values()):
                raise AssertionError("Grounded-SAM2 unload retained PyTorch memory")
            contract_checks["full_cleanup"] = True

        _assert_baseline_recovery(
            memory["context_baseline_mb"],
            memory["cycle_1_unloaded_mb"],
            memory["cycle_2_unloaded_mb"],
            memory["gpu_total_mb"],
        )
        checkpoint_dir = Path(main.CHECKPOINT_DIR)
        sam_filename = main.SAM2_CONFIGS[main.SAM_VARIANT][1]
        dino_filename = main.DINO_CONFIGS[main.DINO_VARIANT][1]
        artifacts = _approved_artifacts(
            checkpoint_dir=checkpoint_dir,
            filenames=[sam_filename, dino_filename],
            approval_ref=model_approval_ref,
        )
        artifacts.extend(
            [
                artifact_evidence(
                    image_path,
                    kind="fixture",
                    approval_ref=fixture_approval_ref,
                ),
                artifact_evidence(
                    video_path,
                    kind="fixture",
                    approval_ref=fixture_approval_ref,
                ),
            ]
        )
        gpu["runtime_version"] = str(main.torch.version.cuda)
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
        pool_topology = {
            pool_id: {"cap": int(snapshot["cap"])}
            for pool_id, snapshot in final_snapshot["pools"].items()
        }
        return build_managed_lifecycle_evidence(
            backend_name="grounded-sam2-backend",
            deployment={
                "git_commit": git_commit,
                "image_id": image_id,
                "runtime_versions": {
                    "python": platform.python_version(),
                    "torch": str(main.torch.__version__),
                    "cuda": str(main.torch.version.cuda),
                    "backend": str(main.BACKEND_VERSION),
                },
                "pool_topology": pool_topology,
            },
            artifacts=artifacts,
            gpu=gpu,
            cycles=cycles,
            contract_checks=contract_checks,
            final_residency=final_health["residency"],
            runtime_ephemera_clean=True,
        )
    finally:
        try:
            await main._shutdown()  # noqa: SLF001
        finally:
            shutil.rmtree(validation_root, ignore_errors=True)


def _failed_evidence(exc: Exception) -> dict[str, Any]:
    return build_managed_lifecycle_evidence(
        backend_name="grounded-sam2-backend",
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

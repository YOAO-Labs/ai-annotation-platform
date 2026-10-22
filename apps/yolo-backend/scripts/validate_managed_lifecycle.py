"""Validate YOLO's managed full-pool lifecycle on the target deployment GPU."""

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
from functools import partial
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
from training_contract.assets import resolve_prepared_asset


CONTROL_SCOPES = {
    AdmissionScope.DRAIN,
    AdmissionScope.UNLOAD,
    AdmissionScope.RESUME,
    AdmissionScope.MODE,
    AdmissionScope.RESET,
}
REGISTRY_ID = "yolo-validation"
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
    return int(
        next(row[1] for row in _gpu_query("uuid,memory.used") if row[0] == target_uuid)
    )


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


def _torch_memory_mb(torch_module: Any) -> dict[str, int]:
    torch_module.cuda.synchronize()
    allocated = torch_module.cuda.memory_allocated()
    reserved = torch_module.cuda.memory_reserved()
    return {
        "allocated_bytes": allocated,
        "reserved_bytes": reserved,
    }


def _initialize_context_baseline(torch_module: Any, target_uuid: str) -> list[int]:
    marker = torch_module.empty(1, device="cuda")
    torch_module.cuda.synchronize()
    del marker
    gc.collect()
    torch_module.cuda.empty_cache()
    torch_module.cuda.ipc_collect()
    torch_memory = _torch_memory_mb(torch_module)
    if torch_memory["allocated_bytes"] or torch_memory["reserved_bytes"]:
        raise AssertionError(f"context probe retained PyTorch memory: {torch_memory}")
    return _gpu_memory_samples(target_uuid)


def _make_inputs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    image_path = root / "fixture.png"
    image = Image.new("RGB", (640, 640), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((120, 180, 420, 520), fill="red")
    draw.ellipse((340, 100, 560, 320), fill="blue")
    image.save(image_path)

    video_path = root / "fixture.mp4"
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        4.0,
        (640, 640),
    )
    if not writer.isOpened():
        raise RuntimeError("failed to create validation video")
    try:
        for index in range(6):
            frame = np.full((640, 640, 3), 255, dtype=np.uint8)
            x = 100 + index * 20
            cv2.rectangle(frame, (x, 180), (x + 280, 500), (0, 0, 255), -1)
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
    assert snapshot["current_size"] == 0
    assert snapshot["builders"] == 0
    assert snapshot["borrowers"] == 0
    assert snapshot["waiters"] == 0
    assert snapshot["cleanup_in_progress"] is False
    assert snapshot["cleanup_failed"] is False
    assert snapshot["gpu_resident"] is False


def _assert_baseline_recovery(
    context: list[int], first: list[int], second: list[int], total_mb: int
) -> None:
    if abs(median(first) - median(second)) > 64:
        raise AssertionError("unloaded GPU baseline drifted between generations")
    slack_mb = max(512, int(total_mb * 0.02))
    if any(median(samples) > median(context) + slack_mb for samples in (first, second)):
        raise AssertionError("unloaded GPU memory did not return to context baseline")


def _approved_weights(
    *,
    checkpoint_dir: Path,
    filenames: list[str],
    approval_ref: str,
    model_assets_dir: Path | None = None,
) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(_required_env("VALIDATION_WEIGHT_SHA256_JSON"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("VALIDATION_WEIGHT_SHA256_JSON must be valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != set(filenames):
        raise RuntimeError("weight manifest must contain exactly the validated weights")
    records = []
    for filename in filenames:
        expected = manifest[filename]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
        ):
            raise RuntimeError(f"invalid approved SHA-256 for {filename}")
        prepared = (
            resolve_prepared_asset(model_assets_dir, filename)
            if model_assets_dir is not None
            else None
        )
        record = artifact_evidence(
            prepared if prepared is not None else checkpoint_dir / filename,
            kind="weight",
            approval_ref=approval_ref,
        )
        if record["sha256"] != expected:
            raise RuntimeError(f"{filename} does not match the approved SHA-256")
        records.append(record)
    return records


async def _run() -> dict[str, Any]:
    git_commit = _required_env("VALIDATION_GIT_COMMIT")
    image_id = _required_env("VALIDATION_IMAGE_ID")
    model_approval_ref = _required_env("VALIDATION_MODEL_APPROVAL_REF")
    fixture_approval_ref = _required_env("VALIDATION_FIXTURE_APPROVAL_REF")
    series = os.getenv("VALIDATION_YOLO_SERIES", "yolo11")
    size = os.getenv("VALIDATION_YOLO_SIZE", "s")

    private_key = Ed25519PrivateKey.generate()
    os.environ["GPU_LIFECYCLE_VERIFY_KEYS_JSON"] = json.dumps(
        {"validation": encode_ed25519_public_key(private_key.public_key())}
    )
    os.environ["YOLO_MANAGED_LIFECYCLE_VERIFIED"] = "1"
    os.environ["YOLO_IDLE_UNLOAD_SECONDS"] = "0"
    os.environ["YOLO_BUILD_TIMEOUT"] = "300"
    os.environ["YOLO_STRICT_OFFLINE"] = "1"

    app_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(app_root))
    import main  # noqa: PLC0415
    from model_registry import resolve_weight_filename  # noqa: PLC0415
    from managed_models import ManagedModelStore  # noqa: PLC0415
    from scripts.controlled_segmentation_validation import (  # noqa: PLC0415
        load_controlled_fixture,
    )

    validation_root = Path(tempfile.mkdtemp(prefix="yolo_lifecycle_validation_"))
    original_managed_store = main._managed_models  # noqa: SLF001

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
            boot_id=main._gpu_lifecycle.boot_id,  # type: ignore[union-attr] # noqa: SLF001
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

    try:
        controlled = load_controlled_fixture(validation_root)
        if controlled is not None:
            # Qualification must never install into the deployment's real cache.
            main._managed_models = ManagedModelStore(validation_root / "managed")  # noqa: SLF001
        image_path, video_path = _make_inputs(validation_root)
        gpu = _gpu_metadata(os.getenv("VALIDATION_GPU_UUID") or None)
        target_uuid = gpu["uuid"]
        memory: dict[str, Any] = {"gpu_total_mb": gpu["total_memory_mb"]}
        contract_checks = {name: False for name in REQUIRED_CONTRACT_CHECKS}
        if controlled is not None:
            contract_checks.update(dict.fromkeys(controlled.checks, False))
        async with main.lifespan(main.app):
            assert main._gpu_lifecycle is not None  # noqa: SLF001
            assert main._model_pool is not None  # noqa: SLF001
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                headers=backend_auth_headers("yolo"),
                base_url="http://yolo-validation",
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
                    raise RuntimeError(
                        "validation GPU is not isolated before CUDA init"
                    )
                memory["cold_context_baseline_mb"] = _initialize_context_baseline(
                    main.torch, target_uuid
                )
                if len(_gpu_compute_processes(target_uuid)) != 1:
                    raise RuntimeError("validator is not the sole GPU compute process")
                # The first real convolution initializes process-level CUDA/cuDNN
                # runtime state that survives an otherwise complete model unload.
                # Prime that state once, clean the pool, then use the mature context
                # as the baseline for both measured lifecycle generations.
                async with main._model_pool.borrow(  # noqa: SLF001
                    "detection",
                    series,
                    size,
                ) as priming_lease:
                    priming_results = await asyncio.get_running_loop().run_in_executor(
                        None,
                        partial(
                            priming_lease.model.predict,
                            str(image_path),
                            device=str(priming_lease.model.device),
                            verbose=False,
                        ),
                    )
                del priming_results
                del priming_lease
                await main._model_pool.unload_all(  # noqa: SLF001
                    reason="manual",
                    force_cleanup=True,
                )
                primed_snapshot = await main._model_pool.snapshot()  # noqa: SLF001
                if primed_snapshot["current_size"] != 0:
                    raise AssertionError("YOLO context priming did not empty the pool")
                primed_torch_memory = _torch_memory_mb(main.torch)
                if (
                    primed_torch_memory["allocated_bytes"]
                    or primed_torch_memory["reserved_bytes"]
                ):
                    raise AssertionError(
                        "YOLO context priming retained PyTorch memory: "
                        f"{primed_torch_memory}"
                    )
                memory["context_baseline_mb"] = _gpu_memory_samples(target_uuid)
                await checked_post(
                    client,
                    "/lifecycle/mode",
                    {"gate": "enforce", "control_epoch": "2"},
                    {
                        GPU_ADMISSION_TOKEN_HEADER: token(
                            AdmissionScope.MODE,
                            control_epoch="2",
                            operation="mode-2",
                        )
                    },
                )
                setup = (await client.get("/setup")).json()
                if "managed_lifecycle" not in setup:
                    raise AssertionError(
                        "managed lifecycle capability was not advertised"
                    )
                contract_checks["managed_lifecycle_advertised"] = True
                if controlled is not None:
                    await controlled.install(client)
                    contract_checks[f"{controlled.check_prefix}_install"] = True
                    contract_checks[
                        f"{controlled.check_prefix}_receipt_mismatch_rejected"
                    ] = True
                    await controlled.reject_mismatched_references(
                        client,
                        lambda: headers(AdmissionScope.PREDICT, "1"),
                    )
                    contract_checks[
                        f"{controlled.check_prefix}_reference_mismatch_rejected"
                    ] = True

                closed_tasks = ("detection", "segmentation", "keypoint", "obb")
                for task in closed_tasks:
                    await checked_post(
                        client,
                        "/predict",
                        {
                            "tasks": [{"id": task, "file_path": str(image_path)}],
                            "context": {
                                "type": task,
                                "variants": {"series": series, "size": size},
                            },
                        },
                        headers(AdmissionScope.PREDICT, "1"),
                    )
                await checked_post(
                    client,
                    "/predict",
                    {
                        "tasks": [{"id": "tracker", "file_path": str(video_path)}],
                        "context": {
                            "type": "tracker",
                            "variants": {"series": series, "size": size},
                            "params": {"tracker": "bytetrack"},
                        },
                    },
                    headers(AdmissionScope.PREDICT, "1"),
                )
                if controlled is not None:
                    response = await checked_post(
                        client,
                        "/predict",
                        controlled.predict_body(),
                        headers(AdmissionScope.PREDICT, "1"),
                    )
                    controlled.inspect_prediction(
                        response,
                        cycle=1,
                        snapshot=await main._model_pool.snapshot(),  # noqa: SLF001
                    )
                contract_checks["real_inference"] = True
                health = (await client.get("/health")).json()
                effective = health["compute"]["effective_device"]
                if not effective or not effective.startswith("cuda"):
                    raise AssertionError(
                        f"YOLO did not remain on CUDA: {health['compute']}"
                    )
                if health["residency"]["evictable"] is not True:
                    raise AssertionError("resident YOLO pool was not evictable")
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
                    await main._model_pool.snapshot(),  # noqa: SLF001
                )
                if controlled is not None:
                    controlled.unloaded(
                        cycle=1,
                        snapshot=await main._model_pool.snapshot(),  # noqa: SLF001
                    )

                warmup_body = {
                    "task": "detection",
                    "variants": {"series": series, "size": size},
                }
                for task in closed_tasks:
                    await checked_post(
                        client,
                        "/warmup",
                        {
                            "task": task,
                            "variants": {"series": series, "size": size},
                        },
                        headers(AdmissionScope.WARMUP, "3"),
                    )
                if controlled is not None:
                    response = await checked_post(
                        client,
                        "/predict",
                        controlled.predict_body(),
                        headers(AdmissionScope.PREDICT, "3"),
                    )
                    controlled.inspect_prediction(
                        response,
                        cycle=2,
                        snapshot=await main._model_pool.snapshot(),  # noqa: SLF001
                    )
                    contract_checks[controlled.output_check] = True
                    contract_checks[f"{controlled.check_prefix}_cold_reload"] = True
                memory["cycle_2_loaded_mb"] = _gpu_memory_samples(target_uuid, count=3)
                workload_scope = (
                    AdmissionScope.PREDICT if controlled else AdmissionScope.WARMUP
                )
                workload_path = "/predict" if controlled else "/warmup"
                workload_body = controlled.predict_body() if controlled else warmup_body
                contract_checks.update(
                    await exercise_lifecycle_fault_matrix(
                        client=client,
                        lifecycle=main._gpu_lifecycle,  # noqa: SLF001
                        token=token,
                        workload_scope=workload_scope,
                        drain_scope=AdmissionScope.DRAIN,
                        unload_scope=AdmissionScope.UNLOAD,
                        workload_path=workload_path,
                        workload_body=workload_body,
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
                final_snapshot = await main._model_pool.snapshot()  # noqa: SLF001
                _assert_unloaded(final_health["residency"], final_snapshot)
                if controlled is not None:
                    controlled.unloaded(cycle=2, snapshot=final_snapshot)
                if second_unload["residency"]["gpu_loaded"] is not False:
                    raise AssertionError("managed unload did not clear GPU residency")
                torch_memory = _torch_memory_mb(main.torch)
                if torch_memory["allocated_bytes"] or torch_memory["reserved_bytes"]:
                    raise AssertionError(
                        f"YOLO unload retained PyTorch memory: {torch_memory}"
                    )
                contract_checks["full_cleanup"] = True

            _assert_baseline_recovery(
                memory["context_baseline_mb"],
                memory["cycle_1_unloaded_mb"],
                memory["cycle_2_unloaded_mb"],
                memory["gpu_total_mb"],
            )
            filenames = [
                resolve_weight_filename(task, series, size)
                for task in ("detection", "segmentation", "keypoint", "obb")
            ]
            checkpoint_dir = Path(main.CHECKPOINTS_DIR)
            artifacts = _approved_weights(
                checkpoint_dir=checkpoint_dir,
                model_assets_dir=Path(main.MODEL_ASSETS_DIR),
                filenames=filenames,
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
            pool_topology = {
                "models": {
                    "cap": int(final_snapshot["cap"]),
                    "series": series,
                    "size": size,
                }
            }
            if controlled is not None:
                controlled_artifacts, controlled_topology = controlled.evidence(
                    model_approval=model_approval_ref,
                    fixture_approval=fixture_approval_ref,
                )
                artifacts.extend(controlled_artifacts)
                pool_topology[controlled.check_prefix] = controlled_topology
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
            return build_managed_lifecycle_evidence(
                backend_name="yolo-backend",
                deployment={
                    "git_commit": git_commit,
                    "image_id": image_id,
                    "runtime_versions": {
                        "python": platform.python_version(),
                        "torch": str(main.torch.__version__),
                        "cuda": str(main.torch.version.cuda),
                        "ultralytics": str(__import__("ultralytics").__version__),
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
        main._managed_models = original_managed_store  # noqa: SLF001
        shutil.rmtree(validation_root)


def _failed_evidence(exc: Exception) -> dict[str, Any]:
    return build_managed_lifecycle_evidence(
        backend_name="yolo-backend",
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

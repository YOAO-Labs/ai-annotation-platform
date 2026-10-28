"""Real, local-exclusive native model checks; not managed admission evidence."""
from __future__ import annotations

import gc
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .provider import Provider


def require_exclusive_gpu(gpu_uuid):
    """Fail closed if process attribution cannot establish an idle selected GPU."""
    visible = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader,nounits"], text=True
    )
    if gpu_uuid.lower() not in {line.strip().lower() for line in visible.splitlines()}:
        raise ValueError("Selected physical GPU is not visible to nvidia-smi")
    raw = subprocess.check_output(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"],
        text=True,
    )
    for row in raw.splitlines():
        fields = [field.strip() for field in row.split(",")]
        if len(fields) != 2 or not fields[0].startswith("GPU-") or not fields[1].isdigit():
            raise ValueError("GPU compute process attribution is unavailable")
        if fields[0].lower() == gpu_uuid.lower() and int(fields[1]) != os.getpid():
            raise ValueError("Selected GPU has another active compute process")


def run(args, provider_factory=Provider):
    import torch
    from PIL import Image
    from .assets import assets, verify
    from .device import verify_device

    device, gpu_uuid = args.device, args.gpu_uuid
    asset_dir, multi_image, empty_image, output = (
        args.asset_dir, args.multi_image, args.empty_image, args.output
    )

    report = {"mode": "local-isolated", "device": device, "gpu_uuid": gpu_uuid, "checks": {}}
    checks = report["checks"]

    def check(name, fn):
        try:
            detail = fn()
            checks[name] = {"passed": True, "detail": detail}
            return True
        except Exception as exc:
            checks[name] = {"passed": False, "detail": f"{type(exc).__name__}: {exc}"}
            return False

    item = assets()["t"]
    path = Path(asset_dir) / item["filename"]

    def missing_weights():
        with tempfile.TemporaryDirectory(prefix="libreyolo-missing-") as temporary:
            try:
                verify(Path(temporary) / item["filename"], item)
            except ValueError:
                return "Missing pinned asset rejected without download or fallback"
        raise AssertionError("Missing asset was accepted")

    def wrong_device():
        bad = "cuda:" + str(torch.cuda.device_count())
        try:
            verify_device(bad, gpu_uuid)
        except (ValueError, RuntimeError):
            return f"Unavailable device {bad} rejected"
        raise AssertionError("Unavailable CUDA device was accepted")

    check("missing_weights", missing_weights)
    check("wrong_device", wrong_device)
    cycles = []
    native_counts = {"native_multi": [], "native_empty": []}

    def execute_cycles():
        if not device.startswith("cuda:"):
            raise ValueError("Local GPU probe requires an explicit CUDA ordinal")
        verify_device(device, gpu_uuid)
        require_exclusive_gpu(gpu_uuid)
        verify(path, item)
        selected = torch.device(device)
        torch.cuda.set_device(selected)
        torch.cuda.synchronize(selected)
        torch.cuda.empty_cache()
        baseline_allocated = torch.cuda.memory_allocated(selected)
        baseline_free, baseline_total = torch.cuda.mem_get_info(selected)
        require_exclusive_gpu(gpu_uuid)
        for cycle in range(2):
            model = provider_factory().load("t", device, {"path": str(path)})
            try:
                torch.cuda.synchronize(selected)
                resident = torch.cuda.memory_allocated(selected)
                if resident <= baseline_allocated:
                    raise AssertionError("Model did not allocate CUDA memory")
                for key, image_path in (("native_multi", multi_image), ("native_empty", empty_image)):
                    with Image.open(image_path) as image:
                        image = image.convert("RGB")
                        detections = model.predict(image, {"conf": 0.25, "iou": 0.45, "max_det": 300})
                    count = len(detections)
                    native_counts[key].append(count)
                    if key == "native_multi" and count < 2:
                        raise AssertionError(f"Expected multiple detections, got {count}")
                    if key == "native_empty" and count != 0:
                        raise AssertionError(f"Expected no detections, got {count}")
            finally:
                model.close()
                del model
                gc.collect()
                torch.cuda.synchronize(selected)
                torch.cuda.empty_cache()
            after = torch.cuda.memory_allocated(selected)
            require_exclusive_gpu(gpu_uuid)
            free, total = torch.cuda.mem_get_info(selected)
            cycles.append({"cycle": cycle + 1, "baseline_allocated_bytes": baseline_allocated,
                           "resident_allocated_bytes": resident, "released_allocated_bytes": after,
                           "baseline_free_bytes": baseline_free, "free_after_bytes": free,
                           "total_bytes": total, "baseline_total_bytes": baseline_total})
            if after > baseline_allocated:
                raise AssertionError("CUDA allocations remain after model release")
        return cycles

    ok = check("gpu_residency", execute_cycles)
    for key in ("native_multi", "native_empty"):
        counts = native_counts[key]
        checks[key] = {"passed": ok and len(counts) == 2,
                       "detail": {"counts": counts, "geometry": "native original-image validation"}}
    checks["gpu_release"] = {"passed": ok and len(cycles) == 2,
                             "detail": cycles if cycles else "No complete GPU release cycles"}
    report["platform_managed_qualified"] = False
    report["cancellation_drain"] = "not assessed; requires HTTP lifecycle evidence"
    Path(output).write_text(json.dumps(report, indent=2) + "\n")
    return all(result["passed"] for result in checks.values())

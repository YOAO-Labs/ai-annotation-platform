"""Loopback-only process used by local-exclusive qualification."""

from __future__ import annotations

import uvicorn
from aap_inference_plugin.app import create_detection_app
from aap_inference_plugin.types import DetectionConfig
from aap_protocol_v2.lifecycle import load_verify_keyring

from .assets import assets, verify
from .device import verify_device
from .provider import Provider


def run(args, provider_factory=Provider):
    actual_uuid = verify_device(args.device, args.gpu_uuid)
    item = assets()["t"]
    path = verify(args.asset_dir / item["filename"], item)
    config = DetectionConfig(
        token=args.token_file.read_text().strip(),
        device=args.device,
        series="yolov9",
        physical_gpu_uuid=actual_uuid,
        catalog={"t": {"path": str(path), "asset": item}},
        default_model="t",
        verify_keyring=load_verify_keyring(args.keyring_file.read_text()),
        expected_registry_id=args.registry,
        expected_gpu_resource_id=args.resource,
        # The preceding local GPU release probe is not platform qualification.
        evictable_verified=True,
        allow_legacy_workloads=True,
    )
    app = create_detection_app(provider_factory(), config)

    @app.get("/_local/gpu-memory")
    def gpu_memory():
        import torch

        selected = torch.device(args.device)
        torch.cuda.synchronize(selected)
        free, total = torch.cuda.mem_get_info(selected)
        return {
            "allocated_bytes": torch.cuda.memory_allocated(selected),
            "reserved_bytes": torch.cuda.memory_reserved(selected),
            "free_bytes": free,
            "total_bytes": total,
        }

    @app.get("/_local/gpu-exclusivity")
    def gpu_exclusivity():
        from .probe import require_exclusive_gpu

        require_exclusive_gpu(actual_uuid)
        return {"exclusive_sample": True}

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")

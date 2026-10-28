"""Loopback-only process used by the local-exclusive qualification command."""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from aap_protocol_v2.lifecycle import load_verify_keyring
from aap_libreyolo_plugin.assets import assets, verify
from aap_libreyolo_plugin.device import verify_device
from aap_libreyolo_plugin.provider import Provider

from .app import create_detection_app
from .types import DetectionConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--gpu-uuid", required=True)
    parser.add_argument("--asset-dir", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--keyring-file", required=True, type=Path)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    actual_uuid = verify_device(args.device, args.gpu_uuid)
    item = assets()["t"]
    path = verify(args.asset_dir / item["filename"], item)
    config = DetectionConfig(
        token=args.token_file.read_text().strip(), device=args.device, series="yolov9",
        physical_gpu_uuid=actual_uuid, catalog={"t": {"path": str(path), "asset": item}}, default_model="t",
        verify_keyring=load_verify_keyring(args.keyring_file.read_text()),
        expected_registry_id=args.registry, expected_gpu_resource_id=args.resource,
        # This process is created only after the separate local GPU release probe succeeds;
        # its ephemeral keys and result are never a platform qualification.
        evictable_verified=True,
    )
    app = create_detection_app(Provider(), config)

    @app.get("/_local/gpu-memory")
    def gpu_memory():
        import torch
        selected = torch.device(args.device)
        torch.cuda.synchronize(selected)
        free, total = torch.cuda.mem_get_info(selected)
        return {"allocated_bytes": torch.cuda.memory_allocated(selected),
                "reserved_bytes": torch.cuda.memory_reserved(selected),
                "free_bytes": free, "total_bytes": total}

    @app.get("/_local/gpu-exclusivity")
    def gpu_exclusivity():
        from aap_libreyolo_plugin.probe import require_exclusive_gpu
        require_exclusive_gpu(actual_uuid)
        return {"exclusive_sample": True}

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

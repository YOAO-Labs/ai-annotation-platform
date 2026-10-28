"""Explicit asset preparation and service composition."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .assets import assets, prepare, verify
from .device import verify_device


def csv(name):
    return tuple(part.strip() for part in os.environ.get(name, "").split(",") if part.strip())


def serve(args):
    import uvicorn
    from aap_backend_runtime.managed_models import ManagedModelStore
    from aap_inference_plugin import DetectionConfig, create_detection_app
    from aap_protocol_v2.lifecycle import load_verify_keyring
    from training_contract.inference_runtime import inference_runtime
    from .install import inspect, install_request_model
    from training_contract.model_return import LIBRE_FORMAT
    from .provider import Provider, validate_receipt

    data = Path(os.environ.get("AAP_PLUGIN_DATA_DIR", "/var/lib/aap-libreyolo"))
    token = Path(args.token_file).read_text().strip()
    if len(token) < 32:
        raise ValueError("Plugin token must contain at least 32 characters")
    gpu_uuid = os.environ.get("AAP_PLUGIN_GPU_UUID")
    verify_device(args.device, gpu_uuid)
    registry = os.environ.get("AAP_PLUGIN_REGISTRY_ID")
    resource = os.environ.get("AAP_PLUGIN_GPU_RESOURCE_ID")
    keyring_file = os.environ.get("AAP_PLUGIN_GPU_KEYRING_FILE")
    if args.device != "cpu" and not (registry and resource and keyring_file):
        raise ValueError("CUDA requires registry, resource and verification keyring bindings")
    keyring = load_verify_keyring(Path(keyring_file).read_text()) if keyring_file else {}
    available = {}
    for size, item in assets().items():
        path = Path(args.asset_dir) / item["filename"]
        if path.exists():
            available[size] = {"path": str(verify(path, item)), "asset": item}
    if "t" not in available:
        raise ValueError("Prepare the pinned YOLOv9t asset before starting the service")
    store = ManagedModelStore(data / "managed", inspect=inspect, validate_receipt=validate_receipt)
    kind = "cpu" if args.device == "cpu" else "cuda"

    def runtime_receipt(receipt):
        return {**receipt, "inference_runtime": inference_runtime(receipt, "1.6.0", device=kind)}

    config = DetectionConfig(
        token=token, device=args.device, catalog=available, default_model="t", series="yolov9",
        versions={"libreyolo": "1.6.0"}, managed_store=store,
        runtime_receipt=runtime_receipt,
        install_request_model=install_request_model(csv("AAP_PLUGIN_ASSET_ORIGINS")),
        managed_model_compatibility=({"framework": "libreyolo", "format": LIBRE_FORMAT, "runtime_device": kind},),
        verify_keyring=keyring, expected_registry_id=registry, expected_gpu_resource_id=resource,
        allowed_image_hosts=csv("AAP_PLUGIN_IMAGE_HOSTS"),
        evictable_verified=os.environ.get("AAP_PLUGIN_EVICTABLE_VERIFIED") == "1",
    )
    uvicorn.run(create_detection_app(Provider(), config), host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(prog="aap-libreyolo")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-assets", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--asset-dir", default=os.environ.get("AAP_PLUGIN_ASSET_DIR", "/var/lib/aap-libreyolo/assets"))
        if name == "prepare-assets":
            command.add_argument("--size", choices=("t", "s", "m", "c"), default="t")
        else:
            command.add_argument("--device", default=os.environ.get("AAP_PLUGIN_DEVICE", "cpu"))
            command.add_argument("--token-file", default=os.environ.get("AAP_PLUGIN_TOKEN_FILE"), required=not os.environ.get("AAP_PLUGIN_TOKEN_FILE"))
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "prepare-assets":
        path = prepare(args.size, args.asset_dir)
        print(f"Prepared {args.size}: {path} sha256={assets()[args.size]['sha256']}")
    else:
        serve(args)


if __name__ == "__main__":
    main()

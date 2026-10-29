"""Explicit asset preparation and service composition."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .assets import assets, prepare, verify
from .device import verify_device
from .provider import Provider


def csv(name):
    return tuple(
        part.strip() for part in os.environ.get(name, "").split(",") if part.strip()
    )


def serve(args, provider_factory=Provider):
    from importlib.metadata import version
    import torch
    import uvicorn
    from aap_backend_runtime.managed_models import ManagedModelStore
    from aap_inference_plugin import DetectionConfig, create_detection_app
    from aap_protocol_v2.lifecycle import load_verify_keyring
    from training_contract.inference_runtime import inference_runtime
    from .install import inspect, install_request_model, validate_installed_receipt
    from training_contract.model_return import LIBRE_FORMAT, LIBRE_RUNTIME
    from .qualification import current_gpu, verify_qualification

    data = Path(os.environ.get("AAP_PLUGIN_DATA_DIR", "/var/lib/aap-libreyolo"))
    token = Path(args.token_file).read_text().strip()
    if len(token) < 32:
        raise ValueError("Plugin token must contain at least 32 characters")
    registry = os.environ.get("AAP_PLUGIN_REGISTRY_ID")
    resource = os.environ.get("AAP_PLUGIN_GPU_RESOURCE_ID")
    keyring_file = os.environ.get("AAP_PLUGIN_GPU_KEYRING_FILE")
    local = args.local
    if local and (
        args.host != "127.0.0.1"
        or registry
        or resource
        or keyring_file
        or os.environ.get("AAP_PLUGIN_QUALIFICATION_FILE")
        or os.environ.get("AAP_PLUGIN_QUALIFICATION_SHA256")
    ):
        raise ValueError(
            "Local development is loopback-only and cannot use platform bindings or qualification"
        )
    gpu_uuid = os.environ.get("AAP_PLUGIN_GPU_UUID")
    physical_gpu_uuid = verify_device(args.device, gpu_uuid, local_exclusive=local)
    if local and physical_gpu_uuid:
        from .probe import require_exclusive_gpu

        require_exclusive_gpu(physical_gpu_uuid)
    actual_versions = {
        name: version(name) for name in ("libreyolo", "torch", "torchvision")
    }
    if any(actual_versions[name] != LIBRE_RUNTIME[name] for name in actual_versions):
        raise ValueError("Installed model runtime differs from qualification baseline")
    if args.device != "cpu":
        if not local and torch.cuda.device_count() != 1:
            raise ValueError(
                "A managed plugin instance must expose exactly one CUDA device"
            )
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
    gpu_facts = current_gpu(physical_gpu_uuid) if physical_gpu_uuid else None
    provider = provider_factory()

    def runtime_info():
        return {
            **actual_versions,
            "cuda_build": torch.version.cuda,
            "device": "cpu" if args.device == "cpu" else "cuda",
            "execution_scope": "local" if local else "managed",
            "precision": None,
            "half": None,
            "amp": None,
            "driver_version": gpu_facts[0] if gpu_facts else None,
            "total_memory_mb": gpu_facts[1] if gpu_facts else None,
            "tf32": bool(
                torch.backends.cuda.matmul.allow_tf32 or torch.backends.cudnn.allow_tf32
            )
            if gpu_facts
            else None,
            **provider.execution_facts(),
        }

    if (
        args.device != "cpu"
        and not local
        and not (registry and resource and keyring_file)
    ):
        raise ValueError(
            "CUDA requires registry, resource and verification keyring bindings"
        )
    keyring = (
        load_verify_keyring(Path(keyring_file).read_text()) if keyring_file else {}
    )
    available = {}
    for size, item in assets().items():
        path = Path(args.asset_dir) / item["filename"]
        if path.exists():
            available[size] = {"path": str(verify(path, item)), "asset": item}
    if "t" not in available:
        raise ValueError("Prepare the pinned YOLOv9t asset before starting the service")
    origins = csv("AAP_PLUGIN_ASSET_ORIGINS")
    managed = not local or bool(origins)
    store = (
        ManagedModelStore(
            data / "managed",
            inspect=inspect,
            validate_receipt=validate_installed_receipt,
        )
        if managed
        else None
    )
    kind = "cpu" if args.device == "cpu" else "cuda"

    def runtime_receipt(receipt):
        return {
            **receipt,
            "inference_runtime": inference_runtime(receipt, "1.6.0", device=kind),
        }

    qualification_path = os.environ.get("AAP_PLUGIN_QUALIFICATION_FILE")
    qualification_sha = os.environ.get("AAP_PLUGIN_QUALIFICATION_SHA256")
    if bool(qualification_path) != bool(qualification_sha):
        raise ValueError(
            "Qualification report and approved digest must be supplied together"
        )
    qualified = False
    if qualification_path:
        if kind != "cuda":
            raise ValueError("GPU lifecycle evidence cannot qualify a CPU deployment")
        profile = inference_runtime(
            {
                "framework": "libreyolo",
                "framework_version": "1.6.0",
                "task": "detection",
            },
            "1.6.0",
            device="cuda",
        )["qualification"]
        qualified = verify_qualification(
            qualification_path,
            report_sha256=qualification_sha,
            artifact=os.environ.get("AAP_PLUGIN_OCI_ARTIFACT"),
            image_id=os.environ.get("AAP_PLUGIN_IMAGE_ID"),
            registry_id=registry,
            gpu_resource_id=resource,
            gpu_uuid=physical_gpu_uuid,
            runtime_qualification=profile,
            expected_weight=assets()["t"],
            gpu_facts=gpu_facts,
        )

    config = DetectionConfig(
        token=token,
        device=args.device,
        catalog=available,
        default_model="t",
        series="yolov9",
        versions={"libreyolo": "1.6.0"},
        managed_store=store,
        runtime_receipt=runtime_receipt if managed else None,
        install_request_model=install_request_model(origins) if managed else None,
        managed_model_compatibility=(
            (
                {
                    "framework": "libreyolo",
                    "format": LIBRE_FORMAT,
                    "runtime_device": kind,
                },
            )
            if managed
            else ()
        ),
        verify_keyring=keyring,
        expected_registry_id=registry,
        expected_gpu_resource_id=resource,
        physical_gpu_uuid=physical_gpu_uuid,
        plugin_identity={"plugin_id": "libreyolo", "version": "0.1.0"},
        runtime_info=runtime_info,
        allowed_image_hosts=csv("AAP_PLUGIN_IMAGE_HOSTS"),
        evictable_verified=qualified,
        allow_legacy_workloads=args.local,
    )
    uvicorn.run(create_detection_app(provider, config), host=args.host, port=args.port)


def main(provider_factory=Provider):
    parser = argparse.ArgumentParser(prog="aap-libreyolo")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-assets", "serve"):
        command = sub.add_parser(name)
        command.add_argument(
            "--asset-dir",
            default=os.environ.get(
                "AAP_PLUGIN_ASSET_DIR", "/var/lib/aap-libreyolo/assets"
            ),
        )
        if name == "prepare-assets":
            command.add_argument("--size", choices=("t", "s", "m", "c"), default="t")
        else:
            command.add_argument(
                "--device", default=os.environ.get("AAP_PLUGIN_DEVICE", "cpu")
            )
            command.add_argument(
                "--token-file",
                default=os.environ.get("AAP_PLUGIN_TOKEN_FILE"),
                required=not os.environ.get("AAP_PLUGIN_TOKEN_FILE"),
            )
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8000)
            command.add_argument(
                "--local",
                action="store_true",
                help="Loopback-only development; CUDA requires an exclusive card and is not platform-qualified",
            )
    probe = sub.add_parser("probe")
    probe.add_argument("--device", required=True)
    probe.add_argument("--gpu-uuid", required=True)
    probe.add_argument("--asset-dir", required=True)
    probe.add_argument("--multi-image", required=True)
    probe.add_argument("--empty-image", required=True)
    probe.add_argument("--output", required=True)
    qualification = sub.add_parser("qualification-server")
    qualification.add_argument("--device", required=True)
    qualification.add_argument("--gpu-uuid", required=True)
    qualification.add_argument("--asset-dir", type=Path, required=True)
    qualification.add_argument("--token-file", type=Path, required=True)
    qualification.add_argument("--keyring-file", type=Path, required=True)
    qualification.add_argument("--registry", required=True)
    qualification.add_argument("--resource", required=True)
    qualification.add_argument("--port", type=int, required=True)
    from .manifest import add_arguments as manifest_arguments

    manifest_arguments(sub.add_parser("manifest"))
    args = parser.parse_args()
    if args.command == "prepare-assets":
        path = prepare(args.size, args.asset_dir)
        print(f"Prepared {args.size}: {path} sha256={assets()[args.size]['sha256']}")
    elif args.command == "probe":
        from .probe import run

        if not run(args, provider_factory=provider_factory):
            raise SystemExit(1)
    elif args.command == "qualification-server":
        from .qualification_server import run

        run(args, provider_factory=provider_factory)
    elif args.command == "manifest":
        from .manifest import run

        run(args)
    else:
        serve(args, provider_factory=provider_factory)


if __name__ == "__main__":
    main()

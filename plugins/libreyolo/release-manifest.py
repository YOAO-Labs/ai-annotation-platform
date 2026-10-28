"""Emit installation intent only for a locally inspected, pushed image digest."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def inspected_entrypoint(config: dict) -> list[str]:
    entrypoint = config.get("Entrypoint")
    if (not isinstance(entrypoint, list) or len(entrypoint) != 6
            or not isinstance(entrypoint[0], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", entrypoint[0])
            or entrypoint[1:] != ["serve", "--host", "0.0.0.0", "--port", "8000"]
            or config.get("Cmd") not in (None, [])):
        raise ValueError("Image must have an approved exec-form entrypoint and empty command")
    return entrypoint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="pushed image reference to inspect")
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--gpu-memory-mb", type=int, default=0)
    parser.add_argument("--asset-origin", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from aap_inference_contract.plugin import PluginManifest
    from training_contract.model_return import LIBRE_FORMAT

    inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image], text=True))
    digests = inspection[0].get("RepoDigests") or []
    if len(digests) != 1 or "@sha256:" not in digests[0]:
        raise SystemExit("Image must have exactly one inspected pushed repository digest")
    try:
        entrypoint = inspected_entrypoint(inspection[0].get("Config") or {})
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    manifest_path = Path(__file__).resolve().parents[2] / "apps/yolo-backend/scripts/controlled-model-assets.json"
    manifest = PluginManifest.model_validate({
        "plugin_id": "libreyolo", "version": "0.1.0", "publisher": "AI Annotation Platform",
        "license": "MIT", "artifact": digests[0], "sdk_compatibility": "0.1.0",
        "entrypoint": entrypoint,
        "capabilities": ["inference", "detection", "managed_models"],
        "resource": {"device": args.device, "memory_mb": args.memory_mb, "gpu_memory_mb": args.gpu_memory_mb},
        "asset_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "allowed_asset_sources": args.asset_origin,
        "managed_model_compatibility": [{"framework": "libreyolo", "format": LIBRE_FORMAT, "runtime_device": args.device}],
    })
    args.output.write_text(manifest.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()

"""Emit installation intent only for a locally inspected, pushed image digest."""

import argparse
import hashlib
import json
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
import re
import subprocess


def inspected_entrypoint(config: dict) -> list[str]:
    entrypoint = config.get("Entrypoint")
    if (
        not isinstance(entrypoint, list)
        or len(entrypoint) != 6
        or not isinstance(entrypoint[0], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", entrypoint[0])
        or entrypoint[1:] != ["serve", "--host", "0.0.0.0", "--port", "8000"]
        or config.get("Cmd") not in (None, [])
    ):
        raise ValueError(
            "Image must have an approved exec-form entrypoint and empty command"
        )
    return entrypoint


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("image", help="pushed image reference to inspect")
    parser.add_argument("--publisher", required=True)
    parser.add_argument(
        "--license",
        required=True,
        help="Publisher's license declaration for this artifact",
    )
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--gpu-memory-mb", type=int, default=0)
    parser.add_argument("--asset-origin", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)


def run(args) -> None:
    from aap_inference_contract.plugin import PluginManifest
    from training_contract.model_return import LIBRE_FORMAT

    inspection = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "--", args.image], text=True
        )
    )
    digests = inspection[0].get("RepoDigests") or []
    if len(digests) != 1 or "@sha256:" not in digests[0]:
        raise SystemExit(
            "Image must have exactly one inspected pushed repository digest"
        )
    try:
        entrypoint = inspected_entrypoint(inspection[0].get("Config") or {})
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    manifest_bytes = (
        files("aap_libreyolo_plugin")
        .joinpath("controlled-model-assets.json")
        .read_bytes()
    )
    manifest = PluginManifest.model_validate(
        {
            "plugin_id": "libreyolo",
            "version": version("aap-libreyolo-plugin"),
            "publisher": args.publisher,
            "license": args.license,
            "artifact": digests[0],
            "sdk_compatibility": version("aap-inference-plugin-sdk"),
            "entrypoint": entrypoint,
            "capabilities": ["inference", "detection", "managed_models"],
            "resource": {
                "device": args.device,
                "memory_mb": args.memory_mb,
                "gpu_memory_mb": args.gpu_memory_mb,
            },
            "asset_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "allowed_asset_sources": args.asset_origin,
            "managed_model_compatibility": [
                {
                    "framework": "libreyolo",
                    "format": LIBRE_FORMAT,
                    "runtime_device": args.device,
                }
            ],
        }
    )
    args.output.write_text(manifest.model_dump_json(indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()

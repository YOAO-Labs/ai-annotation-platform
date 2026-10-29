"""Pinned asset selection and verification for the RapidOCR plugin image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import catalog
from download_models import MANIFEST


def _known_assets() -> dict[str, str]:
    return {
        f"{version}/{component}/{url.rsplit('/', 1)[-1]}": digest
        for version, component, url, digest in MANIFEST
    }


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_assets(
    data_dir: Path, expected_manifest_sha256: str
) -> list[dict[str, str | int]]:
    """Verify the approved manifest and return measured selected model identities."""
    raw_manifest = (data_dir / "assets.json").read_bytes()
    if hashlib.sha256(raw_manifest).hexdigest() != expected_manifest_sha256:
        raise RuntimeError(
            "RapidOCR asset manifest digest differs from the installation"
        )
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict) or set(manifest) != {"files"}:
        raise ValueError("Invalid RapidOCR asset manifest")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("RapidOCR asset manifest must select at least one file")
    known = _known_assets()
    seen: set[str] = set()
    weights: list[dict[str, str | int]] = []
    root = (data_dir / "models").resolve()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("Invalid RapidOCR asset entry")
        relative = entry["path"]
        if (
            not isinstance(relative, str)
            or relative in seen
            or known.get(relative) != entry["sha256"]
        ):
            raise ValueError("RapidOCR asset is duplicated or not pinned")
        seen.add(relative)
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or _digest(path) != entry["sha256"]:
            raise ValueError("RapidOCR asset digest differs from the pinned model")
        weights.append({"sha256": entry["sha256"], "size_bytes": path.stat().st_size})
    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.onnx")
        if path.is_file()
    }
    if present != seen:
        raise ValueError(
            "RapidOCR model directory contains unapproved or missing assets"
        )
    return weights


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a RapidOCR plugin manifest")
    parser.add_argument(
        "--artifact", required=True, help="Digest-pinned local OCI image"
    )
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--license", required=True)
    parser.add_argument("--sdk-compatibility", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--gpu-memory-mb", type=int, default=0)
    parser.add_argument(
        "--asset",
        action="append",
        required=True,
        help="Approved model path relative to models/",
    )
    parser.add_argument(
        "--asset-manifest", type=Path, required=True, help="Output assets.json"
    )
    parser.add_argument("--asset-origin", action="append", default=[])
    args = parser.parse_args()
    if os.environ.get("RAPIDOCR_POOL_CAP", "3") != "3":
        parser.error("Plugin manifests require the default RAPIDOCR_POOL_CAP=3")
    if (
        args.memory_mb <= 0
        or args.gpu_memory_mb < 0
        or (args.device == "cuda") != (args.gpu_memory_mb > 0)
    ):
        parser.error(
            "CPU requires zero GPU memory; CUDA requires a positive GPU memory budget"
        )
    known = _known_assets()
    if len(args.asset) != len(set(args.asset)) or any(
        path not in known for path in args.asset
    ):
        parser.error("Asset paths must be unique pinned RapidOCR model paths")
    assets = {
        "files": [{"path": path, "sha256": known[path]} for path in sorted(args.asset)]
    }
    encoded = (
        json.dumps(assets, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    args.asset_manifest.write_bytes(encoded)
    keys = (
        "id",
        "task",
        "is_interactive",
        "supported_prompts",
        "supported_inputs",
        "supported_geometric_outputs",
    )
    manifest = {
        "plugin_id": "rapidocr",
        "version": "0.1.0",
        "publisher": args.publisher,
        "license": args.license,
        "artifact": args.artifact,
        "sdk_compatibility": args.sdk_compatibility,
        "entrypoint": [
            "python3",
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8005",
            "--workers",
            "1",
        ],
        "capabilities": ["inference", "detection"],
        "models": [
            {key: item[key] for key in keys} for item in catalog.model_entries()
        ],
        "pools": {"engines": {"cap": 3}},
        "resource": {
            "device": args.device,
            "memory_mb": args.memory_mb,
            "gpu_memory_mb": args.gpu_memory_mb,
        },
        "asset_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "managed_model_compatibility": [],
        "allowed_asset_sources": args.asset_origin,
    }
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the unified yoao-sdk wheel and sdist from a committed revision.

Exports the first-party yoao-sdk distribution (API client, CLI, and portable
task / lifecycle / inference / protocol-v2 / training contracts). Model catalogs,
weights, and third-party dependency wheels are not included.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("packages/python-sdk",)
BUILDERS = {
    "build": "1.3.0",
    "setuptools": "80.9.0",
    "wheel": "0.45.1",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="committed platform source")
    parser.add_argument(
        "--output", required=True, type=Path, help="new artifact directory"
    )
    args = parser.parse_args()
    for package, expected in BUILDERS.items():
        if importlib.metadata.version(package) != expected:
            parser.error(f"builder requires {package}=={expected}")
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.revision + "^{commit}"],
        cwd=ROOT,
        text=True,
    ).strip()
    epoch = subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", revision], cwd=ROOT, text=True
    ).strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_archive = output / "contract-source.tar"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            "--output",
            str(source_archive),
            revision,
            "LICENSE",
            *PACKAGES,
        ],
        cwd=ROOT,
        check=True,
    )
    environment = {**os.environ, "SOURCE_DATE_EPOCH": epoch, "PYTHONHASHSEED": "0"}
    with tempfile.TemporaryDirectory(prefix="plugin-contracts-") as temporary:
        source = Path(temporary)
        subprocess.run(
            ["tar", "-xf", str(source_archive), "-C", str(source)], check=True
        )
        for package in PACKAGES:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--sdist",
                    "--no-isolation",
                    "--outdir",
                    str(output),
                    str(source / package),
                ],
                env=environment,
                check=True,
            )
        license_bytes = (source / "LICENSE").read_bytes()
    files = []
    for path in sorted(output.iterdir()):
        record = {
            "file": path.name,
            "sha256": sha256(path),
            "size": path.stat().st_size,
        }
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as wheel:
                metadata_paths = [
                    name
                    for name in wheel.namelist()
                    if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
                ]
                if len(metadata_paths) != 1:
                    raise ValueError(f"ambiguous wheel metadata: {path.name}")
                metadata = email.parser.BytesParser().parsebytes(
                    wheel.read(metadata_paths[0])
                )
                if not any(
                    name.endswith("/LICENSE") and wheel.read(name) == license_bytes
                    for name in wheel.namelist()
                ):
                    raise ValueError(f"missing first-party MIT notice: {path.name}")
                record.update(
                    name=metadata["Name"],
                    version=metadata["Version"],
                    requires_python=metadata["Requires-Python"],
                )
        files.append(record)
    manifest = {
        "schema_version": 1,
        "source_repository": "https://github.com/YOAO-Labs/ai-annotation-platform",
        "source_revision": revision,
        "source_date_epoch": int(epoch),
        "source_archive": source_archive.name,
        "builders": BUILDERS,
        "files": files,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(output.iterdir())),
        encoding="utf-8",
    )
    print(f"Contract source: {revision}")
    print(f"Manifest SHA256: {sha256(manifest_path)}")
    print(f"Artifacts: {output}")


if __name__ == "__main__":
    main()

"""Standalone development commands for detection plugins."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

import httpx


def init_project(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "pyproject.toml").write_text('''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "my-libreyolo-plugin"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["aap-libreyolo-plugin"]

[project.scripts]
my-libreyolo-plugin = "my_libreyolo_plugin:main"

[tool.setuptools]
py-modules = ["my_libreyolo_plugin"]
''', encoding="utf-8")
    (directory / "my_libreyolo_plugin.py").write_text('''"""Real LibreYOLO detector; adapt these three model responsibilities."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PIL.Image import Image
from aap_inference_plugin import Detection, DetectionModel
from aap_libreyolo_plugin import cli
from aap_libreyolo_plugin.provider import Provider as LibreProvider


class Model:
    """Own one loaded native model; image boxes remain in original-image pixels."""

    def __init__(self, native: DetectionModel) -> None:
        self.native = native
        self.classes = native.classes

    def predict(self, image: Image, parameters: Mapping[str, Any]) -> Sequence[Detection]:
        return self.native.predict(image, parameters)

    def close(self) -> None:
        self.native.close()


class Provider:
    def __init__(self) -> None:
        self.native = LibreProvider()

    def load(self, model_id: str, device: str, config: Mapping[str, Any]) -> DetectionModel:
        return Model(self.native.load(model_id, device, config))

    def execution_facts(self) -> Mapping[str, Any]:
        return self.native.execution_facts()


def main() -> None:
    cli.main(provider_factory=Provider)
''', encoding="utf-8")


def dev_server(args) -> None:
    """Warm a developer-only service; this is not managed GPU admission."""
    token = args.token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("token file must contain at least 32 characters")
    if args.host != "127.0.0.1":
        raise ValueError("developer preflight is loopback-only; use provider serve for managed deployment")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.entrypoint):
        raise ValueError("--entrypoint must be the name of an installed console script")
    executable = shutil.which(args.entrypoint)
    if executable is None:
        raise ValueError(f"installed console script {args.entrypoint!r} was not found; install the generated project in this environment")
    command = [executable, "serve", "--device", args.device, "--asset-dir", str(args.asset_dir),
               "--token-file", str(args.token_file), "--host", args.host, "--port", str(args.port)]
    process = subprocess.Popen(command)
    try:
        url = f"http://127.0.0.1:{args.port}"
        with httpx.Client(base_url=url, timeout=120, trust_env=False,
                          headers={"Authorization": "Bearer " + token}) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError(f"developer service exited before readiness (exit {process.returncode})")
                try:
                    if client.get("/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise ValueError("developer service did not become ready")
            response = client.post("/warmup", json={"task": "detect", "model_variants": {"series": "yolov9", "size": "t"}})
            response.raise_for_status()
            health = client.get("/health")
            health.raise_for_status()
            info = health.json()
            if info.get("compute", {}).get("effective_device") != args.device or info.get("pool", {}).get("current_size", 0) < 1:
                raise ValueError("preflight did not establish a resident model on the requested device")
            if args.device.startswith("cuda:") and info.get("residency", {}).get("gpu_loaded") is not True:
                raise ValueError("preflight did not establish actual CUDA model residency")
        print("Developer preflight loaded the model on the requested device; local mode is not managed GPU qualification", flush=True)
        code = process.wait()
        if code:
            raise ValueError(f"developer service exited with status {code}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(prog="aap-plugin")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("directory", type=Path)
    init.add_argument("--template", choices=["libreyolo-yolov9"], default="libreyolo-yolov9")
    dev = commands.add_parser("dev")
    dev.add_argument("--entrypoint", default="my-libreyolo-plugin", help="Installed generated project's console script")
    dev.add_argument("--device", required=True)
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)
    dev.add_argument("--asset-dir", type=Path, required=True)
    dev.add_argument("--token-file", type=Path, required=True)
    prepare = commands.add_parser("prepare-assets")
    prepare.add_argument("--size", choices=["t", "s", "m", "c"], default="t")
    prepare.add_argument("--asset-dir", type=Path, required=True)
    verify = commands.add_parser("check")
    mode = verify.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url", help="Check a running HTTP endpoint only")
    mode.add_argument("--qualify-local", action="store_true", help="Run full local-exclusive GPU qualification")
    verify.add_argument("--fixtures", required=True, type=Path)
    verify.add_argument("--device")
    verify.add_argument("--gpu-uuid")
    verify.add_argument("--asset-dir", type=Path)
    verify.add_argument("--exclusive-gpu", action="store_true")
    verify.add_argument("--report", type=Path)
    build = commands.add_parser("build")
    build.add_argument("directory", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        if args.command == "init":
            init_project(args.directory)
        elif args.command == "dev":
            dev_server(args)
        elif args.command == "prepare-assets":
            subprocess.run(["aap-libreyolo", "prepare-assets", "--size", args.size, "--asset-dir", str(args.asset_dir)], check=True)
        elif args.command == "check":
            if args.qualify_local:
                if not (args.device and args.gpu_uuid and args.asset_dir and args.report and args.exclusive_gpu):
                    raise ValueError("--qualify-local requires --device, --gpu-uuid, --asset-dir, --exclusive-gpu and --report")
                from .qualification import qualify_local
                report = qualify_local(args.fixtures, args.device, args.gpu_uuid, args.asset_dir, args.report)
                print(json.dumps({"mode": report["mode"], "report": str(args.report), "platform_managed_qualified": False}))
            else:
                if args.device or args.gpu_uuid or args.asset_dir or args.exclusive_gpu:
                    raise ValueError("GPU qualification options require --qualify-local")
                from .check import check_http
                report = check_http(args.url, os.environ.get("AAP_PLUGIN_TOKEN", ""), args.fixtures)
                if args.report:
                    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report))
        else:
            subprocess.run([sys.executable, "-m", "build", "--wheel", str(args.directory)], check=True)
            for artifact in sorted((args.directory / "dist").glob("*.whl")):
                print(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact}")
    except (OSError, ValueError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        print(f"aap-plugin: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

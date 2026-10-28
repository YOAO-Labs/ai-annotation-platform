"""Standalone development commands for detection plugins."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

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
    (directory / "my_libreyolo_plugin.py").write_text('''"""Standalone LibreYOLO plugin entry point."""
from aap_libreyolo_plugin.cli import main
''', encoding="utf-8")


def check(url: str, token: str, image: Path) -> None:
    if len(token) < 32:
        raise ValueError("set AAP_PLUGIN_TOKEN to the configured token (at least 32 characters)")
    data = image.read_bytes()
    if not data or len(data) > 20 * 1024 * 1024:
        raise ValueError("image must be nonempty and at most 20 MiB")
    source = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    with httpx.Client(base_url=url.rstrip("/"), timeout=120, follow_redirects=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        if health.json().get("status") != "ok":
            raise ValueError("/health did not report status=ok")
        denied = client.get("/setup")
        if denied.status_code not in (401, 403):
            raise ValueError("/setup accepted a request without a token")
        client.headers["Authorization"] = "Bearer " + token
        setup = client.get("/setup")
        setup.raise_for_status()
        setup = setup.json()
        models = setup.get("models", [])
        if not any(m.get("id") == "detect" and m.get("task") == "detect" and
                   set(m.get("supported_inputs", [])) >= {"full_image", "crop"} for m in models):
            raise ValueError("/setup must expose detect with full_image and crop inputs")
        request = {"tasks": [{"id": "check", "file_path": source}], "context": {"task": "detect", "model_id": "detect"}}
        response = client.post("/predict", json=request)
        response.raise_for_status()
        results = response.json().get("results")
        if not isinstance(results, list) or len(results) != 1 or results[0].get("task") != "check":
            raise ValueError("/predict did not preserve the supplied task id")
        for item in results[0].get("result", []):
            value = item.get("value", {})
            if item.get("type") != "rectanglelabels" or not isinstance(item.get("native_class_index"), int):
                raise ValueError("/predict returned a non-detection or missing native class index")
            score = item.get("score")
            labels = value.get("rectanglelabels")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1 or not isinstance(labels, list) or len(labels) != 1 or not isinstance(labels[0], str) or not labels[0]:
                raise ValueError("/predict returned an invalid score or native class name")
            if not (0 <= value.get("x", -1) < 100 and 0 <= value.get("y", -1) < 100 and
                    0 < value.get("width", 0) <= 100 - value["x"] and 0 < value.get("height", 0) <= 100 - value["y"]):
                raise ValueError("/predict returned an invalid percentage box")
        bad = dict(request)
        bad["context"] = {"task": "detect", "params": {"conf": 2}}
        if client.post("/predict", json=bad).status_code != 422:
            raise ValueError("/predict did not reject an invalid confidence parameter")
    print("HTTP detection check passed; local execution does not establish managed GPU qualification")


def main() -> None:
    parser = argparse.ArgumentParser(prog="aap-plugin")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("directory", type=Path)
    init.add_argument("--template", choices=["libreyolo-yolov9"], default="libreyolo-yolov9")
    dev = commands.add_parser("dev")
    dev.add_argument("--device", required=True)
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)
    dev.add_argument("--asset-dir", type=Path, required=True)
    dev.add_argument("--token-file", type=Path, required=True)
    prepare = commands.add_parser("prepare-assets")
    prepare.add_argument("--size", choices=["t", "s", "m", "c"], default="t")
    prepare.add_argument("--asset-dir", type=Path, required=True)
    verify = commands.add_parser("check")
    verify.add_argument("--url", required=True)
    verify.add_argument("--image", required=True, type=Path)
    build = commands.add_parser("build")
    build.add_argument("directory", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        if args.command == "init":
            init_project(args.directory)
        elif args.command == "dev":
            subprocess.run(["aap-libreyolo", "serve", "--device", args.device, "--asset-dir", str(args.asset_dir), "--token-file", str(args.token_file), "--host", args.host, "--port", str(args.port)], check=True)
        elif args.command == "prepare-assets":
            subprocess.run(["aap-libreyolo", "prepare-assets", "--size", args.size, "--asset-dir", str(args.asset_dir)], check=True)
        elif args.command == "check":
            check(args.url, os.environ.get("AAP_PLUGIN_TOKEN", ""), args.image)
        else:
            subprocess.run([sys.executable, "-m", "build", "--wheel", str(args.directory)], check=True)
            for artifact in sorted((args.directory / "dist").glob("*.whl")):
                print(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact}")
    except (OSError, ValueError, httpx.HTTPError, subprocess.CalledProcessError) as exc:
        print(f"aap-plugin: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

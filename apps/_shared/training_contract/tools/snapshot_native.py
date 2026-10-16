"""Snapshot pinned config source without importing or executing either framework."""

import argparse
import ast
import hashlib
import json
from pathlib import Path

import yaml


def snapshot(root):
    ultra = root / "ultralytics/cfg/default.yaml"
    libre = root / "libreyolo/training/config.py"
    defaults = yaml.safe_load(ultra.read_text())
    fields = {}
    for node in ast.parse(libre.read_text()).body:
        if isinstance(node, ast.ClassDef) and node.name in {
            "TrainConfig",
            "YOLO9Config",
        }:
            for field in node.body:
                if isinstance(field, ast.AnnAssign) and isinstance(
                    field.target, ast.Name
                ):
                    fields[field.target.id] = {
                        "default": ast.literal_eval(field.value),
                        "native_type": ast.unparse(field.annotation),
                        "defined_in": node.name,
                    }
    result = {
        "ultralytics": {
            "version": "8.4.177",
            "source": "https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/cfg/default.yaml",
            "source_sha256": hashlib.sha256(ultra.read_bytes()).hexdigest(),
            "fields": {
                key: {
                    "default": value,
                    "native_type": type(value).__name__,
                    "defined_in": "default.yaml",
                }
                for key, value in defaults.items()
            },
        },
        "libreyolo": {
            "version": "1.6.0",
            "source": "https://github.com/LibreYOLO/libreyolo/blob/809009bc294daa6e75e0976ab7cfdd0057d17d79/libreyolo/training/config.py",
            "source_sha256": hashlib.sha256(libre.read_bytes()).hexdigest(),
            "fields": fields,
        },
    }
    return json.loads(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = snapshot(args.native_root)
    if args.check:
        if json.loads(args.output.read_text()) != data:
            raise SystemExit(
                "Native configuration drift: regenerate and review every changed field"
            )
    else:
        args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

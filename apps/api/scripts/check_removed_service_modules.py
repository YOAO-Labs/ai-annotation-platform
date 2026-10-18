#!/usr/bin/env python
"""Production-image artifact scanner for removed legacy service modules.

Run inside the production API container (no volume mounts, no .venv) to verify
that all removed legacy service modules are absent from
the image: no source, stub, or cached bytecode, and every import form fails.

Usage (inside the image)::

    python /app/scripts/check_removed_service_modules.py --artifact-root /app

Exit code: 0 if all removed modules are confirmed absent, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path

_MANIFEST_REL = "tests/_fixtures/removed_service_modules.json"


def _load_manifest(artifact_root: Path) -> list[str]:
    manifest_path = (
        artifact_root / "tests" / "_fixtures" / "removed_service_modules.json"
    )
    if not manifest_path.is_file():
        # Fallback: the manifest might be at apps/api/tests/... relative to repo root.
        manifest_path = artifact_root / "apps" / "api" / _MANIFEST_REL
    if not manifest_path.is_file():
        print(f"FATAL: manifest not found under {artifact_root}", file=sys.stderr)
        sys.exit(2)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [m["facade_module"] for m in data["modules"]]


def _check_source_absence(artifact_root: Path, removed: list[str]) -> list[str]:
    """Verify no .py / .pyi / .pyc files exist for any removed module."""
    services_dir = artifact_root / "app" / "services"
    if not services_dir.is_dir():
        # Fallback for image layout where app lives under apps/api/app
        services_dir = artifact_root / "apps" / "api" / "app" / "services"
    offenders = []
    for dotted in removed:
        basename = dotted.rsplit(".", 1)[-1]
        for suffix in (".py", ".pyi"):
            p = services_dir / f"{basename}{suffix}"
            if p.exists():
                offenders.append(f"source file exists: {p}")
        # __pycache__ bytecode
        pycache = services_dir / "__pycache__"
        if pycache.is_dir():
            for f in pycache.iterdir():
                if f.name.startswith(f"{basename}.") and f.suffix in {".pyc", ".pyo"}:
                    offenders.append(f"bytecode exists: {f}")
    return offenders


def _check_import_failure(removed: list[str]) -> list[str]:
    """Verify every import form fails for each removed module."""
    offenders = []
    for dotted in removed:
        # find_spec must return None
        spec = importlib.util.find_spec(dotted)
        if spec is not None:
            offenders.append(
                f"find_spec({dotted!r}) returned a spec (module still exists)"
            )
            continue

        # Direct import must fail
        try:
            importlib.import_module(dotted)
            offenders.append(f"import {dotted} succeeded (should have failed)")
        except ModuleNotFoundError:
            pass  # expected
        except ImportError:
            pass  # also acceptable for package-attribute form

        # from app.services import <name> must fail
        parts = dotted.rsplit(".", 1)
        if len(parts) == 2 and parts[0] == "app.services":
            name = parts[1]
            try:
                importlib.import_module("app.services")
                import app.services as pkg  # noqa: F811

                if hasattr(pkg, name):
                    offenders.append(
                        f"app.services.{name} still accessible as package attribute"
                    )
            except (ImportError, ModuleNotFoundError):
                pass  # fine

    return offenders


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        required=True,
        help="Root directory of the API application inside the image.",
    )
    args = parser.parse_args()

    removed = _load_manifest(args.artifact_root)
    print(f"Checking {len(removed)} removed service modules under {args.artifact_root}")

    offenders: list[str] = []
    offenders.extend(_check_source_absence(args.artifact_root, removed))
    offenders.extend(_check_import_failure(removed))

    if offenders:
        print(f"\n✗ {len(offenders)} violation(s) found:", file=sys.stderr)
        for o in offenders:
            print(f"  {o}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ All {len(removed)} removed service modules confirmed absent.")


if __name__ == "__main__":
    main()

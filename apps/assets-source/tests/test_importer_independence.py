from __future__ import annotations

import ast
from pathlib import Path


_SOURCE_ROOT = Path(__file__).parents[1] / "src"
_FORBIDDEN_ROOTS = {"app", "sqlalchemy", "celery", "redis"}


def test_importer_and_portable_connectors_have_no_api_or_framework_imports() -> None:
    files = [
        *_SOURCE_ROOT.joinpath("aap_assets_source_import").rglob("*.py"),
        *_SOURCE_ROOT.joinpath("aap_assets_source/connectors").rglob("*.py"),
    ]
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                root = module.split(".", 1)[0]
                if root in _FORBIDDEN_ROOTS:
                    violations.append(f"{path}:{node.lineno}: {module}")

    assert violations == []

"""AST boundary guards for Learning owner services, schemas, and routes."""

from __future__ import annotations

import ast
from pathlib import Path

API_APP_DIR = Path(__file__).resolve().parents[1] / "app"
SERVICES_DIR = API_APP_DIR / "services"
SCHEMAS_DIR = API_APP_DIR / "schemas"
ROUTES_DIR = API_APP_DIR / "api" / "v1"

LEARNING_SERVICE_PATTERNS = (
    "training_*.py",
    "trained_model*.py",
    "trainer_plugins*.py",
    "learning_*.py",
    "sample_evaluation*.py",
    "active_learning*.py",
    "managed_inference*.py",
    "model_lineage.py",
    "project_task_dataset.py",
    "controlled_segmentation.py",
)

FORBIDDEN_NON_LEARNING_ORM_PREFIXES = (
    "app.db.models.user",
    "app.db.models.project",
    "app.db.models.ml_backend",
    "app.db.models.dataset",
    "app.db.models.task",
    "app.db.models.annotation",
)

FORBIDDEN_SERVICE_IMPORTS = ("app.services.ml_backend",)
FORBIDDEN_ROUTER_PRIVATE_SYMBOLS = frozenset({"_build_candidates", "_router_mode"})

DELITERALIZED_SCHEMAS = (
    "trainer_plugin.py",
    "training_run.py",
    "training_hub.py",
    "training_study.py",
    "learning_policy.py",
    "training_recipe.py",
    "training_preview.py",
    "training_initialization.py",
    "training_dataset.py",
)

DELITERALIZED_ROUTES = (
    "active_learning.py",
    "admin_trainer_plugins.py",
    "learning_policies.py",
    "sample_evaluations.py",
    "trained_models.py",
    "training_datasets.py",
    "training_hub.py",
    "training_previews.py",
    "training_recipes.py",
    "training_runs.py",
    "training_studies.py",
)

FORBIDDEN_FRAMEWORK_LITERALS = frozenset(
    {
        "ultralytics",
        "libreyolo",
        "ultralytics-bbox-1",
        "ultralytics-simple-polygon-1",
        "ultralytics-instance-rle-1",
    }
)


def _learning_service_files() -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in LEARNING_SERVICE_PATTERNS:
        for path in SERVICES_DIR.glob(pattern):
            found[path.name] = path
    return [found[name] for name in sorted(found)]


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _resolve_import_from_base(
    node: ast.ImportFrom, package_parts: tuple[str, ...]
) -> str:
    if node.level > 0:
        keep = len(package_parts) - (node.level - 1)
        prefix = ".".join(package_parts[: max(keep, 0)])
        if node.module:
            return f"{prefix}.{node.module}" if prefix else node.module
        return prefix
    return node.module or ""


def _imported_modules(
    tree: ast.AST, package_parts: tuple[str, ...] = ("app", "services")
) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from_base(node, package_parts)
            if base:
                imports.add(base)
                for alias in node.names:
                    if alias.name != "*":
                        imports.add(f"{base}.{alias.name}")
            else:
                for alias in node.names:
                    if alias.name != "*":
                        imports.add(alias.name)
    return imports


def _matches_forbidden_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


def _is_literal_subscript(node: ast.Subscript) -> bool:
    if isinstance(node.value, ast.Name):
        return node.value.id == "Literal"
    if isinstance(node.value, ast.Attribute):
        return node.value.attr == "Literal"
    return False


def test_learning_services_do_not_import_foreign_orm_or_ml_backend_service() -> None:
    forbidden = (*FORBIDDEN_NON_LEARNING_ORM_PREFIXES, *FORBIDDEN_SERVICE_IMPORTS)
    violations: list[str] = []
    for path in _learning_service_files():
        rel_parent = path.relative_to(API_APP_DIR.parent).parent.parts
        tree = _parse(path)
        for mod in sorted(_imported_modules(tree, rel_parent)):
            if _matches_forbidden_prefix(mod, forbidden):
                violations.append(f"{path.name}: {mod}")
    assert violations == []


def test_learning_services_do_not_access_private_ml_router_symbols() -> None:
    violations: list[str] = []
    for path in _learning_service_files():
        rel_parent = path.relative_to(API_APP_DIR.parent).parent.parts
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = _resolve_import_from_base(node, rel_parent)
                if base == "app.services.ml_routing.router":
                    for alias in node.names:
                        if alias.name in FORBIDDEN_ROUTER_PRIVATE_SYMBOLS:
                            violations.append(f"{path.name}: import {alias.name}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in FORBIDDEN_ROUTER_PRIVATE_SYMBOLS
            ):
                violations.append(f"{path.name}: attribute {node.attr}")
    assert violations == []


def _literal_string_values(node: ast.Subscript) -> list[str]:
    found: list[str] = []
    for sub in ast.walk(node.slice):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.append(sub.value)
    return found


def _find_hardcoded_framework_literals(tree: ast.AST) -> list[tuple[int, list[str]]]:
    hits: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_literal_subscript(node):
            bad = sorted(
                set(_literal_string_values(node)) & FORBIDDEN_FRAMEWORK_LITERALS
            )
            if bad:
                hits.append((node.lineno, bad))
    return hits


def test_learning_schemas_and_routes_forbid_hardcoded_framework_literals() -> None:
    targets = [SCHEMAS_DIR / name for name in DELITERALIZED_SCHEMAS] + [
        ROUTES_DIR / name for name in DELITERALIZED_ROUTES
    ]
    violations: list[str] = []
    for path in targets:
        tree = _parse(path)
        for lineno, bad in _find_hardcoded_framework_literals(tree):
            violations.append(f"{path.name}:{lineno}: Literal{bad}")
    assert violations == []


def test_ast_boundary_helpers_detect_evasion_patterns() -> None:
    forbidden = (*FORBIDDEN_NON_LEARNING_ORM_PREFIXES, *FORBIDDEN_SERVICE_IMPORTS)
    evasion_imports = ast.parse(
        "from app.db.models import user\n"
        "from app.services import ml_backend\n"
        "from . import ml_backend as mb\n"
    )
    found = _imported_modules(evasion_imports, ("app", "services"))
    assert "app.db.models.user" in found
    assert "app.services.ml_backend" in found
    assert any(_matches_forbidden_prefix(m, forbidden) for m in found)

    evasion_literal = ast.parse(
        "import typing\nX = typing.Literal['ultralytics', 'libreyolo']\n"
    )
    hits = _find_hardcoded_framework_literals(evasion_literal)
    assert hits == [(2, ["libreyolo", "ultralytics"])]

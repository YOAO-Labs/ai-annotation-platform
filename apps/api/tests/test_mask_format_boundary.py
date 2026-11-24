from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.services.mask_formats.contracts import (
    FormatHostPort,
    FormatProjectContext,
    MaskFormatAdapter,
)

MASK_FORMATS_DIR = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "mask_formats"
)
PURE_ADAPTER_MODULES = (
    "contracts.py",
    "registry.py",
    "planning.py",
    "adapters.py",
    "image_adapters.py",
    "video_adapters.py",
    "remote_adapter.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "sqlalchemy",
    "app.db.models",
    "app.services.annotations_import",
    "app.services.assets_media_summary",
    "app.services.labeling_core_read_relations",
    "app.services.task_matcher",
)


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_mask_format_adapters_have_no_session_or_orm_imports() -> None:
    violations: list[str] = []
    for filename in PURE_ADAPTER_MODULES:
        path = MASK_FORMATS_DIR / filename
        for imported in sorted(_module_imports(path)):
            if any(
                imported == prefix or imported.startswith(f"{prefix}.")
                for prefix in FORBIDDEN_IMPORT_PREFIXES
            ):
                violations.append(f"{filename}: {imported}")
    assert violations == []


def test_mask_format_adapter_protocol_exposes_no_session_or_orm_types() -> None:
    for method_name in ("preflight_import", "execute_import_item", "preflight_export"):
        sig = inspect.signature(getattr(MaskFormatAdapter, method_name))
        assert "db" not in sig.parameters
        assert "host" in sig.parameters
        assert "project" in sig.parameters
        rendered = str(sig)
        assert "AsyncSession" not in rendered
        assert "app.db.models" not in rendered
        assert "FormatHostPort" in rendered
        assert "FormatProjectContext" in rendered


def test_format_host_port_exposes_no_session_or_orm_types() -> None:
    for name, member in inspect.getmembers(
        FormatHostPort, predicate=inspect.isfunction
    ):
        if name.startswith("_"):
            continue
        rendered = str(inspect.signature(member))
        assert "AsyncSession" not in rendered
        assert "Project" not in rendered
        assert (
            "Task" not in rendered
            or "ResolvedTaskInfo" in rendered
            or "ExportTaskGeometrySummary" in rendered
        )
        assert "DatasetItem" not in rendered


def test_format_project_context_is_pure_value_object() -> None:
    fields = FormatProjectContext.__dataclass_fields__
    assert set(fields) == {
        "project_id",
        "data_type",
        "owner_id",
        "allowed_region_classes",
    }

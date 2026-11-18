"""Replay frozen query-body migrations against the current owned namespace."""

from __future__ import annotations

import importlib.util
from pathlib import Path

LEGACY_SCHEMA = "aap_workspace_project_query_v1"
CURRENT_SCHEMA = "yoao_workspace_project_query_v1"
_SCHEMA_REPLACEMENTS = {LEGACY_SCHEMA: CURRENT_SCHEMA}


def load_query_migration(path: Path, directory: Path):
    """Load a frozen body replacement while keeping its SQL artifacts historical.

    The 0262/0264 artifacts predate the bo0471 namespace cutover. The cutover
    owns the deployed function identities and the old namespace no longer
    exists, so the frozen source is replayed with only its schema-qualified
    identities redirected; the body and ACL mechanics stay byte-identical.
    """
    source = path.read_text(encoding="utf-8")
    for before, after in _SCHEMA_REPLACEMENTS.items():
        source = source.replace(before, after)
    module_path = directory / path.name
    module_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(path.stem, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for attribute in ("_SQL_PATH", "_PREVIOUS_SQL_PATH"):
        original = getattr(module, attribute, None)
        if original is None:
            continue
        rewritten = directory / f"{attribute}_{Path(original).name}"
        sql = Path(original).read_text(encoding="utf-8")
        for before, after in _SCHEMA_REPLACEMENTS.items():
            sql = sql.replace(before, after)
        rewritten.write_text(sql, encoding="utf-8")
        setattr(module, attribute, rewritten)
    return module

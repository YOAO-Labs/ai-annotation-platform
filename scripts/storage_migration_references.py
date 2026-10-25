"""Compatibility alias preserving the mounted core and its monkeypatch seams."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from app.services import storage_migration_references as _core  # noqa: E402

sys.modules[__name__] = _core

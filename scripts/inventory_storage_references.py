"""Compatibility alias preserving the mounted core and its monkeypatch seams."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from app.services import storage_migration_reference_collector as _core  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_core.main())

sys.modules[__name__] = _core

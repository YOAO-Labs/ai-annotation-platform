"""Compatibility module for the mounted application inventory core.

Alias the real module so private symbols and monkeypatches retain their identity.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from app.services import storage_migration_inventory as _core  # noqa: E402

sys.modules[__name__] = _core

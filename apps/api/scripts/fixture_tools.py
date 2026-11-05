"""Locate the coordinator tools supplied to private fixture commands."""

import os
from pathlib import Path
import sys


def configure_fixture_tools_path() -> None:
    raw = os.environ.get("AAP_FIXTURE_TOOLS_ROOT")
    if not raw:
        raise RuntimeError("AAP_FIXTURE_TOOLS_ROOT is required")
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir():
        raise RuntimeError(
            "AAP_FIXTURE_TOOLS_ROOT must be an existing absolute directory"
        )
    sys.path.insert(0, str(root))

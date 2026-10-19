#!/usr/bin/env python3
"""Regenerate the portable protocol and private HTTP snapshots."""

from __future__ import annotations

import json
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "src"))

from aap_labeling_mask_gc.contract import contract_document  # noqa: E402
from aap_labeling_mask_gc_runtime.api import openapi_document  # noqa: E402


def main() -> None:
    for name, document in (
        ("protocol.schema.json", contract_document()),
        ("openapi.snapshot.json", openapi_document()),
    ):
        path = APP_ROOT / "src" / "aap_labeling_mask_gc" / name
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

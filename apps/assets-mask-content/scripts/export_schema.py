"""Write the frozen JSON Schema and OpenAPI snapshots for this distribution."""

from __future__ import annotations

import json
from pathlib import Path

from aap_assets_mask_content.contract import contract_document, openapi_document


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_assets_mask_content"


def main() -> None:
    for name, document in (
        ("protocol.schema.json", contract_document()),
        ("openapi.snapshot.json", openapi_document()),
    ):
        (PACKAGE / name).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

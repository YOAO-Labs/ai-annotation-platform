"""Write the frozen JSON Schema snapshot for the portable contract."""

from __future__ import annotations

import json
from pathlib import Path

from aap_preannotation_results.contract import contract_document


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_preannotation_results"
PRINT_WIDTH = 100


def _format_json(value, depth: int = 0, line_prefix: str | None = None) -> str:
    """Write sorted JSON with the repository's Prettier-compatible array layout."""
    prefix = " " * (depth * 2)
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = []
        for key in sorted(value):
            row_prefix = " " * ((depth + 1) * 2) + json.dumps(key, ensure_ascii=False)
            rows.append(
                f"{row_prefix}: "
                f"{_format_json(value[key], depth + 1, row_prefix + ': ')}"
            )
        return "{\n" + ",\n".join(rows) + f"\n{prefix}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(item is None or type(item) in {str, bool, int, float} for item in value):
            flat = (
                "["
                + ", ".join(
                    json.dumps(item, ensure_ascii=False, allow_nan=False)
                    for item in value
                )
                + "]"
            )
            if len(line_prefix or prefix) + len(flat) <= PRINT_WIDTH:
                return flat
        rows = [
            f"{' ' * ((depth + 1) * 2)}"
            f"{_format_json(item, depth + 1, ' ' * ((depth + 1) * 2))}"
            for item in value
        ]
        return "[\n" + ",\n".join(rows) + f"\n{prefix}]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def main() -> None:
    (PACKAGE / "protocol.schema.json").write_text(
        _format_json(contract_document()) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

"""Write the frozen JSON Schema snapshot for prediction retry operations."""

from __future__ import annotations

from pathlib import Path

from aap_preannotation_results.retry_contract import contract_document
from export_schema import _format_json


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_preannotation_results"


def main() -> None:
    (PACKAGE / "prediction-retries.schema.json").write_text(
        _format_json(contract_document()) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

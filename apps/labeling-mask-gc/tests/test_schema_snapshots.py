from __future__ import annotations

import json
from pathlib import Path

from aap_labeling_mask_gc.contract import contract_document
from aap_labeling_mask_gc_runtime.api import openapi_document

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "src" / "aap_labeling_mask_gc"


def test_protocol_schema_snapshot_matches_contract():
    snapshot = json.loads((SCHEMA_DIR / "protocol.schema.json").read_text())
    assert snapshot == contract_document()


def test_private_openapi_snapshot_matches_routes():
    snapshot = json.loads((SCHEMA_DIR / "openapi.snapshot.json").read_text())
    assert snapshot == openapi_document()
    assert set(snapshot["paths"]) == {
        "/v1/mask-gc/run",
        "/v1/mask-gc/progress",
    }

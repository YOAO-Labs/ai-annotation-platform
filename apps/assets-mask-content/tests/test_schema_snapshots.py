from __future__ import annotations

import json
from pathlib import Path

from aap_assets_mask_content.contract import contract_document, openapi_document
from aap_assets_mask_content.gc_contract import (
    contract_document as gc_contract_document,
    openapi_document as gc_openapi_document,
)

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_assets_mask_content"


def test_installed_protocol_schema_matches_frozen_contract():
    actual = json.loads((PACKAGE / "protocol.schema.json").read_text("utf-8"))
    assert actual == contract_document()


def test_installed_openapi_snapshot_matches_frozen_http_paths():
    actual = json.loads((PACKAGE / "openapi.snapshot.json").read_text("utf-8"))
    assert actual == openapi_document()
    assert set(actual["paths"]) == {
        "/v1/mask-content/put",
        "/v1/mask-content/get",
        "/v1/mask-content/list",
        "/v1/mask-content/delete",
    }


def test_installed_gc_schema_matches_frozen_contract():
    actual = json.loads((PACKAGE / "gc.protocol.schema.json").read_text("utf-8"))
    assert actual == gc_contract_document()


def test_installed_gc_openapi_snapshot_has_only_cleanup_routes():
    actual = json.loads((PACKAGE / "gc.openapi.snapshot.json").read_text("utf-8"))
    assert actual == gc_openapi_document()
    assert set(actual["paths"]) == {
        "/v1/mask-gc/scan",
        "/v1/mask-gc/execute",
        "/v1/mask-gc/status",
    }

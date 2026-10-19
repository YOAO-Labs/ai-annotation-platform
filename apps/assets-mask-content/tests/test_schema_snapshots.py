from __future__ import annotations

import json
from pathlib import Path

from aap_assets_mask_content.contract import contract_document, openapi_document

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

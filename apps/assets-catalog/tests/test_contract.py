from datetime import UTC, datetime
import subprocess
import sys
from uuid import UUID, uuid4
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_assets_catalog.contract import (
    CATALOG_SCHEMA,
    CatalogFailure,
    CatalogItem,
    CreateDatasetRequest,
    DatasetPatch,
    DatasetMutationResult,
    DatasetPage,
    DatasetSummary,
    ListDatasetsRequest,
    decode_reply,
)


def _dataset() -> dict:
    return {
        "id": str(uuid4()),
        "display_id": "D-12",
        "name": "sample",
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "has_scenes": False,
        "file_count": 0,
        "total_size": 0,
        "created_by": str(uuid4()),
        "project_count": 0,
        "axis_convention": None,
        "created_at": "2026-10-18T00:00:00Z",
        "updated_at": "2026-10-18T00:00:00Z",
    }


def test_catalog_requests_are_frozen_strict_and_keep_pagination_bounds() -> None:
    request = ListDatasetsRequest(actor_id=uuid4())
    assert request.limit == 50
    assert request.offset == 0
    with pytest.raises(ValidationError):
        ListDatasetsRequest(actor_id=uuid4(), limit=True)
    with pytest.raises(ValidationError):
        ListDatasetsRequest(actor_id=uuid4(), limit=201)
    with pytest.raises(ValidationError):
        request.offset = 1  # type: ignore[misc]


def test_dataset_patch_preserves_omitted_and_explicit_null_fields() -> None:
    assert DatasetPatch().native_payload() == {}
    assert DatasetPatch(name=None, axis_convention=None).native_payload() == {
        "name": None,
        "axis_convention": None,
    }
    assert DatasetPatch(name="renamed").native_payload() == {"name": "renamed"}


def test_create_native_payload_contains_defaults_but_not_transport_identity() -> None:
    request = CreateDatasetRequest(
        operation_id=uuid4(), actor_id=uuid4(), name="sample"
    )
    assert request.native_payload() == {
        "name": "sample",
        "description": "",
        "data_type": "image",
        "is_temporal": False,
        "axis_convention": None,
    }


def test_success_reply_decodes_exact_typed_branch() -> None:
    result = decode_reply(
        {
            "schema": CATALOG_SCHEMA,
            "ok": True,
            "data": {
                "items": [_dataset()],
                "total": 1,
                "limit": 50,
                "offset": 0,
            },
        },
        DatasetPage,
    )
    assert result.items[0].id == UUID(result.items[0].id.hex)
    assert isinstance(result.items[0].created_at, datetime)
    assert result.items[0].created_at.utcoffset() == UTC.utcoffset(None)


def test_business_failure_is_typed_and_unknown_or_malformed_replies_fail_closed() -> (
    None
):
    with pytest.raises(CatalogFailure, match="catalog_manager_required") as raised:
        decode_reply(
            {
                "schema": CATALOG_SCHEMA,
                "ok": False,
                "error": {"code": "catalog_manager_required"},
            },
            DatasetMutationResult,
        )
    assert raised.value.code == "catalog_manager_required"

    for payload in (
        {"schema": "aap-assets-catalog/v2", "ok": True, "data": {}},
        {
            "schema": CATALOG_SCHEMA,
            "ok": False,
            "error": {"code": "db_error"},
        },
        {"schema": CATALOG_SCHEMA, "ok": True, "data": {}, "ignored": True},
        {"schema": CATALOG_SCHEMA, "ok": "true", "data": {}},
    ):
        with pytest.raises(ValueError):
            decode_reply(payload, DatasetSummary)


def test_item_public_projection_removes_only_internal_signing_key() -> None:
    item = CatalogItem(
        id=uuid4(),
        dataset_id=uuid4(),
        file_name="frame.jpg",
        file_path="legacy/frame.jpg",
        file_type="image",
        file_size=42,
        content_hash=None,
        width=10,
        height=20,
        metadata={"calibration": {"vendor_field": [1, 2, 3]}, "unknown": "kept"},
        file_url="https://storage.example/source",
        thumbnail_url=None,
        blurhash=None,
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
        thumbnail_path="thumbnails/task/preview.webp",
    )
    public = item.public_payload()
    assert "thumbnail_path" not in public
    assert public["metadata"] == {
        "calibration": {"vendor_field": [1, 2, 3]},
        "unknown": "kept",
    }


def test_core_import_does_not_load_server_or_api_modules() -> None:
    source_dir = Path(__file__).parents[1] / "src"
    workspace_source_dir = (
        Path(__file__).parents[2] / "_shared" / "workspace_contract" / "src"
    )
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(source_dir)!r}); "
        f"sys.path.insert(0, {str(workspace_source_dir)!r}); "
        "import aap_assets_catalog; "
        "forbidden = {'fastapi', 'asyncpg', 'boto3', 'sqlalchemy', 'app'}; "
        "assert not forbidden.intersection(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)

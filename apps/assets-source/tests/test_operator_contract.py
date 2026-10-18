from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_source.operator_contract import (
    OPERATOR_NATIVE_ARGUMENTS,
    OPERATOR_NATIVE_OPERATIONS,
    OPERATOR_NATIVE_SIGNATURES,
    OPERATOR_SCHEMA,
    AppendOperatorItemsRequest,
    CreateOperatorImageDatasetRequest,
    EnsureOperatorNuScenesDatasetRequest,
    NuScenesDatasetSpec,
    OperatorDataset,
    OperatorDatasetMutation,
    OperatorDimensions,
    OperatorFailure,
    OperatorItem,
    OperatorItemsMutation,
    OperatorItemsPage,
    OperatorSourceEntry,
    OperatorSourcePatch,
    PatchOperatorItemsRequest,
    ReadOperatorItemsRequest,
    decode_operator_reply,
    operator_contract_document,
    require_operator_scope,
)


def _item(dataset_id, entry):
    return OperatorItem(
        **entry.model_dump(),
        dataset_id=dataset_id,
        scene_id=None,
        frame_index=None,
        source_snapshot="a" * 64,
    )


def test_operator_contract_is_installed_with_six_exact_native_signatures():
    assert len(OPERATOR_NATIVE_OPERATIONS) == 6
    expected = operator_contract_document()
    assert expected["$id"] == "urn:aap:assets-operator:v1"
    for name, pair in OPERATOR_NATIVE_OPERATIONS.items():
        assert len(OPERATOR_NATIVE_ARGUMENTS[name]) == len(
            OPERATOR_NATIVE_SIGNATURES[name].split(",")
        )
        for model in pair:
            assert model.__name__ in expected["$defs"]
    source = (
        Path(__file__).resolve().parents[1]
        / "src/aap_assets_source/operator_contract.schema.json"
    )
    assert json.loads(source.read_text()) == expected
    native = (
        Path(__file__).resolve().parents[3]
        / "apps/api/alembic/sql/0218_assets_operator.schema.json"
    )
    if native.exists():
        assert source.read_bytes() == native.read_bytes()


@pytest.mark.parametrize(
    "metadata",
    [{"bad": float("nan")}, {"bad": float("inf")}, {"nested": {"key": "nul\x00"}}],
)
def test_operator_requests_reject_non_postgresql_json_before_binding(metadata):
    with pytest.raises(ValidationError):
        OperatorSourceEntry(
            id=uuid4(),
            file_name="file.jpg",
            file_path="folder/file.jpg",
            file_type="image",
            metadata=metadata,
        )


def test_operator_append_rejects_wrong_order_wrong_parent_and_changed_source():
    dataset_id = uuid4()
    entries = [
        OperatorSourceEntry(
            id=uuid4(), file_name=name, file_path="folder/" + name, file_type="image"
        )
        for name in ("one.jpg", "two.jpg")
    ]
    request = AppendOperatorItemsRequest(
        operation_id=uuid4(), dataset_id=dataset_id, entries=entries
    )
    result = OperatorItemsMutation(
        operation_id=request.operation_id,
        dataset_id=dataset_id,
        action="append_items",
        items=[_item(dataset_id, entry) for entry in entries],
        file_count=2,
        dimensions_backfilled=0,
    )
    require_operator_scope(request, result)
    with pytest.raises(ValueError):
        require_operator_scope(
            request, result.model_copy(update={"items": result.items[::-1]})
        )
    with pytest.raises(ValueError):
        require_operator_scope(
            request, result.model_copy(update={"dataset_id": uuid4()})
        )
    with pytest.raises(ValueError):
        require_operator_scope(
            request,
            result.model_copy(
                update={
                    "items": [
                        result.items[0].model_copy(update={"file_path": "different"}),
                        result.items[1],
                    ]
                }
            ),
        )
    with pytest.raises(ValidationError):
        AppendOperatorItemsRequest(
            operation_id=uuid4(),
            dataset_id=dataset_id,
            entries=[entries[0], entries[0]],
        )


def test_operator_reply_keeps_strict_namespace_and_failure_codes():
    failure = {
        "schema": OPERATOR_SCHEMA,
        "ok": False,
        "error": {"code": "source_changed"},
    }
    with pytest.raises(OperatorFailure) as rejected:
        decode_operator_reply(failure, OperatorItemsMutation)
    assert rejected.value.code == "source_changed"
    for value in (
        {**failure, "schema": "aap-assets-source/v1"},
        {**failure, "ok": 0},
        {**failure, "error": {"code": "unknown"}},
        {**failure, "data": {}},
    ):
        with pytest.raises(ValueError):
            decode_operator_reply(value, OperatorItemsMutation)


def test_operator_image_creation_cannot_return_another_owner_or_dataset():
    request = CreateOperatorImageDatasetRequest(
        operation_id=uuid4(),
        created_by=uuid4(),
        name="images",
        description="local files",
    )
    now = datetime.now(timezone.utc)
    dataset = OperatorDataset(
        id=request.operation_id,
        display_id=f"DS-{request.operation_id.hex[:6].upper()}",
        name=request.name,
        description=request.description,
        data_type="image",
        is_temporal=False,
        file_count=0,
        created_by=request.created_by,
        metadata={},
        created_at=now,
        updated_at=now,
    )
    result = OperatorDatasetMutation(
        operation_id=request.operation_id,
        dataset_id=dataset.id,
        action="create_image_dataset",
        dataset=dataset,
        storage_root=None,
        needs_pcd_upgrade=None,
    )
    require_operator_scope(request, result)
    with pytest.raises(ValueError):
        require_operator_scope(
            request,
            result.model_copy(
                update={"dataset": dataset.model_copy(update={"created_by": uuid4()})}
            ),
        )


def test_operator_pages_require_progress_and_complete_scope():
    dataset_id = uuid4()
    entry = OperatorSourceEntry(
        id=uuid4(), file_name="file.jpg", file_path="file.jpg", file_type="image"
    )
    item = _item(dataset_id, entry)
    request = ReadOperatorItemsRequest(dataset_id=dataset_id)
    result = OperatorItemsPage(
        dataset_id=dataset_id,
        after_item_id=None,
        next_item_id=entry.id,
        snapshot_digest="a" * 64,
        total=2,
        complete=False,
        items=[item],
    )
    require_operator_scope(request, result)
    with pytest.raises(ValueError):
        require_operator_scope(request, result.model_copy(update={"items": []}))
    with pytest.raises(ValueError):
        require_operator_scope(
            request, result.model_copy(update={"next_item_id": None})
        )


def test_operator_dimension_reply_checks_fill_count_without_changing_existing_values():
    dataset_id = uuid4()
    entry = OperatorSourceEntry(
        id=uuid4(),
        file_name="camera.jpg",
        file_path="camera.jpg",
        file_type="image",
        width=320,
        height=200,
    )
    item = _item(dataset_id, entry)
    patch = OperatorSourcePatch(
        id=item.id,
        source_snapshot=item.source_snapshot,
        dimensions=OperatorDimensions(width=640, height=480),
    )
    request = PatchOperatorItemsRequest(
        operation_id=uuid4(), dataset_id=dataset_id, entries=[patch]
    )
    unchanged = OperatorItemsMutation(
        operation_id=request.operation_id,
        dataset_id=dataset_id,
        action="patch_items",
        items=[item],
        file_count=1,
        dimensions_backfilled=0,
    )
    require_operator_scope(request, unchanged)
    with pytest.raises(ValueError):
        require_operator_scope(
            request, unchanged.model_copy(update={"dimensions_backfilled": 1})
        )
    with pytest.raises(ValueError):
        require_operator_scope(
            request, unchanged.model_copy(update={"dimensions_backfilled": 2})
        )


def test_operator_nuscenes_reuse_preserves_existing_name_kind_and_creator():
    owner_id = uuid4()
    request = EnsureOperatorNuScenesDatasetRequest(
        operation_id=uuid4(),
        owner_id=owner_id,
        spec=NuScenesDatasetSpec(
            display_id="DS-EXISTING",
            name="new-CLI-name",
            version="v1.0-mini",
            scene_names=["scene"],
            axis_convention="apollo",
            frame="sensor",
            pcd_encoding="binary",
        ),
    )
    now = datetime.now(timezone.utc)
    source = {
        "format": "nuscenes",
        "version": "v1.0-mini",
        "scenes": ["scene"],
        "pcd_encoding": "binary",
        "storage_root": "new-CLI-name",
        "storage_layout": "legacy_name",
    }
    dataset = OperatorDataset(
        id=uuid4(),
        display_id=request.spec.display_id,
        name="existing Dataset name",
        description="retained",
        data_type="lidar",
        is_temporal=True,
        file_count=3,
        created_by=uuid4(),
        metadata={"source": source, "keep": True},
        created_at=now,
        updated_at=now,
    )
    result = OperatorDatasetMutation(
        operation_id=request.operation_id,
        dataset_id=dataset.id,
        action="ensure_nuscenes_dataset",
        dataset=dataset,
        storage_root="new-CLI-name",
        needs_pcd_upgrade=True,
    )
    require_operator_scope(request, result)

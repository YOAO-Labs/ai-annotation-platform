"""Import inventory claims require the saved endpoint, bucket and physical key."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import import_storage, storage_usage_categories as categories
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_storage_usage_categories import family_case


@pytest.mark.parametrize("external", [False, True])
async def test_import_lease_managed_root_keeps_endpoint_identity(external):
    index = next(
        i
        for i, family in enumerate(categories.FAMILIES)
        if family.service == "prediction_inputs"
    )
    family, data, logical_key = family_case(index)
    endpoint = (
        "https://other-import.example.test" if external else categories.endpoint()
    )
    prefix = "team/_aap_managed/import/" + data["storage_ref"]["key_prefix"]
    data["storage_ref"].update(
        schema=2,
        binding_generation=2,
        location_id=str(uuid.uuid4()),
        key_prefix=prefix,
        endpoint_url=endpoint,
        storage_identity=managed_storage_identity(endpoint, "listed-bucket"),
    )
    key = "team/_aap_managed/import/" + logical_key
    db = SimpleNamespace(execute=AsyncMock(return_value=[(f"ref:{index}", data)]))
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 17}])
    assert result["categories"]["unclassified" if external else family.category] == {
        "object_count": 1,
        "size_bytes": 17,
    }
    db.execute.assert_awaited_once()


@pytest.mark.parametrize("kind", ["mask_upload", "mask_batch"])
@pytest.mark.parametrize("changed", [None, "endpoint", "bucket", "key"])
async def test_mask_claim_never_uses_logical_key_as_physical_address(kind, changed):
    logical = f"mask-formats/{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}/"
    ref = import_storage.legacy_ref(logical, bucket="listed-bucket")
    ref.update(
        schema=2,
        binding_generation=2,
        location_id=str(uuid.uuid4()),
        key_prefix="team/_aap_managed/import/" + logical,
    )
    if changed == "endpoint":
        ref.update(endpoint_url="https://other-import.example.test")
        ref["storage_identity"] = managed_storage_identity(
            ref["endpoint_url"], ref["bucket"]
        )
    elif changed == "bucket":
        ref.update(bucket="other-bucket")
        ref["storage_identity"] = managed_storage_identity(
            ref["endpoint_url"], ref["bucket"]
        )
    key = ref["key_prefix"] + "annotations.json"
    data = {"logical_key": logical + "annotations.json", "storage_ref": deepcopy(ref)}
    if changed == "key":
        key = logical + "annotations.json"
    db = SimpleNamespace(execute=AsyncMock(return_value=[(kind, data)]))
    result = await categories.classify(db, "listed-bucket", [{"key": key, "size": 19}])
    assert result["categories"]["unclassified" if changed else "temporary"] == {
        "object_count": 1,
        "size_bytes": 19,
    }
    db.execute.assert_awaited_once()

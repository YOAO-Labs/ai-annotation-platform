from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from aap_assets_cleanup.contract import (
    AssetStorageDeletion,
    CleanupBucket,
    DeletionCursor,
    PendingAssetStorageDeletion,
)


def _row(**overrides):
    asset_id = uuid4()
    row = {
        "operation_id": uuid4(),
        "asset_id": asset_id,
        "bucket": "media-cache",
        "prefix": f"image-pyramids/{asset_id}/",
    }
    row.update(overrides)
    return row


def test_four_field_contract_is_frozen_and_has_no_private_fields():
    row = AssetStorageDeletion.model_validate(_row())

    assert set(row.model_dump()) == {"operation_id", "asset_id", "bucket", "prefix"}
    with pytest.raises(ValidationError):
        row.bucket = "other-bucket"
    with pytest.raises(ValidationError):
        AssetStorageDeletion.model_validate({**_row(), "created_by": uuid4()})


def test_pending_contract_adds_timezone_aware_cursor_and_exact_asset_prefix():
    row = PendingAssetStorageDeletion.model_validate(
        {**_row(), "created_at": datetime(2026, 10, 18, tzinfo=timezone.utc)}
    )

    cursor = DeletionCursor.after(row)
    assert cursor.created_at == row.created_at
    assert cursor.operation_id == row.operation_id
    assert cursor.asset_id == row.asset_id
    with pytest.raises(ValidationError):
        PendingAssetStorageDeletion.model_validate(
            {**_row(), "created_at": datetime(2026, 10, 18)}
        )
    with pytest.raises(ValidationError):
        AssetStorageDeletion.model_validate(_row(prefix="image-pyramids/not-an-id/"))


@pytest.mark.parametrize("bucket", ["127.0.0.1", "Media-Cache", "x", "with/slash"])
def test_bucket_contract_rejects_invalid_scopes(bucket):
    with pytest.raises(ValidationError):
        TypeAdapter(CleanupBucket).validate_python(bucket)


def test_bucket_contract_accepts_dns_bucket():
    assert TypeAdapter(CleanupBucket).validate_python("media-cache.test") == (
        "media-cache.test"
    )

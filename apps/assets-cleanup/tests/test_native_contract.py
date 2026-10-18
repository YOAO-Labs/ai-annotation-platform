import json
from pathlib import Path
import re

from aap_assets_cleanup.contract import (
    AssetStorageDeletion,
    PendingAssetStorageDeletion,
)

ROOT = Path(__file__).resolve().parents[3]


def test_schema_freezes_only_the_cleanup_calls_and_pending_row_fields():
    schema = json.loads(
        (
            ROOT / "apps/api/alembic/sql/0210_asset_storage_cleanup.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["x-postgresql-functions"] == [
        "aap_assets_v1.pending_storage_deletions_v2(text, timestamptz, uuid, uuid, integer)",
        "aap_assets_v1.complete_storage_deletion(uuid, uuid)",
    ]
    assert schema["$defs"]["pendingDeletionRow"]["additionalProperties"] is False
    assert set(schema["$defs"]["pendingDeletionRow"]["required"]) == {
        "operation_id",
        "asset_id",
        "bucket",
        "prefix",
        "created_at",
    }
    assert set(AssetStorageDeletion.model_fields) == {
        "operation_id",
        "asset_id",
        "bucket",
        "prefix",
    }
    assert set(PendingAssetStorageDeletion.model_fields) == {
        "operation_id",
        "asset_id",
        "bucket",
        "prefix",
        "created_at",
    }
    assert set(
        schema["$defs"]["pendingCall"]["properties"]["arguments"]["required"]
    ) == {
        "p_bucket",
        "p_after_created_at",
        "p_after_operation",
        "p_after_asset",
        "p_limit",
    }


def test_pending_sql_filters_bucket_deadline_and_uses_the_full_keyset_order():
    sql = (ROOT / "apps/api/alembic/sql/0210_asset_storage_cleanup.sql").read_text(
        encoding="utf-8"
    )

    assert "deletion.bucket = p_bucket" in sql
    assert "deletion.not_before <= pg_catalog.statement_timestamp()" in sql
    assert "IF p_after_created_at IS NULL THEN" in sql
    assert re.search(
        r"\(deletion\.created_at, deletion\.operation_id, deletion\.asset_id\)\s*>\s*"
        r"\(p_after_created_at, p_after_operation, p_after_asset\)",
        sql,
    )
    assert (
        "ORDER BY deletion.created_at, deletion.operation_id, deletion.asset_id" in sql
    )

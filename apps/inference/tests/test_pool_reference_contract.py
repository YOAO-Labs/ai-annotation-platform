from __future__ import annotations

import json
from pathlib import Path


def test_pool_reference_schema_and_packaged_sql_describe_fixed_primary_key_read():
    sql_dir = Path(__file__).resolve().parents[2] / "api" / "alembic" / "sql"
    schema = json.loads((sql_dir / "inference.contracts.json").read_text())[
        "artifacts"
    ]["0356_inference_pool_reference.schema.json"]
    functions = schema["functions"]
    assert [function["signature"] for function in functions] == [
        "aap_inference_v1.ai_mask_pool_reference_row(uuid)"
    ]
    function = functions[0]
    assert function["volatility"] == "STABLE"
    assert function["returns"]["kind"] == "table"
    columns = function["returns"]["columns"]
    assert [column["name"] for column in columns] == [
        "id",
        "name",
        "enabled",
        "routing_policy",
        "legacy_instance_id",
        "routing_generation",
        "capability_fingerprint",
        "capability_snapshot",
        "created_at",
        "updated_at",
    ]
    assert {column["name"] for column in columns if column["nullable"]} == {
        "legacy_instance_id",
        "capability_fingerprint",
        "capability_snapshot",
    }
    sql = (sql_dir / "0356_inference_pool_reference.sql").read_text()
    assert "FROM public.ml_backend_service_pools AS pool" in sql
    assert "JOIN public.ml_backend_registry" not in sql
    assert "ml_backend_pool_members" not in sql
    assert "FOR UPDATE" not in sql

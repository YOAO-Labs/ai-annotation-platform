from __future__ import annotations

import json
from pathlib import Path


def test_pool_reference_schema_and_packaged_sql_describe_fixed_primary_key_read():
    package_dir = Path(__file__).resolve().parents[1] / "src/aap_inference/sql"
    schema = json.loads(
        (package_dir / "0356_inference_pool_reference.schema.json").read_text()
    )
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
    sql = (package_dir / "0356_inference_pool_reference.sql").read_text()
    assert "FROM public.ml_backend_service_pools AS pool" in sql
    assert "JOIN public.ml_backend_registry" not in sql
    assert "ml_backend_pool_members" not in sql
    assert "FOR UPDATE" not in sql

    repository = Path(__file__).resolve().parents[3]
    api_sql = repository / "apps/api/alembic/sql/0356_inference_pool_reference.sql"
    api_schema = (
        repository / "apps/api/alembic/sql/0356_inference_pool_reference.schema.json"
    )
    assert (
        api_sql.read_bytes()
        == (package_dir / "0356_inference_pool_reference.sql").read_bytes()
    )
    assert json.loads(api_schema.read_text()) == schema

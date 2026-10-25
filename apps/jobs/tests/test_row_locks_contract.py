"""The row-lock ABI and installed resources stay language independent and frozen."""

import json
from pathlib import Path

from aap_jobs.row_lock_contract import jobs_row_lock_schema, jobs_row_lock_sql


def test_native_row_lock_contract_and_frozen_resources_match():
    root = Path(__file__).resolve().parents[3]
    schema = jobs_row_lock_schema()
    sql = jobs_row_lock_sql()
    package = root / "apps/jobs/src/aap_jobs"
    frozen = root / "apps/api/alembic/sql"
    assert json.loads((package / "row_locks.schema.json").read_text()) == schema
    assert (
        json.loads((frozen / "0328_jobs_row_locks.schema.json").read_text()) == schema
    )
    assert (frozen / "0328_jobs_row_locks.sql").read_text() == sql
    assert len(schema["x-row-columns"]) == 15
    functions = schema["x-postgresql-functions"]
    assert len(functions) == 2
    assert {entry["lock"] for entry in functions.values()} == {
        "FOR UPDATE",
        "FOR SHARE NOWAIT",
    }
    assert sql.count("VOLATILE\nSECURITY DEFINER") == 2
    assert "RETURNS SETOF public.async_jobs" not in sql

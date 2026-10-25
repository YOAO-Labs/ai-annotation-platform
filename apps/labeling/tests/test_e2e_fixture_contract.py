from __future__ import annotations

import json
import uuid
from importlib.resources import files
from pathlib import Path

import pytest

from aap_labeling.e2e_fixture_contract import (
    LABELING_E2E_FIXTURE_FUNCTION,
    LABELING_E2E_FIXTURE_PROTOCOL,
    InsertFixtureBatches,
    LabelingE2EFixtureUnavailable,
    TaskBatchFixtureRow,
    decode_fixture_write_reply,
    e2e_fixture_schema,
    fixture_command_json,
)


def test_fixture_schema_and_sql_resources_match_the_frozen_migration():
    root = Path(__file__).resolve().parents[3]
    migration = root / "apps/api/alembic/sql/0362_labeling_e2e_fixture_writes"
    resource = files("aap_labeling")
    packaged_sql = resource.joinpath(
        "sql/0362_labeling_e2e_fixture_writes.sql"
    ).read_text(encoding="utf-8")
    packaged_schema = json.loads(
        resource.joinpath("e2e-fixtures.schema.json").read_text(encoding="utf-8")
    )

    assert packaged_sql == migration.with_suffix(".sql").read_text(encoding="utf-8")
    assert packaged_schema == json.loads(
        migration.with_suffix(".schema.json").read_text(encoding="utf-8")
    )
    assert packaged_schema == e2e_fixture_schema()
    assert packaged_schema["$id"] == LABELING_E2E_FIXTURE_PROTOCOL
    assert (
        packaged_schema["x-postgresql-functions"][LABELING_E2E_FIXTURE_FUNCTION][
            "arguments"
        ]
        == "jsonb"
    )


def test_fixture_request_preserves_omitted_defaults_and_explicit_null():
    project_id = uuid.uuid4()
    batch_id = uuid.uuid4()

    def request(**fields):
        return InsertFixtureBatches(
            schema=LABELING_E2E_FIXTURE_PROTOCOL,
            operation="insert_batches",
            project_id=project_id,
            rows=[
                TaskBatchFixtureRow(
                    id=batch_id,
                    project_id=project_id,
                    display_id="B-E2E-DEFAULTS",
                    name="Default semantics",
                    **fields,
                )
            ],
        )

    omitted = json.loads(fixture_command_json(request()))["rows"][0]
    explicit_null = json.loads(
        fixture_command_json(request(status=None, reviewer_id=None))
    )["rows"][0]

    assert "status" not in omitted
    assert "reviewer_id" not in omitted
    assert explicit_null["status"] is None
    assert explicit_null["reviewer_id"] is None

    reply = {
        "schema": LABELING_E2E_FIXTURE_PROTOCOL,
        "operation": "insert_batches",
        "ok": True,
        "data": {"ids": [str(batch_id)]},
    }
    assert decode_fixture_write_reply(request(), reply).ids == [batch_id]


def test_fixture_reply_rejects_wrong_operation_and_row_count():
    request = InsertFixtureBatches(
        schema=LABELING_E2E_FIXTURE_PROTOCOL,
        operation="insert_batches",
        project_id=uuid.uuid4(),
        rows=[
            TaskBatchFixtureRow(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                display_id="B-E2E-REPLY",
                name="Reply binding",
            )
        ],
    )
    with pytest.raises(LabelingE2EFixtureUnavailable):
        decode_fixture_write_reply(
            request,
            {
                "schema": LABELING_E2E_FIXTURE_PROTOCOL,
                "operation": "patch_batches",
                "ok": True,
                "data": {"ids": []},
            },
        )

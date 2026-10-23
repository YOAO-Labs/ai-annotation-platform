from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aap_workspace.media_seed_project_contract import (
    MEDIA_SEED_PROJECT_PROTOCOL,
    MEDIA_SEED_PROJECT_PROJECTION_FIELDS,
    MediaSeedProjectCreated,
    MediaSeedProjectUnavailable,
    SeedProjectProjectionWrite,
    decode_media_seed_project_created,
    decode_seed_project_projection_write,
    media_seed_project_schema,
)


def _created(*, kind: str, display_id: str, project_id: uuid.UUID) -> dict:
    return {
        "schema": MEDIA_SEED_PROJECT_PROTOCOL,
        "kind": kind,
        "display_id": display_id,
        "project_id": str(project_id),
    }


def _projection_write(
    *, project_id: uuid.UUID, found: bool, updated_at: str | None
) -> dict:
    return {
        "schema": MEDIA_SEED_PROJECT_PROTOCOL,
        "project_id": str(project_id),
        "found": found,
        "updated_at": updated_at,
    }


def test_media_seed_create_receipt_binds_kind_display_and_project_identity():
    project_id = uuid.uuid4()
    reply = _created(kind="video", display_id="P-VIDEO-DEV", project_id=project_id)

    assert (
        decode_media_seed_project_created(
            reply,
            kind="video",
            display_id="P-VIDEO-DEV",
            expected_project_id=project_id,
        )
        == project_id
    )
    assert (
        decode_media_seed_project_created(
            json.dumps(reply),
            kind="video",
            display_id="P-VIDEO-DEV",
            expected_project_id=project_id,
        )
        == project_id
    )
    assert (
        decode_media_seed_project_created(
            MediaSeedProjectCreated.model_validate_json(json.dumps(reply)),
            kind="video",
            display_id="P-VIDEO-DEV",
            expected_project_id=project_id,
        )
        == project_id
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "ocr"},
        {"display_id": "P-OCR"},
        {"project_id": str(uuid.uuid4())},
    ],
)
def test_media_seed_create_receipt_rejects_scope_mismatch(changes):
    project_id = uuid.uuid4()
    reply = _created(kind="video", display_id="P-VIDEO-DEV", project_id=project_id)
    reply.update(changes)

    with pytest.raises(MediaSeedProjectUnavailable):
        decode_media_seed_project_created(
            reply,
            kind="video",
            display_id="P-VIDEO-DEV",
            expected_project_id=project_id,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": MEDIA_SEED_PROJECT_PROTOCOL, "display_id": "P-VIDEO-DEV"},
        {
            "schema": MEDIA_SEED_PROJECT_PROTOCOL,
            "kind": "unknown",
            "display_id": "P-VIDEO-DEV",
            "project_id": str(uuid.uuid4()),
        },
        {
            "protocol": MEDIA_SEED_PROJECT_PROTOCOL,
            "kind": "video",
            "display_id": "P-VIDEO-DEV",
            "project_id": str(uuid.uuid4()),
        },
        {
            "schema": "aap-workspace-media-seed-project/v2",
            "kind": "video",
            "display_id": "P-VIDEO-DEV",
            "project_id": str(uuid.uuid4()),
        },
        {
            "schema": MEDIA_SEED_PROJECT_PROTOCOL,
            "kind": "video",
            "display_id": "P-VIDEO-DEV",
            "project_id": str(uuid.uuid4()),
            "extra": True,
        },
    ],
)
def test_media_seed_create_receipt_rejects_invalid_closed_payloads(payload):
    with pytest.raises(MediaSeedProjectUnavailable):
        decode_media_seed_project_created(
            payload,
            kind="video",
            display_id="P-VIDEO-DEV",
            expected_project_id=uuid.uuid4(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        (
            b'{"schema":"aap-workspace-media-seed-project/v1",'
            b'"kind":"video","kind":"video","display_id":"P-VIDEO-DEV",'
            b'"project_id":"00000000-0000-0000-0000-000000000001"}'
        ),
        (
            b'{"schema":"aap-workspace-media-seed-project/v1",'
            b'"kind":"video","display_id":"P-VIDEO-DEV",'
            b'"project_id":NaN}'
        ),
    ],
)
def test_media_seed_create_receipt_rejects_malformed_json(payload):
    with pytest.raises(MediaSeedProjectUnavailable):
        decode_media_seed_project_created(
            payload,
            kind="video",
            display_id="P-VIDEO-DEV",
            expected_project_id=uuid.uuid4(),
        )


def test_projection_receipt_distinguishes_written_and_missing_projects():
    project_id = uuid.uuid4()
    written = decode_seed_project_projection_write(
        _projection_write(
            project_id=project_id,
            found=True,
            updated_at="2026-10-23T07:40:12.345678Z",
        ),
        project_id=project_id,
    )
    missing = decode_seed_project_projection_write(
        json.dumps(
            _projection_write(project_id=project_id, found=False, updated_at=None)
        ),
        project_id=project_id,
    )

    assert written.found is True
    assert written.updated_at == datetime(2026, 10, 23, 7, 40, 12, 345678, tzinfo=UTC)
    assert missing.found is False
    assert missing.updated_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": str(uuid.uuid4())},
        {"found": False},
        {"updated_at": None},
        {"updated_at": "2026-10-23T07:40:12"},
        {"unexpected": True},
        {"schema": "aap-workspace-media-seed-project/v2"},
    ],
)
def test_projection_receipt_rejects_mismatched_or_invalid_fields(changes):
    project_id = uuid.uuid4()
    reply = _projection_write(
        project_id=project_id,
        found=True,
        updated_at="2026-10-23T07:40:12+00:00",
    )
    reply.update(changes)

    with pytest.raises(MediaSeedProjectUnavailable):
        decode_seed_project_projection_write(reply, project_id=project_id)


def test_media_seed_project_schema_freezes_native_signatures_and_projection_fields():
    schema = media_seed_project_schema()
    descriptor = schema["x-native-capabilities"]
    functions = descriptor["functions"]

    assert schema["$id"] == MEDIA_SEED_PROJECT_PROTOCOL
    assert descriptor["protocol"] == MEDIA_SEED_PROJECT_PROTOCOL
    assert descriptor["postgres_schema"] == "aap_workspace_v1"
    assert [function["signature"] for function in functions] == [
        "aap_workspace_v1.create_media_seed_project(uuid, text, text, uuid, text, jsonb)",
        "aap_workspace_v1.write_seed_project_projection(uuid, jsonb)",
    ]
    assert [
        [argument["name"] for argument in function["arguments"]]
        for function in functions
    ] == [
        [
            "p_project_id",
            "p_display_id",
            "p_name",
            "p_owner_id",
            "p_kind",
            "p_tool_bindings",
        ],
        ["p_project_id", "p_patch"],
    ]
    assert [function["result_model"] for function in functions] == [
        "MediaSeedProjectCreated",
        "SeedProjectProjectionWrite",
    ]
    assert descriptor["projection_fields"] == list(MEDIA_SEED_PROJECT_PROJECTION_FIELDS)
    patch_schema = functions[1]["arguments"][1]["json_schema"]
    assert patch_schema["type"] == "object"
    assert patch_schema["minProperties"] == 1
    assert patch_schema["additionalProperties"] is False
    assert list(patch_schema["properties"]) == list(
        MEDIA_SEED_PROJECT_PROJECTION_FIELDS
    )
    assert patch_schema["properties"]["ml_backend_pool_id"]["anyOf"] == [
        {"type": "string", "format": "uuid"},
        {"type": "null"},
    ]
    assert patch_schema["properties"]["preannotate_pipeline"]["anyOf"] == [
        {"type": "array", "items": {}},
        {"type": "null"},
    ]
    assert functions[0]["result_postgres_type"] == "jsonb"
    assert functions[1]["result_postgres_type"] == "jsonb"
    assert "not an upsert" in functions[0]["transaction_semantics"]
    assert "found=false" in functions[1]["transaction_semantics"]
    assert SeedProjectProjectionWrite.model_config["frozen"] is True


def test_checked_in_schemas_match_the_closed_contract():
    expected = media_seed_project_schema()
    workspace_schema = (
        Path(__file__).resolve().parents[1]
        / "src/aap_workspace/media_seed_project_protocol.schema.json"
    )
    migration_schema = (
        Path(__file__).resolve().parents[2]
        / "api/alembic/sql/0307_workspace_media_seed_project.schema.json"
    )

    assert json.loads(workspace_schema.read_text(encoding="utf-8")) == expected
    assert json.loads(migration_schema.read_text(encoding="utf-8")) == expected

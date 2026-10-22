from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import uuid

import pytest
from pydantic import ValidationError

from aap_audit_contract import ProjectDeletionAuditContext
from aap_workspace.dataset_unlink_contract import (
    DATASET_UNLINK_PREVIEW_PROTOCOL,
    DATASET_UNLINK_PROTOCOL,
    DatasetUnlinkCommand,
    DatasetUnlinkFailure,
    DatasetUnlinkPreview,
    DatasetUnlinkPreviewCommand,
    DatasetUnlinkReceipt,
    dataset_unlink_schema,
    decode_unlink_command,
    decode_unlink_preview_command,
    decode_unlink_reply,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_DATASET_ID = uuid.UUID("23b973f5-a1fa-4973-bde0-c61d36c93e8e")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_ASSOCIATION_ID = uuid.UUID("c71a1997-c3c5-457c-8cf3-53be3f7cf207")
_BATCH_ID = uuid.UUID("5d49ffae-67f8-4893-8c75-86df553cc772")
_CLEANUP_BUCKET = "media-cache-test"


def _command(**changes):
    return {
        "operation_id": str(_OPERATION_ID),
        "actor_id": str(_ACTOR_ID),
        "dataset_id": str(_DATASET_ID),
        "project_id": str(_PROJECT_ID),
        "cleanup_bucket": _CLEANUP_BUCKET,
        **changes,
    }


def _preview_command(**changes):
    return {
        "actor_id": str(_ACTOR_ID),
        "dataset_id": str(_DATASET_ID),
        "project_id": str(_PROJECT_ID),
        **changes,
    }


def _receipt(**changes):
    return {
        "operation_id": str(_OPERATION_ID),
        "actor_id": str(_ACTOR_ID),
        "dataset_id": str(_DATASET_ID),
        "project_id": str(_PROJECT_ID),
        "association_id": str(_ASSOCIATION_ID),
        "cleanup_bucket": _CLEANUP_BUCKET,
        "audit_id": 731,
        "deleted_tasks": 4,
        "deleted_annotations": 3,
        "deleted_batches": 1,
        "deleted_batch_ids": [str(_BATCH_ID)],
        "soft": False,
        **changes,
    }


def _unlink_reply(data=None):
    return {
        "schema": DATASET_UNLINK_PROTOCOL,
        "ok": True,
        "data": _receipt() if data is None else data,
    }


def _preview_reply(data=None):
    return {
        "schema": DATASET_UNLINK_PREVIEW_PROTOCOL,
        "ok": True,
        "data": (
            {
                "will_delete_tasks": 2,
                "will_delete_annotations": 1,
                "will_delete_batches": 1,
            }
            if data is None
            else data
        ),
    }


@pytest.mark.parametrize("kind", ["command", "reply"])
def test_python_uuid_values_match_original_contract(kind):
    if kind == "command":
        payload = _command(actor_id=_ACTOR_ID, project_id=_PROJECT_ID)
        expected = DatasetUnlinkCommand.model_validate(payload)
        assert decode_unlink_command(payload) == expected
        return

    from aap_workspace import dataset_unlink_contract as contract

    base = "17992f64cd97bdd1af322066d5d41c0bdae9ec99"
    source = subprocess.check_output(
        ["git", "show", base + ":apps/api/app/services/dataset_unlink_contract.py"],
        cwd=Path(__file__).resolve().parents[3],
        text=True,
    )
    original = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "decode_unlink_reply"
    )
    namespace = dict(vars(contract))
    exec(
        compile(
            ast.Module(body=[original], type_ignores=[]),
            base + ":dataset_unlink_contract.py",
            "exec",
        ),
        namespace,
    )
    payload = _unlink_reply(
        _receipt(
            actor_id=_ACTOR_ID,
            association_id=_ASSOCIATION_ID,
            deleted_batch_ids=(_BATCH_ID,),
        )
    )
    expected = namespace["decode_unlink_reply"](payload)
    assert decode_unlink_reply(payload) == expected


def test_moved_models_preserve_the_frozen_native_schema_and_defaults():
    frozen = json.loads(
        (
            Path(__file__).parents[2]
            / "api"
            / "alembic"
            / "sql"
            / "0205_dataset_unlink.schema.json"
        ).read_text(encoding="utf-8")
    )["$defs"]

    command_schema = DatasetUnlinkCommand.model_json_schema()
    audit_context_schema = command_schema.pop("$defs")["ProjectDeletionAuditContext"]
    assert audit_context_schema == frozen["ProjectDeletionAuditContext"]
    assert command_schema == frozen["command"]
    assert DatasetUnlinkReceipt.model_json_schema() == frozen["receipt"]
    assert DatasetUnlinkPreview.model_json_schema() == frozen["previewResult"]

    preview_command_schema = DatasetUnlinkPreviewCommand.model_json_schema()
    preview_command_schema.pop("title")
    for field in preview_command_schema["properties"].values():
        field.pop("title")
    assert preview_command_schema == frozen["previewCommand"]

    command = decode_unlink_command(_command())
    assert command.audit_context is None
    assert command.model_dump(mode="json")["audit_context"] is None
    assert "audit_context" not in command.model_fields_set
    explicit_null = decode_unlink_command(_command(audit_context=None))
    assert explicit_null.audit_context is None
    assert "audit_context" in explicit_null.model_fields_set


def test_command_decoders_revalidate_models_and_accept_uuid_text():
    context = ProjectDeletionAuditContext(
        method="DELETE",
        path=f"/api/v1/datasets/{_DATASET_ID}/link/{_PROJECT_ID}",
        ip="192.0.2.8",
        request_id="request-42",
    )
    command = decode_unlink_command(
        _command(audit_context=context.model_dump(mode="json"))
    )
    preview = decode_unlink_preview_command(_preview_command())
    assert command.operation_id == _OPERATION_ID
    assert command.audit_context == context
    assert preview.actor_id == _ACTOR_ID
    assert preview.dataset_id == _DATASET_ID
    assert preview.project_id == _PROJECT_ID

    forged = command.model_copy(update={"cleanup_bucket": "bad bucket"})
    with pytest.raises(ValidationError):
        decode_unlink_command(forged)
    with pytest.raises(ValidationError):
        decode_unlink_command({**_command(), "private": "field"})
    with pytest.raises(ValidationError):
        decode_unlink_preview_command({**_preview_command(), "private": "field"})


@pytest.mark.parametrize(
    "payload",
    [
        b'{"operation_id":"x","operation_id":"y"}',
        b'{"operation_id":"x","value":NaN}',
        b'{"operation_id":"x","value":1e9999}',
    ],
)
def test_command_decoders_reject_ambiguous_or_non_finite_raw_json(payload):
    with pytest.raises(ValueError):
        decode_unlink_command(payload)
    with pytest.raises(ValueError):
        decode_unlink_preview_command(payload)


def test_receipt_reply_keeps_frozen_public_result_and_open_failure_codes():
    receipt = decode_unlink_reply(json.dumps(_unlink_reply()))
    assert isinstance(receipt, DatasetUnlinkReceipt)
    assert receipt.deleted_batch_ids == (_BATCH_ID,)
    assert receipt.public_result() == {
        "deleted_tasks": 4,
        "deleted_annotations": 3,
        "deleted_batches": 1,
        "deleted_batch_ids": [str(_BATCH_ID)],
        "soft": False,
    }

    with pytest.raises(DatasetUnlinkFailure) as raised:
        decode_unlink_reply(
            {
                "schema": DATASET_UNLINK_PROTOCOL,
                "ok": False,
                "error": {"code": "retained_video_execution_record"},
            }
        )
    assert raised.value.code == "retained_video_execution_record"


def test_preview_reply_uses_its_version_and_strict_nonnegative_counts():
    preview = decode_unlink_reply(_preview_reply(), preview=True)
    assert isinstance(preview, DatasetUnlinkPreview)
    assert preview.will_delete_batches == 1
    with pytest.raises(ValueError, match="Unexpected Dataset unlink contract"):
        decode_unlink_reply(_preview_reply(), preview=False)
    with pytest.raises(ValidationError):
        decode_unlink_reply(
            _preview_reply(
                {
                    "will_delete_tasks": True,
                    "will_delete_annotations": 0,
                    "will_delete_batches": 0,
                }
            ),
            preview=True,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"aap-dataset-unlink/v1","schema":"other"}',
        b'{"schema":"aap-dataset-unlink/v1","ok":true,"bad":NaN}',
        b'{"schema":"aap-dataset-unlink/v1","ok":true,"bad":1e9999}',
    ],
)
def test_reply_decoder_rejects_duplicate_and_non_finite_json(payload):
    with pytest.raises(ValueError):
        decode_unlink_reply(payload)


def test_invalid_receipt_counts_soft_flag_and_batch_identity_set_are_rejected():
    for changes in (
        {"deleted_tasks": True},
        {"deleted_annotations": -1},
        {"soft": True},
        {"deleted_batches": 2},
        {"deleted_batch_ids": [str(_BATCH_ID), str(_BATCH_ID)]},
    ):
        with pytest.raises(ValidationError):
            decode_unlink_reply(_unlink_reply(_receipt(**changes)))


def test_portable_descriptor_contains_only_the_existing_native_capabilities():
    schema = dataset_unlink_schema()
    descriptor = schema["x-native-capabilities"]
    assert schema["$id"] == "urn:aap:contract:dataset-unlink:v1"
    assert set(descriptor["protocols"]) == {
        DATASET_UNLINK_PROTOCOL,
        DATASET_UNLINK_PREVIEW_PROTOCOL,
    }
    assert [entry["name"] for entry in descriptor["functions"]] == [
        "unlink_dataset",
        "preview_dataset_unlink",
    ]
    assert schema["x-postgresql-functions"] == [
        "aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)",
        "aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid)",
    ]
    assert descriptor["functions"][0]["arguments"][-1]["nullable"] is True
    assert descriptor["functions"][1]["volatility"] == "VOLATILE"
    assert "not use a read-only" in descriptor["functions"][1]["transaction_semantics"]

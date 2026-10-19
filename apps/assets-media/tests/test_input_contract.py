import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_inputs import contract as c
from aap_assets_inputs_runtime.database import (
    prepare_native_call,
    validate_native_result,
)
from input_fixtures import input_reference, input_request, input_work


@pytest.mark.parametrize("version", ["", " ", "null"])
def test_input_blob_requires_real_version(version):
    blob = input_work().blob.model_dump()
    blob["version_id"] = version
    with pytest.raises(ValidationError):
        c.InputBlob.model_validate(blob)


@pytest.mark.parametrize(
    "query",
    [
        "",
        "versionId=other",
        "versionId=null",
        "versionId=version-a&versionId=version-b",
    ],
)
def test_reference_requires_exact_version(query):
    work = input_work()
    value = input_reference(work).model_dump()
    value["url"] = "http://s3.test/input?" + query
    with pytest.raises(ValidationError):
        c.InputReference.model_validate(value)


@pytest.mark.parametrize("change", ["source", "scope", "version", "measured_bytes"])
def test_private_work_rejects_mixed_identity(change):
    value = input_work().model_dump(mode="json")
    if change == "source":
        value["source"]["source_snapshot"] = "c" * 64
    elif change == "scope":
        value["source"]["task_id"] = str(uuid4())
    elif change == "version":
        value["blob"]["version_id"] = "null"
    else:
        value["blob"]["sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        c.InputWork.model_validate(value)


def test_consumer_metadata_does_not_disclose_source_key():
    value = c.json_bytes(input_work().input).decode()
    assert "private/original.jpg" not in value
    assert "file_path" not in value


def test_native_capability_roles_and_closed_names():
    request = c.ClaimInputRequest(worker_id=uuid4())
    with pytest.raises(PermissionError):
        prepare_native_call(
            "claim_accepted_image_input", request, allowed_roles={"control"}
        )
    with pytest.raises(TypeError):
        prepare_native_call(
            "claim_accepted_image_input; SELECT 1", request, allowed_roles={"worker"}
        )
    sql, arguments = prepare_native_call(
        "claim_accepted_image_input", request, allowed_roles={"worker"}
    )
    assert (
        sql
        == "SELECT aap_assets_v1.claim_accepted_image_input($1::uuid, $2::uuid, $3::integer)"
    )
    assert arguments == (request.worker_id, None, 2100)


def test_native_reply_cannot_cross_input_scope():
    work = input_work()
    request = input_request(work).model_copy(update={"input_id": uuid4()})
    with pytest.raises(c.InputFailure, match="invalid_response"):
        validate_native_result(
            "read_accepted_image_input",
            request,
            {
                "schema": c.INPUT_SCHEMA,
                "ok": True,
                "data": work.input.model_dump(mode="json"),
            },
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"schema": c.INPUT_SCHEMA, "ok": False, "error": {"code": "unknown"}},
        {"schema": c.INPUT_SCHEMA, "ok": True, "data": {}, "error": {}},
        {"schema": "other/v1", "ok": False, "error": {"code": "source_changed"}},
        '{"schema":"aap-assets-inputs/v1","ok":true,"data":NaN}',
    ],
)
def test_malformed_envelopes_fail_closed(raw):
    with pytest.raises(c.InputFailure, match="invalid_response"):
        c.decode_reply(raw, c.InputRecord)


def test_contract_is_json_safe_and_covers_native_operations():
    document = c.contract_document()
    assert document["$id"] == c.INPUT_SCHEMA
    assert set(document["x-native"]) == set(c.OPERATIONS)
    assert json.loads(json.dumps(document)) == document


def test_frozen_artifacts_and_public_http_scope():
    from pathlib import Path

    from aap_assets_inputs.http_contract import http_document

    package = Path(c.__file__).parent
    assert (
        json.loads((package / "protocol.schema.json").read_text())
        == c.contract_document()
    )
    document = http_document()
    assert json.loads((package / "openapi.snapshot.json").read_text()) == document
    assert set(document["paths"]) == {
        "/v1/inputs/" + name
        for name in ("find", "accept", "record", "reference", "verify-source")
    }
    assert "file_path" not in json.dumps(document)

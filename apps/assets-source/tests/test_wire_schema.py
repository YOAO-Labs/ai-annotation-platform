"""The distributable schema describes the actual three source operations."""

from importlib.resources import files
import json

from aap_assets_source.contract import (
    SourceDeletionRequest,
    SourceFailureCode,
    UploadCompleteRequest,
    UploadInitRequest,
)
from aap_assets_source.upload_contract import DatasetUploadDiscardedResult
from pydantic import ValidationError
import pytest


@pytest.mark.parametrize("payload", [{"duplicate_of": None}, {"reason": None}])
def test_native_discard_disposition_requires_a_non_null_value(payload):
    with pytest.raises(ValidationError):
        DatasetUploadDiscardedResult.model_validate(payload)


def test_distributable_wire_schema_freezes_paths_fields_and_error_codes():
    schema = json.loads(
        files("aap_assets_source").joinpath("protocol.schema.json").read_text()
    )
    assert schema["$id"] == "urn:aap:assets-source:v1"
    definitions = schema["$defs"]
    for operation, model in (
        ("upload-init", UploadInitRequest),
        ("upload-complete", UploadCompleteRequest),
        ("delete", SourceDeletionRequest),
    ):
        call = definitions[model.__name__ + "Call"]["properties"]
        assert call["path"]["const"] == f"/v1/source/{operation}"
        expected = model.model_json_schema(ref_template="#/$defs/{model}")
        expected.pop("$defs", None)
        assert definitions[model.__name__] == expected
        assert set(definitions[model.__name__]["properties"]) == set(model.model_fields)
        assert definitions[model.__name__]["additionalProperties"] is False
    assert definitions["failure"]["properties"]["error"]["properties"]["code"][
        "enum"
    ] == list(SourceFailureCode.__args__)
    assert definitions["UploadOutcome"]["oneOf"][1]["properties"]["result"] == {
        "type": "null"
    }

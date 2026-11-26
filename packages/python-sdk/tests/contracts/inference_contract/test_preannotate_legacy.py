"""Retain captured pipeline behavior across the provider-to-contract move."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from yoao_inference_contract.pipeline import (
    PipelineValidationError,
    validate_saved_pipeline,
)
from yoao_inference_contract.preannotate import PipelineStage, PreannotateRequest


LEGACY = json.loads(
    (Path(__file__).parent / "fixtures/preannotate_legacy.json").read_text()
)


@pytest.mark.parametrize("case", LEGACY["cases"], ids=lambda case: case["name"])
def test_saved_pipeline_accepts_or_rejects_legacy_vectors(case):
    if case["expected"]["ok"]:
        assert (
            validate_saved_pipeline(copy.deepcopy(case["input"]))
            == case["expected"]["value"]
        )
    else:
        with pytest.raises(PipelineValidationError):
            validate_saved_pipeline(copy.deepcopy(case["input"]))


def test_shared_model_fields_and_request_materialization_match_legacy():
    actual = {
        "PipelineStage": PipelineStage.model_json_schema(),
        "PreannotateRequest": PreannotateRequest.model_json_schema(),
    }
    expected = copy.deepcopy(LEGACY["schemas"])
    for schemas in (actual, expected):
        schemas["PipelineStage"].pop("description")
        schemas["PreannotateRequest"]["$defs"]["PipelineStage"].pop("description")
    assert actual == expected
    example = LEGACY["request_example"]
    assert (
        PreannotateRequest.model_validate(example).model_dump(
            mode="json", exclude_unset=True
        )
        == example
    )


def test_published_request_schema_matches_current_contract():
    from yoao_inference_contract import preannotate

    artifact = Path(preannotate.__file__).with_name("preannotate.schema.json")
    assert json.loads(artifact.read_text()) == PreannotateRequest.model_json_schema()

"""Retain captured pipeline behavior across the provider-to-contract move."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aap_inference_contract.pipeline import validate_saved_pipeline
from aap_inference_contract.preannotate import PipelineStage, PreannotateRequest


LEGACY = json.loads(
    (Path(__file__).parent / "fixtures/preannotate_legacy.json").read_text()
)


@pytest.mark.parametrize("case", LEGACY["cases"], ids=lambda case: case["name"])
def test_saved_pipeline_retains_captured_result_or_exact_error(case):
    try:
        outcome = {
            "ok": True,
            "value": validate_saved_pipeline(copy.deepcopy(case["input"])),
        }
    except Exception as error:
        outcome = {
            "ok": False,
            "exception": type(error).__name__,
            "detail": str(error),
        }
    assert json.dumps(outcome, sort_keys=True) == json.dumps(
        case["expected"], sort_keys=True
    )


def test_shared_model_fields_and_request_materialization_match_legacy():
    actual = {
        "PipelineStage": PipelineStage.model_json_schema(),
        "PreannotateRequest": PreannotateRequest.model_json_schema(),
    }
    expected = copy.deepcopy(LEGACY["schemas"])
    # The current description replaces historical version/stage-zero prose.
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
    from aap_inference_contract import preannotate

    artifact = Path(preannotate.__file__).with_name("preannotate.schema.json")
    assert json.loads(artifact.read_text()) == PreannotateRequest.model_json_schema()

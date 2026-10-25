"""Replay the frozen class-usage HTTP contract through each current provider."""

from __future__ import annotations

import os

import pytest

from tests.labeling_class_query_helpers import (
    add_frozen_interleave_row,
    bound_api_client,
    cleanup_world,
    fixture_data,
    request_sample,
    seed_legacy_world,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _expected_sample(sample: dict) -> dict:
    response = sample["response"]
    return {
        "label": sample["label"],
        "request": {
            "method": sample["request"]["method"],
            "path": sample["request"]["path"],
            "actor_alias": sample["request"]["actor"]["alias"],
        },
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": response["body"],
        },
    }


def _semantic_sample(sample: dict) -> dict:
    return {
        "label": sample["label"],
        "request": sample["request"],
        "response": sample["response"],
    }


def _assert_borrowed_native_phases(sample: dict) -> None:
    phases = sample.get("borrowed_labeling_rpc_phases", [])
    body = sample["response"]["body"]
    if sample["response"]["status"] == 200 and isinstance(body, dict):
        assert phases.count("class_query_policy") == 1, sample["label"]
        assert phases.count("class_counts") == 1, sample["label"]
        assert phases.count("attribute_rows") == 1, sample["label"]
        assert phases.index("class_counts") < phases.index("attribute_rows")
    elif sample["label"] == "large_json_number_attribute_read":
        assert phases.count("class_counts") == 1
        assert phases.count("attribute_rows") == 1
        assert phases.index("class_counts") < phases.index("attribute_rows")


@pytest.mark.parametrize("binding", ["borrowed", "dedicated", "http"])
async def test_current_labeling_query_owner_matches_all_frozen_http_samples(
    test_engine, app_module, monkeypatch, tmp_path, binding
):
    reference = fixture_data()
    world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
    try:
        async with bound_api_client(
            test_engine,
            app_module,
            monkeypatch,
            binding=binding,
            workspace_token="test-workspace-query-control",
            labeling_token="test-labeling-class-query-control",
        ) as (client, _runtime_engine, _native_rpc):
            actual = []
            for frozen in reference["samples"]:
                if frozen["label"] == "class_then_attributes_second_session_commit":
                    # This row recreates only that frozen response for semantic
                    # checks. Remote providers are not treated as phase evidence.
                    await add_frozen_interleave_row(test_engine, world)
                sample = await request_sample(
                    client, world, frozen, native_rpc=_native_rpc
                )
                actual.append(sample)
                if binding == "borrowed":
                    _assert_borrowed_native_phases(sample)

            expected = [_expected_sample(sample) for sample in reference["samples"]]
            assert [_semantic_sample(sample) for sample in actual] == expected
    finally:
        await cleanup_world(test_engine, world)

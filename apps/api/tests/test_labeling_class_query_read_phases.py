"""Exercise the real class/attribute read gap with a committed second session."""

from __future__ import annotations

import os

import pytest

from tests.labeling_class_query_helpers import (
    bound_api_client,
    cleanup_world,
    fixture_data,
    gated_class_counts,
    run_read_committed_phase_probe,
    seed_legacy_world,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("binding", ["borrowed", "dedicated", "http"])
async def test_committed_annotation_between_real_labeling_reads_is_visible_to_attributes(
    test_engine, app_module, monkeypatch, tmp_path, binding
):
    reference = fixture_data()
    baseline = next(
        sample
        for sample in reference["samples"]
        if sample["label"] == "target_project_owner_aggregation"
    )
    world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
    try:
        async with bound_api_client(
            test_engine,
            app_module,
            monkeypatch,
            binding=binding,
            workspace_token="test-workspace-phase-control",
            labeling_token="test-labeling-phase-control",
            application_name="labeling-class-query-phase",
        ) as (client, _runtime_engine, _native_rpc):
            async with gated_class_counts(test_engine):
                result = await run_read_committed_phase_probe(
                    test_engine, client, world
                )

        assert result["status"] == 200
        assert result["headers"]["content-type"] == "application/json"
        assert result["body"]["classes"] == baseline["response"]["body"]["classes"]
        expected_attributes = dict(baseline["response"]["body"]["attributes"])
        expected_attributes["phase_committed_key"] = 1
        assert result["body"]["attributes"] == expected_attributes
        assert "phase-new-class" not in result["body"]["classes"]
        assert (
            "aap_labeling_class_query_v1.class_counts"
            in result["blocked_rpc"]["query"].casefold()
        )
        assert result["blocked_rpc"]["wait_event_type"] == "Lock"
    finally:
        await cleanup_world(test_engine, world)

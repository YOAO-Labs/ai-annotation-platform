"""Replay the frozen named-pipeline oracle through all three owner bindings."""

from __future__ import annotations

import os

import pytest

from tests.inference_pipeline_owner_helpers import (
    _BINDINGS,
    api_pipeline_owner_binding,
)
from tests.test_named_pipeline_legacy import (
    test_replay_legacy_named_pipeline_http_and_database as _replay_legacy_samples,
)


@pytest.mark.parametrize("mode", _BINDINGS)
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires explicit owned module-data role acceptance",
)
async def test_inference_pipeline_bindings_replay_legacy_http_and_database(
    mode, test_engine, app_module, monkeypatch, tmp_path
):
    async with api_pipeline_owner_binding(
        test_engine,
        monkeypatch,
        mode,
        tmp_path,
    ):
        await _replay_legacy_samples(test_engine, app_module, monkeypatch)

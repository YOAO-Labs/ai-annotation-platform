from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest


def test_empty_prediction_is_valid_but_missing_target_result_is_not():
    from app.application.durable_prediction_retry import _one_result

    task_id = uuid.uuid4()
    empty = SimpleNamespace(
        task_id=str(task_id),
        result=[],
        score=0,
        model_version=None,
        inference_time_ms=0,
        meta=None,
    )
    assert _one_result([empty], task_id) is empty
    with pytest.raises(ValueError):
        _one_result([], task_id)

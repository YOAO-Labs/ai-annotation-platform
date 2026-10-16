import pytest

from aap_protocol_v2.predict import normalize_context_task


@pytest.mark.parametrize(
    "context,expected",
    [
        ({"task": "detect"}, {"task": "detect"}),
        ({"type": "detection"}, {"task": "detect"}),
        ({"task": "detect", "type": "detection"}, {"task": "detect"}),
        (
            {"task": "segment", "type": "text", "text": "car"},
            {"task": "segment", "type": "text", "text": "car"},
        ),
        ({"task_type": "text"}, {"type": "text"}),
        ({"type": "video_tracker"}, {"type": "video_tracker"}),
    ],
)
def test_task_and_prompt_modes(context, expected):
    original = dict(context)
    assert normalize_context_task(context) == expected
    assert context == original


@pytest.mark.parametrize(
    "context",
    [
        {"task": "segment", "type": "detection"},
        {"task": "detect", "task_type": "segmentation"},
        {"task": "aap"},
        {"type": "point", "prompt_type": "text"},
    ],
)
def test_conflicts_and_formats_are_rejected(context):
    with pytest.raises(ValueError):
        normalize_context_task(context)


def test_explicit_model_resolves_task_and_rejects_contradiction():
    assert (
        normalize_context_task({"model_id": "opaque"}, model_task="classification")[
            "task"
        ]
        == "classify"
    )
    with pytest.raises(ValueError):
        normalize_context_task({"task": "detect"}, model_task="segment")

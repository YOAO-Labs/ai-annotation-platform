"""Pure checks for the pending/in-progress annotation-count transitions."""

import pytest

from aap_labeling.annotation_statistics_contract import (
    annotation_status_after_recompute,
)


@pytest.mark.parametrize(
    ("status", "total_annotations", "expected"),
    [
        ("pending", 1, "in_progress"),
        ("in_progress", 0, "pending"),
        ("pending", 0, "pending"),
        ("in_progress", 1, "in_progress"),
        ("review", 0, "review"),
        ("completed", 1, "completed"),
    ],
)
def test_recompute_only_flips_pending_and_in_progress(
    status, total_annotations, expected
):
    assert annotation_status_after_recompute(status, total_annotations) == expected

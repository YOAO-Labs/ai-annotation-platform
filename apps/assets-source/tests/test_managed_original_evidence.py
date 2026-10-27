"""Observed current-head evidence for owner verification payloads."""

from __future__ import annotations

from aap_assets_source.managed_original_contract import (
    observed_current_head_match,
)


def test_current_head_flag_requires_an_observed_version_pinned_match():
    assert (
        observed_current_head_match(
            {"version_id": "v1", "observation": {"version_id": "v1", "size": 3}},
            size_bytes=3,
        )
        is True
    )
    assert (
        observed_current_head_match(
            {"version_id": "v1", "observation": {"version_id": "v2", "size": 3}},
            size_bytes=3,
        )
        is False
    )
    assert (
        observed_current_head_match(
            {"version_id": "v1", "observation": {"version_id": "v1", "size": 4}},
            size_bytes=3,
        )
        is False
    )
    # Unversioned, missing observation and absent evidence never claim a match.
    assert (
        observed_current_head_match(
            {"version_id": None, "observation": {"version_id": None, "size": 3}},
            size_bytes=3,
        )
        is None
    )
    assert observed_current_head_match({"version_id": "v1"}, size_bytes=3) is None
    assert observed_current_head_match(None, size_bytes=3) is None

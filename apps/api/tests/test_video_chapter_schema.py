"""视频章节 frame_step / source 元数据互转 + 网格对齐 helper 单测。

frame_step / source 不新增数据库列, 而是约定存进 chapter_metadata (JSONB)。
schema 层做强类型互转, 旧章节缺键时退化为 frame_step=None / source="manual"。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.video_chapter import (
    VideoChapterBase,
    VideoChapterOut,
    merge_chapter_metadata,
    snap_chapter_to_grid,
)


def _out(metadata: dict, **overrides) -> VideoChapterOut:
    payload = {
        "id": uuid.uuid4(),
        "dataset_item_id": uuid.uuid4(),
        "start_frame": 0,
        "end_frame": 10,
        "title": "ch",
        "metadata": metadata,
        "created_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    return VideoChapterOut.model_validate(payload)


def test_base_frame_step_optional_defaults_none():
    base = VideoChapterBase(start_frame=0, end_frame=5, title="ch")
    assert base.frame_step is None
    assert base.source is None


def test_base_frame_step_rejects_zero():
    with pytest.raises(ValidationError):
        VideoChapterBase(start_frame=0, end_frame=5, title="ch", frame_step=0)


def test_base_frame_step_accepts_one():
    base = VideoChapterBase(start_frame=0, end_frame=5, title="ch", frame_step=1)
    assert base.frame_step == 1


def test_base_source_rejects_unknown():
    with pytest.raises(ValidationError):
        VideoChapterBase(start_frame=0, end_frame=5, title="ch", source="auto")


def test_out_legacy_chapter_without_keys_defaults():
    out = _out({})
    assert out.frame_step is None
    assert out.source == "manual"


def test_out_derives_frame_step_and_source_from_metadata():
    out = _out({"frame_step": 5, "source": "sampled"})
    assert out.frame_step == 5
    assert out.source == "sampled"


def test_out_ignores_invalid_frame_step_in_metadata():
    out = _out({"frame_step": 0})
    assert out.frame_step is None


def test_out_ignores_unknown_source_in_metadata():
    out = _out({"source": "bogus"})
    assert out.source == "manual"


def test_merge_writes_keys_into_empty_base():
    merged = merge_chapter_metadata(None, frame_step=3, source="sampled")
    assert merged == {"frame_step": 3, "source": "sampled"}


def test_merge_preserves_unrelated_keys():
    merged = merge_chapter_metadata({"note": "x"}, frame_step=2, source="manual")
    assert merged == {"note": "x", "frame_step": 2, "source": "manual"}


def test_merge_none_fields_leave_base_untouched():
    merged = merge_chapter_metadata(
        {"frame_step": 9, "source": "sampled"}, frame_step=None, source=None
    )
    assert merged == {"frame_step": 9, "source": "sampled"}


def test_merge_does_not_mutate_base():
    base = {"frame_step": 1}
    merge_chapter_metadata(base, frame_step=4, source=None)
    assert base == {"frame_step": 1}


def test_snap_step_one_is_identity():
    assert snap_chapter_to_grid(3, 17, 1) == (3, 17)


def test_snap_step_clamped_to_one_is_identity():
    assert snap_chapter_to_grid(3, 17, 0) == (3, 17)


def test_snap_start_floors_end_floors():
    assert snap_chapter_to_grid(7, 17, 5) == (5, 15)


def test_snap_exact_grid_points_unchanged():
    assert snap_chapter_to_grid(10, 20, 5) == (10, 20)


def test_snap_end_below_step_collapses_to_start():
    assert snap_chapter_to_grid(0, 3, 5) == (0, 0)


def test_snap_keeps_start_le_end():
    s, e = snap_chapter_to_grid(8, 9, 5)
    assert s <= e

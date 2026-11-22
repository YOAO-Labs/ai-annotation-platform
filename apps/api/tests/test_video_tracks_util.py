"""D-2.1a derive_track_number 确定性派生单测。
polygon track 弧长参数化插值单测。"""

from app.services.video_tracks import (
    derive_track_number,
    lerp_polygon,
    lerp_polyline,
    resolve_track_at_frame,
    resolved_track_frames,
    _resample_closed_polygon,
    _resample_open_polyline,
)


def _track(track_id: str, first_frame: int) -> dict:
    return {
        "type": "video_track_bbox",
        "track_id": track_id,
        "keyframes": [
            {"frame_index": first_frame, "bbox": {"x": 0, "y": 0, "w": 1, "h": 1}},
            {"frame_index": first_frame + 5, "bbox": {"x": 0, "y": 0, "w": 1, "h": 1}},
        ],
    }


def test_derive_track_number_orders_by_first_keyframe():
    tracks = [
        ("a", _track("trk_a", 10)),
        ("b", _track("trk_b", 0)),
        ("c", _track("trk_c", 5)),
    ]
    assert derive_track_number(tracks) == {"b": 1, "c": 2, "a": 3}


def test_derive_track_number_breaks_ties_by_track_id():
    tracks = [
        ("z", _track("trk_z", 0)),
        ("a", _track("trk_a", 0)),
    ]
    assert derive_track_number(tracks) == {"a": 1, "z": 2}


def test_derive_track_number_empty():
    assert derive_track_number([]) == {}


def test_derive_track_number_unsorted_keyframes_use_min_frame():
    geometry = {
        "type": "video_track_bbox",
        "track_id": "trk_x",
        "keyframes": [
            {"frame_index": 8, "bbox": {"x": 0, "y": 0, "w": 1, "h": 1}},
            {"frame_index": 3, "bbox": {"x": 0, "y": 0, "w": 1, "h": 1}},
        ],
    }
    other = _track("trk_y", 5)
    assert derive_track_number([("x", geometry), ("y", other)]) == {"x": 1, "y": 2}


_SQUARE_A = [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2]]
_SQUARE_B = [[0.4, 0.0], [0.6, 0.0], [0.6, 0.2], [0.4, 0.2]]


def _polygon_track(keyframes: list[dict]) -> dict:
    return {"type": "video_track_polygon", "track_id": "p1", "keyframes": keyframes}


def test_resample_closed_polygon_square_to_4_returns_vertices():
    out = _resample_closed_polygon(_SQUARE_A, 4)
    assert out == _SQUARE_A


def test_resample_closed_polygon_degenerate():
    assert _resample_closed_polygon([[0.1, 0.1]], 4) == [[0.1, 0.1]]
    assert _resample_closed_polygon([[0.1, 0.1], [0.1, 0.1]], 3) == [
        [0.1, 0.1],
        [0.1, 0.1],
        [0.1, 0.1],
    ]


def test_lerp_polygon_equal_vertices_midpoint():
    out = lerp_polygon({"points": _SQUARE_A}, {"points": _SQUARE_B}, 0.5)
    assert out == [[0.2, 0.0], [0.4, 0.0], [0.4, 0.2], [0.2, 0.2]]


def test_lerp_polygon_endpoints():
    assert lerp_polygon({"points": _SQUARE_A}, {"points": _SQUARE_B}, 0.0) == _SQUARE_A
    assert lerp_polygon({"points": _SQUARE_A}, {"points": _SQUARE_B}, 1.0) == _SQUARE_B


def test_lerp_polygon_unequal_vertex_count_resamples():
    tri = [[0.0, 0.0], [0.4, 0.0], [0.2, 0.4]]
    out = lerp_polygon({"points": tri}, {"points": _SQUARE_B}, 0.5)
    assert len(out) == 4


def test_resolve_polygon_track_exact_and_interpolated():
    geom = _polygon_track(
        [
            {"frame_index": 0, "points": _SQUARE_A},
            {"frame_index": 10, "points": _SQUARE_B},
        ]
    )
    exact = resolve_track_at_frame(geom, 0)
    assert exact is not None and exact["points"] == _SQUARE_A
    assert "bbox" not in exact
    mid = resolve_track_at_frame(geom, 5)
    assert mid is not None and mid["source"] == "interpolated"
    assert mid["points"] == [[0.2, 0.0], [0.4, 0.0], [0.4, 0.2], [0.2, 0.2]]


def test_resolved_track_frames_polygon_keyframes_mode():
    geom = _polygon_track(
        [
            {"frame_index": 0, "points": _SQUARE_A},
            {"frame_index": 10, "points": _SQUARE_B},
        ]
    )
    rows = resolved_track_frames(geom, frame_mode="keyframes")
    assert [r["frame_index"] for r in rows] == [0, 10]
    assert all("points" in r and "bbox" not in r for r in rows)


_LINE_A = [[0.0, 0.0], [0.2, 0.0], [0.4, 0.0]]
_LINE_B = [[0.0, 0.2], [0.2, 0.2], [0.4, 0.2]]


def _polyline_track(keyframes: list[dict]) -> dict:
    return {"type": "video_track_polyline", "track_id": "l1", "keyframes": keyframes}


def test_resample_open_polyline_keeps_endpoints():
    assert _resample_open_polyline(_LINE_A, 3) == _LINE_A
    out = _resample_open_polyline([[0.0, 0.0], [0.4, 0.0]], 5)
    assert out[0] == [0.0, 0.0]
    assert out[-1] == [0.4, 0.0]
    assert len(out) == 5


def test_lerp_polyline_midpoint_open():
    out = lerp_polyline({"points": _LINE_A}, {"points": _LINE_B}, 0.5)
    assert out == [[0.0, 0.1], [0.2, 0.1], [0.4, 0.1]]


def test_lerp_polyline_unequal_counts_resample():
    short = [[0.0, 0.0], [0.4, 0.0]]
    out = lerp_polyline({"points": short}, {"points": _LINE_B}, 0.5)
    assert len(out) == 3


def test_resolve_polyline_track_exact_and_interpolated():
    geom = _polyline_track(
        [
            {"frame_index": 0, "points": _LINE_A},
            {"frame_index": 10, "points": _LINE_B},
        ]
    )
    exact = resolve_track_at_frame(geom, 0)
    assert exact is not None and exact["points"] == _LINE_A and "bbox" not in exact
    mid = resolve_track_at_frame(geom, 5)
    assert mid is not None and mid["source"] == "interpolated"
    assert mid["points"] == [[0.0, 0.1], [0.2, 0.1], [0.4, 0.1]]


def test_polyline_uses_open_resample_not_closed():
    tri = [[0.0, 0.0], [0.4, 0.0], [0.2, 0.4]]
    quad = [[0.0, 0.2], [0.4, 0.2], [0.4, 0.5], [0.0, 0.5]]
    line = lerp_polyline({"points": tri}, {"points": quad}, 0.5)
    poly = lerp_polygon({"points": tri}, {"points": quad}, 0.5)
    assert line != poly


def test_bbox_track_resolve_unchanged_regression():
    geom = {
        "type": "video_track_bbox",
        "track_id": "b1",
        "keyframes": [
            {"frame_index": 0, "bbox": {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2}},
            {"frame_index": 10, "bbox": {"x": 0.4, "y": 0.0, "w": 0.2, "h": 0.2}},
        ],
    }
    mid = resolve_track_at_frame(geom, 5)
    assert mid is not None and "points" not in mid
    assert mid["bbox"] == {"x": 0.2, "y": 0.0, "w": 0.2, "h": 0.2}


def _mask_ref(seed: str) -> dict:
    sha = seed * 64
    return {
        "encoding": "coco_rle_ref",
        "size": [2, 3],
        "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
        "sha256": sha,
        "runs": 4,
        "bytes": 64,
    }


def test_mask_track_uses_nearest_hold_with_earlier_tie_and_endpoint_hold():
    geometry = {
        "type": "video_track_mask",
        "track_id": "m1",
        "keyframes": [
            {
                "frame_index": 4,
                "mask": _mask_ref("a"),
                "source": "manual",
                "attributes": {"state": "a"},
            },
            {
                "frame_index": 8,
                "mask": _mask_ref("b"),
                "source": "prediction",
                "occluded": True,
            },
        ],
        "outside": [],
    }
    assert resolve_track_at_frame(geometry, 0)["mask"]["sha256"] == "a" * 64
    tie = resolve_track_at_frame(geometry, 6)
    assert tie["mask"]["sha256"] == "a" * 64
    assert tie["attributes"] == {"state": "a"}
    assert resolve_track_at_frame(geometry, 20)["mask"]["sha256"] == "b" * 64


def test_mask_track_outside_wins_and_keyframes_mode_preserves_mask():
    geometry = {
        "type": "video_track_mask",
        "track_id": "m1",
        "keyframes": [
            {"frame_index": 4, "mask": _mask_ref("a"), "source": "manual"},
            {"frame_index": 8, "mask": _mask_ref("b"), "source": "prediction"},
        ],
        "outside": [{"from": 5, "to": 7}],
    }
    assert resolve_track_at_frame(geometry, 6) is None
    rows = resolved_track_frames(geometry, frame_mode="keyframes")
    assert [row["mask"]["sha256"] for row in rows] == ["a" * 64, "b" * 64]

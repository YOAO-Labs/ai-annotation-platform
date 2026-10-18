import shutil
import subprocess

import pytest
from pydantic import ValidationError

from aap_media_probe.probe import (
    FFPROBE_TIMEOUT_SECONDS,
    parse_ffprobe_frame_timetable,
    parse_ffprobe_packet_samples,
    parse_ffprobe_video_metadata,
    probe_chunk_samples,
    probe_video_file,
    probe_video_frame_timetable,
)
from aap_media_probe.protocol import StreamComplete, VideoFrame


def test_metadata_rounding_and_frame_count_match_existing_contract():
    result = parse_ffprobe_video_metadata(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "30000/1001",
                    "nb_frames": "N/A",
                    "duration": "2.002",
                }
            ],
            "format": {"duration": "2.002"},
        }
    )
    assert result == {
        "duration_ms": 2002,
        "fps": 29.97,
        "frame_count": 60,
        "width": 1280,
        "height": 720,
        "codec": "h264",
    }


def test_frame_timetable_preserves_order_and_skips_unusable_frames():
    rows = parse_ffprobe_frame_timetable(
        {
            "frames": [
                {
                    "media_type": "video",
                    "best_effort_timestamp_time": "0.0005",
                    "key_frame": "1",
                    "pkt_pos": "48",
                    "pict_type": "I",
                },
                {"media_type": "audio", "best_effort_timestamp_time": "0.01"},
                {
                    "media_type": "video",
                    "pkt_pts_time": "0.033367",
                    "key_frame": "0",
                    "pkt_pos": "N/A",
                    "pict_type": "P",
                },
                {"media_type": "video", "pts_time": "N/A"},
            ]
        }
    )
    assert rows == [
        {
            "frame_index": 0,
            "pts_ms": 0,
            "is_keyframe": True,
            "pict_type": "I",
            "byte_offset": 48,
        },
        {
            "frame_index": 1,
            "pts_ms": 33,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": None,
        },
    ]


def test_packet_samples_keep_decode_order_and_assign_presentation_rank():
    rows = parse_ffprobe_packet_samples(
        {
            "packets": [
                {
                    "pts_time": "0.000000",
                    "duration_time": "0.033367",
                    "size": "5000",
                    "pos": "48",
                    "flags": "K__",
                },
                {
                    "pts_time": "0.066733",
                    "duration_time": "0.033367",
                    "size": "1200",
                    "pos": "5048",
                    "flags": "___",
                },
                {
                    "pts_time": "0.033367",
                    "duration_time": "0.033367",
                    "size": "800",
                    "pos": "6248",
                    "flags": "___",
                },
            ]
        },
        start_frame=10,
    )
    assert [row["offset_in_chunk"] for row in rows] == [48, 5048, 6248]
    assert [row["frame_index"] for row in rows] == [10, 12, 11]
    assert [row["pts_ms"] for row in rows] == [0, 67, 33]


def test_packet_samples_skip_missing_positions_and_pts():
    rows = parse_ffprobe_packet_samples(
        {
            "packets": [
                {"pts_time": "0", "size": "1", "pos": "0"},
                {"pts_time": "1", "size": "1"},
                {"pts_time": "N/A", "size": "1", "pos": "2"},
            ]
        },
        start_frame=7,
    )
    assert len(rows) == 1
    assert rows[0]["frame_index"] == 7


def test_probe_helpers_keep_the_existing_30_second_timeout(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if "-show_frames" in args:
            output = (
                '{"frames": [{"media_type":"video","best_effort_timestamp_time":"0"}]}'
            )
        elif "-show_packets" in args:
            output = '{"packets": []}'
        else:
            output = '{"streams": [{"codec_type":"video","width":1,"height":1}]}'
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert probe_video_file(video)["width"] == 1
    assert probe_video_frame_timetable(video)[0]["pts_ms"] == 0
    assert probe_chunk_samples(video, 3) == []
    assert len(calls) == 3
    assert all(call[1]["timeout"] == FFPROBE_TIMEOUT_SECONDS for call in calls)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not installed",
)
def test_real_ffprobe_reads_a_small_generated_video(tmp_path):
    video = tmp_path / "tiny.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=2:d=1",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    assert probe_video_file(video)["width"] == 16
    assert len(probe_video_frame_timetable(video)) == 2
    assert len(probe_chunk_samples(video, 5)) == 2


def test_importing_kernel_does_not_require_service_modules():
    from aap_media_probe import probe

    assert "fastapi" not in probe.__dict__
    assert "sqlalchemy" not in probe.__dict__


def test_protocol_rejects_coerced_boolean_and_completion_count():
    with pytest.raises(ValidationError):
        VideoFrame.model_validate(
            {
                "frame_index": 0,
                "pts_ms": 0,
                "is_keyframe": "false",
                "pict_type": None,
                "byte_offset": None,
            }
        )
    with pytest.raises(ValidationError):
        StreamComplete.model_validate(
            {"type": "complete", "count": 2.0, "sha256": "0" * 64}
        )

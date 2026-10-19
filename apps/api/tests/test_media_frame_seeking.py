"""Compare rounded-timetable seeks with ordinal decoding, without a database."""

import json
import shutil
import subprocess

import pytest
from PIL import Image

from app.workers.media_chunks import extract_video_chunk
from app.workers.media_frames import extract_video_frame_image


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe are required",
)


@pytest.fixture(
    params=[("12", False), ("240", False), ("30000/1001", False), ("12", True)]
)
def video(request, tmp_path):
    rate, variable = request.param
    source = tmp_path / "source.mp4"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=96x64:rate={rate}:duration=0.5",
    ]
    if variable:
        command += ["-vf", r"select=not(eq(n\,1)+eq(n\,3))", "-fps_mode", "vfr"]
    command += ["-frames:v", "6", "-c:v", "libx264", "-threads", "1", str(source)]
    subprocess.run(command, check=True, capture_output=True)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    timestamps = [
        float(frame["best_effort_timestamp_time"])
        for frame in json.loads(probe.stdout)["frames"]
    ]
    return source, timestamps


def test_frame_seeks_match_ordinal_decoding_including_last_frame(video, tmp_path):
    source, timestamps = video
    for index, timestamp in enumerate(timestamps):
        actual = tmp_path / f"actual-{index}.png"
        expected = tmp_path / f"expected-{index}.png"
        extract_video_frame_image(source, actual, round(timestamp * 1000), 64)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(source),
                "-vf",
                rf"select=eq(n\,{index}),scale='min(64,iw)':-2",
                "-frames:v",
                "1",
                str(expected),
            ],
            check=True,
            capture_output=True,
        )
        with Image.open(actual) as image, Image.open(expected) as reference:
            assert image.size == reference.size
            assert image.tobytes() == reference.tobytes(), (
                f"wrong source frame {index} at {timestamp}"
            )


@pytest.mark.parametrize("extension", ["webp", "jpg"])
def test_last_frame_produces_decodable_cache_image(video, tmp_path, extension):
    source, timestamps = video
    output = tmp_path / f"last.{extension}"
    extract_video_frame_image(source, output, round(timestamps[-1] * 1000), 64)
    with Image.open(output) as image:
        image.load()
        assert image.size == (64, 42)
        assert image.format == ("WEBP" if extension == "webp" else "JPEG")


def test_transcoded_chunk_keeps_rounded_start_frame(video, tmp_path):
    source, timestamps = video
    start = 2
    count = len(timestamps) - start
    actual = tmp_path / "actual.mp4"
    extract_video_chunk(source, actual, round(timestamps[start] * 1000), count)

    def pixels(path):
        return subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout

    frame_size = 96 * 64 * 3
    actual_pixels = pixels(actual)
    assert len(actual_pixels) == count * frame_size
    source_pixels = pixels(source)
    references = [
        source_pixels[i : i + frame_size]
        for i in range(0, len(source_pixels), frame_size)
    ]
    for index in range(count):
        frame = actual_pixels[index * frame_size : (index + 1) * frame_size]
        # H.264 re-encoding is lossy; identify the nearest decoded source frame.
        errors = [
            sum((a - b) ** 2 for a, b in zip(frame, reference))
            for reference in references
        ]
        assert min(range(len(errors)), key=errors.__getitem__) == start + index

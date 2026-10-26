"""Verified anonymous video handoff and actual decoder process settlement."""

from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from billiard.exceptions import SoftTimeLimitExceeded
import pytest
from PIL import Image

from app.services import media_process as process
from app.workers.media_chunks import (
    extract_video_chunk,
    extract_video_chunk_smart_copy,
)
from app.workers.media_frames import extract_video_frame_image
from app.services.media_probe import (
    probe_chunk_samples,
    probe_video_file,
    probe_video_frame_timetable,
)


@contextmanager
def _stage(raw, directory):
    with tempfile.TemporaryFile("w+b", dir=directory) as binary:
        binary.write(raw)
        binary.flush()
        binary.seek(0)
        yield SimpleNamespace(file=binary, size=len(raw))


@pytest.fixture
def mov_header(tmp_path):
    with _stage(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2", tmp_path) as stage:
        yield stage


def _require_decoder():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for actual decoder acceptance")


def _encode(path, *, codec="libx264"):
    _require_decoder()
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=96x64:rate=12:duration=0.5",
                "-frames:v",
                "6",
                "-c:v",
                codec,
                "-g",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "1",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def video_bytes(tmp_path_factory):
    directory = tmp_path_factory.mktemp("anonymous-video-source")
    try:
        return _encode(directory / "source.mp4")
    finally:
        directory.rmdir()


@pytest.fixture
def stage(video_bytes, tmp_path):
    with _stage(video_bytes, tmp_path) as value:
        yield value


def test_decoder_input_borrows_a_regular_stage_without_reading_or_copying(mov_header):
    original = mov_header.file

    class BorrowOnly:
        def fileno(self):
            return original.fileno()

        def read(self, *args):
            raise AssertionError("Decoder handoff must not copy the original")

        def close(self):
            raise AssertionError("The caller owns the source stage")

    mov_header.file = BorrowOnly()
    before = os.fstat(original.fileno())
    path, fds = process.decoder_input(mov_header)
    assert path == Path(f"/proc/self/fd/{original.fileno()}")
    assert fds == (original.fileno(),)
    assert os.fstat(fds[0]) == before
    assert not original.closed


@pytest.mark.parametrize("size", [-1, 0, True, 99999])
def test_decoder_input_rejects_a_changed_or_invalid_stage_size(mov_header, size):
    mov_header.size = size
    with pytest.raises(ValueError, match="video_decoder_input_invalid"):
        process.decoder_input(mov_header)


def test_decoder_input_rejects_a_closed_stage(tmp_path):
    with tempfile.TemporaryFile("w+b", dir=tmp_path) as binary:
        binary.write(b"known")
        value = SimpleNamespace(file=binary, size=5)
    with pytest.raises(ValueError, match="video_decoder_input_invalid"):
        process.decoder_input(value)


def test_decoder_input_rejects_nonregular_descriptors(tmp_path):
    read_fd, write_fd = os.pipe()
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for fd in (read_fd, directory_fd):
            value = SimpleNamespace(file=SimpleNamespace(fileno=lambda: fd), size=1)
            with pytest.raises(ValueError, match="video_decoder_input_invalid"):
                process.decoder_input(value)
    finally:
        for fd in (read_fd, write_fd, directory_fd):
            os.close(fd)


def test_empty_inheritance_preserves_legacy_subprocess_options(monkeypatch):
    calls = []
    expected = subprocess.CompletedProcess(["legacy"], 0, "ok", "")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(process.subprocess, "run", run)
    assert process.run_process(["legacy"], capture_output=True, text=True) is expected
    assert calls == [(["legacy"], {"capture_output": True, "text": True})]


@pytest.mark.parametrize(
    "payload",
    [
        b"#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1/private.ts\n",
        b"ffconcat version 1.0\nfile '/unverified/source.mp4'\n",
        b"<MPD><BaseURL>http://127.0.0.1/private.mp4</BaseURL></MPD>",
    ],
)
def test_playlists_are_rejected_before_any_subprocess_or_external_read(
    tmp_path, monkeypatch, payload
):
    def forbidden(*args, **kwargs):
        raise AssertionError("An unverified nested source must not reach a decoder")

    monkeypatch.setattr(process.subprocess, "Popen", forbidden)
    with _stage(payload, tmp_path) as stage:
        path, fds = process.decoder_input(stage)
        with pytest.raises(ValueError, match="video_decoder_format_unsupported"):
            probe_video_file(path, pass_fds=fds)


@pytest.mark.parametrize("case", ["other-path", "extra-fd", "override", "shell"])
def test_inheritance_cannot_substitute_an_input_or_decoder_policy(
    mov_header, monkeypatch, case
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid handoff must fail before process creation")

    monkeypatch.setattr(process.subprocess, "Popen", forbidden)
    path, fds = process.decoder_input(mov_header)
    args, kwargs = ["ffprobe", str(path)], {}
    if case == "other-path":
        args[-1] = "/unverified/video.mp4"
    elif case == "extra-fd":
        fds += fds
    elif case == "override":
        args[1:1] = ["-protocol_whitelist", "file,http"]
    else:
        kwargs["shell"] = True
    with pytest.raises(ValueError, match="video_decoder_input_invalid"):
        process.run_process(args, pass_fds=fds, **kwargs)


def test_anonymous_original_supports_all_three_probes_without_a_named_copy(
    stage, video_bytes, tmp_path
):
    path, fds = process.decoder_input(stage)
    stage.file.seek(7)
    before = os.fstat(fds[0])
    metadata = probe_video_file(path, pass_fds=fds)
    timetable = probe_video_frame_timetable(path, pass_fds=fds)
    samples = probe_chunk_samples(path, 40, pass_fds=fds)
    assert (metadata["width"], metadata["height"], metadata["fps"]) == (96, 64, 12)
    assert len(timetable) == 6
    assert [row["frame_index"] for row in samples] == list(range(40, 46))
    assert stage.file.tell() == 7
    after = os.fstat(fds[0])
    assert (before.st_dev, before.st_ino, before.st_size) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
    )
    assert (
        hashlib.sha256(os.pread(fds[0], stage.size, 0)).digest()
        == hashlib.sha256(video_bytes).digest()
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "extension,codec",
    [
        ("mp4", "libx264"),
        ("mov", "libx264"),
        ("mkv", "libx264"),
        ("avi", "mpeg4"),
        ("wmv", "wmv2"),
        ("flv", "flv"),
        ("webm", "libvpx-vp9"),
    ],
)
def test_platform_self_contained_containers_can_be_probed_from_anonymous_fd(
    tmp_path, extension, codec
):
    raw = _encode(tmp_path / f"source.{extension}", codec=codec)
    with _stage(raw, tmp_path) as stage:
        path, fds = process.decoder_input(stage)
        metadata = probe_video_file(path, pass_fds=fds)
        assert (metadata["width"], metadata["height"]) == (96, 64)
        assert len(probe_video_frame_timetable(path, pass_fds=fds)) == 6
    assert list(tmp_path.iterdir()) == []


def test_inherited_frame_matches_real_ordinal_decode(stage, tmp_path):
    path, fds = process.decoder_input(stage)
    output = tmp_path / "frame.png"
    extract_video_frame_image(path, output, 167, 64, pass_fds=fds)
    expected = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            r"select=eq(n\,2),scale='min(64,iw)':-2",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "-",
        ],
        pass_fds=fds,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    with Image.open(output) as image, Image.open(io.BytesIO(expected)) as reference:
        image.load()
        reference.load()
        assert image.size == (64, 42)
        assert image.tobytes() == reference.tobytes()
    assert not stage.file.closed
    assert [value.name for value in tmp_path.iterdir()] == ["frame.png"]


@pytest.mark.parametrize("smart", [False, True])
def test_both_chunk_modes_decode_the_inherited_original(stage, tmp_path, smart):
    path, fds = process.decoder_input(stage)
    output = tmp_path / "chunk.mp4"
    if smart:
        extract_video_chunk_smart_copy(path, output, 0, 250, pass_fds=fds)
    else:
        extract_video_chunk(path, output, 0, 3, pass_fds=fds)
    samples = probe_chunk_samples(output, 10)
    assert len(samples) == 3
    assert [row["frame_index"] for row in samples] == [10, 11, 12]
    assert samples[0]["is_keyframe"]
    metadata = probe_video_file(output)
    assert (metadata["width"], metadata["height"]) == (96, 64)
    assert not stage.file.closed
    assert [value.name for value in tmp_path.iterdir()] == ["chunk.mp4"]


def _slow_args(path):
    return [
        "ffmpeg",
        "-v",
        "error",
        "-stream_loop",
        "-1",
        "-re",
        "-i",
        str(path),
        "-an",
        "-f",
        "null",
        "-",
    ]


def test_real_timeout_kills_waits_drains_and_closes_before_return(stage, monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(process.subprocess, "Popen", popen)
    path, fds = process.decoder_input(stage)
    with pytest.raises(subprocess.TimeoutExpired) as interrupted:
        process.run_process(
            _slow_args(path), pass_fds=fds, capture_output=True, timeout=0.05
        )
    assert len(children) == 1
    child = children[0]
    assert child.returncode is not None
    assert child.stdout.closed and child.stderr.closed
    assert not getattr(interrupted.value, "storage_operation_unsettled", False)
    assert not stage.file.closed


def test_real_soft_limit_drains_the_process_and_repeated_wait_interruption(
    stage, monkeypatch
):
    real_popen = subprocess.Popen
    children = []
    initial = SoftTimeLimitExceeded()
    wait_calls = []

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        communicate, wait = child.communicate, child.wait
        first = True

        def interrupted_communicate(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise initial
            return communicate(*args, **kwargs)

        def interrupted_wait(*args, **kwargs):
            wait_calls.append(True)
            if len(wait_calls) == 1:
                raise KeyboardInterrupt()
            return wait(*args, **kwargs)

        child.communicate = interrupted_communicate
        child.wait = interrupted_wait
        children.append(child)
        return child

    monkeypatch.setattr(process.subprocess, "Popen", popen)
    path, fds = process.decoder_input(stage)
    with pytest.raises(SoftTimeLimitExceeded) as interrupted:
        process.run_process(_slow_args(path), pass_fds=fds, capture_output=True)
    assert interrupted.value is initial
    assert len(wait_calls) >= 2
    assert children[0].returncode is not None
    assert children[0].stdout.closed and children[0].stderr.closed
    assert not getattr(initial, "storage_operation_unsettled", False)
    assert not stage.file.closed


class _Pipe(io.BytesIO):
    def __init__(self, *, uncertain=False):
        super().__init__()
        self.uncertain = uncertain

    def close(self):
        was_closed = self.closed
        super().close()
        if self.uncertain and not was_closed:
            raise OSError("physical_pipe_close_uncertain")


def test_uncertain_pipe_close_keeps_responsibility_and_closes_other_pipes(
    mov_header, monkeypatch
):
    child = SimpleNamespace(
        stdin=_Pipe(uncertain=True),
        stdout=_Pipe(),
        stderr=_Pipe(),
        returncode=0,
        communicate=lambda *args, **kwargs: (b"", b""),
        poll=lambda: 0,
        wait=lambda **kwargs: 0,
    )
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)
    path, fds = process.decoder_input(mov_header)
    with pytest.raises(OSError, match="physical_pipe_close_uncertain") as failure:
        process.run_process(["ffprobe", str(path)], pass_fds=fds)
    assert failure.value.storage_operation_unsettled
    assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    assert not mov_header.file.closed


def test_unknown_process_constructor_keeps_responsibility(mov_header, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError("process_constructor_uncertain")

    monkeypatch.setattr(process.subprocess, "Popen", refused)
    path, fds = process.decoder_input(mov_header)
    with pytest.raises(OSError, match="process_constructor_uncertain") as failure:
        process.run_process(["ffprobe", str(path)], pass_fds=fds)
    assert failure.value.storage_operation_unsettled


def test_unknown_kill_and_wait_do_not_turn_timeout_into_success(
    mov_header, monkeypatch
):
    initial = subprocess.TimeoutExpired("ffprobe", 1)
    calls = []

    def communicate(*args, **kwargs):
        raise initial

    def kill():
        calls.append("kill")
        raise OSError("cannot-confirm-signal")

    def wait(**kwargs):
        calls.append(("wait", kwargs))
        raise subprocess.TimeoutExpired("ffprobe", 1)

    child = SimpleNamespace(
        stdin=None,
        stdout=_Pipe(),
        stderr=_Pipe(),
        returncode=None,
        communicate=communicate,
        kill=kill,
        wait=wait,
    )
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)
    path, fds = process.decoder_input(mov_header)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        process.run_process(["ffprobe", str(path)], pass_fds=fds, capture_output=True)
    assert failure.value is initial
    assert failure.value.storage_operation_unsettled
    assert calls == ["kill", ("wait", {"timeout": 1})]
    assert child.stdout.closed and child.stderr.closed


def test_mov_policy_explicitly_disables_external_data_references(
    mov_header, monkeypatch
):
    calls = []
    child = SimpleNamespace(
        stdin=None,
        stdout=None,
        stderr=None,
        returncode=0,
        communicate=lambda *args, **kwargs: (None, None),
        poll=lambda: 0,
        wait=lambda **kwargs: 0,
    )

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return child

    monkeypatch.setattr(process.subprocess, "Popen", popen)
    path, fds = process.decoder_input(mov_header)
    process.run_process(["ffprobe", str(path)], pass_fds=fds)
    args, kwargs = calls[0]
    assert kwargs["pass_fds"] == fds
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args[args.index("-enable_drefs") + 1] == "0"
    assert args[args.index("-use_absolute_path") + 1] == "0"

from contextlib import contextmanager
import hashlib
from io import BytesIO
import os
import stat
import threading

import pytest

from aap_filesystem_read import (
    FilesystemStorageError,
    RootSpec,
    capture_anchor,
    content_file,
    stage_content,
)
from aap_filesystem_read import stage as module


@contextmanager
def source(data=b"verified bytes", *, size=None, body=None):
    with body or BytesIO(data) as opened:
        yield opened, {"size": len(data) if size is None else size, "identity": [1, 2]}


def test_stage_is_private_complete_and_owned_until_close(tmp_path):
    data = b"verified bytes"
    result = stage_content(
        source,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expected_size=len(data),
        directory=tmp_path,
    )
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)
    assert result.observation == {"size": len(data), "identity": [1, 2]}
    assert result.file.tell() == 0 and result.file.read() == data
    info = os.fstat(result.file.fileno())
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 0
    assert list(tmp_path.iterdir()) == []
    result.close()
    result.close()
    assert result.file.closed


def test_empty_content_reads_eof_and_accepts_zero_budget(tmp_path):
    with stage_content(
        lambda: source(b""),
        expected_size=0,
        expected_sha256=hashlib.sha256(b"").hexdigest(),
        max_bytes=0,
        directory=tmp_path,
    ) as result:
        assert result.file.read() == b"" and result.size == 0


@pytest.mark.parametrize(
    ("factory", "kwargs", "code"),
    [
        (source, {"expected_sha256": "0" * 64}, "content_mismatch"),
        (source, {"expected_size": 1}, "source_changed"),
        (lambda: source(b"short", size=10), {}, "source_changed"),
        (lambda: source(b"longer", size=2), {}, "source_changed"),
        (lambda: source(b"beyond", size=6), {"max_bytes": 5}, "content_too_large"),
        (lambda: source(b"beyond", size=1), {"max_bytes": 5}, "content_too_large"),
        (lambda: source(b"", size=-1), {}, "content_read_invalid"),
        (lambda: source(b"", size=True), {}, "content_read_invalid"),
    ],
)
def test_mismatch_and_truncation_close_private_stages(
    tmp_path, monkeypatch, factory, kwargs, code
):
    actual_temporary = module.tempfile.TemporaryFile
    opened = []

    def temporary(**options):
        result = actual_temporary(**options)
        opened.append(result)
        return result

    monkeypatch.setattr(module.tempfile, "TemporaryFile", temporary)
    with pytest.raises(FilesystemStorageError, match=code):
        stage_content(factory, directory=tmp_path, **kwargs)
    assert opened and all(item.closed for item in opened)
    assert list(tmp_path.iterdir()) == []


def test_partial_physical_reads_continue_until_exact_eof(tmp_path):
    class PartialBody(BytesIO):
        def read(self, maximum):
            return super().read(min(2, maximum))

    data = b"several chunks"
    with stage_content(
        lambda: source(data, body=PartialBody(data)), directory=tmp_path
    ) as result:
        assert result.file.read() == data


def test_stage_is_never_returned_before_real_source_exit_revalidation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    path = root / "sample.bin"
    path.write_bytes(b"original")
    spec = RootSpec("test", str(root))
    anchor = capture_anchor(spec)

    @contextmanager
    def factory():
        with content_file(spec, "", "sample.bin", expected_anchor=anchor) as opened:
            yield opened
            path.write_bytes(b"changed source bytes")

    with pytest.raises(FilesystemStorageError, match="source_changed"):
        stage_content(factory, directory=state)
    assert list(state.iterdir()) == []


def test_deadline_includes_source_exit_and_cancelled_reads(tmp_path, monkeypatch):
    now = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    @contextmanager
    def late_exit():
        with source() as opened:
            yield opened
        now[0] = 11

    with pytest.raises(FilesystemStorageError, match="content_read_timeout"):
        stage_content(late_exit, directory=tmp_path)
    now[0] = 0
    stop = threading.Event()

    class CancelledBody(BytesIO):
        def read(self, maximum):
            stop.set()
            return super().read(maximum)

    with pytest.raises(FilesystemStorageError, match="content_read_timeout"):
        stage_content(
            lambda: source(body=CancelledBody(b"verified bytes")),
            directory=tmp_path,
            stop=stop,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": module.MAX_BYTES + 1},
        {"max_bytes": -1},
        {"max_bytes": True},
        {"timeout_seconds": 11},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": 10**1000},
        {"expected_sha256": "A" * 64},
        {"expected_sha256": "bad"},
        {"expected_size": -1},
        {"expected_size": True},
    ],
)
def test_invalid_limits_never_open_source_or_create_files(tmp_path, kwargs):
    def forbidden():
        pytest.fail("Invalid limits must be rejected before opening source")

    with pytest.raises(FilesystemStorageError, match="content_read_invalid"):
        stage_content(forbidden, directory=tmp_path, **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_declared_size_over_budget_is_rejected_before_source(tmp_path):
    def forbidden():
        pytest.fail("Oversized declared content must not open the source")

    with pytest.raises(FilesystemStorageError, match="content_too_large"):
        stage_content(forbidden, expected_size=6, max_bytes=5, directory=tmp_path)


@pytest.mark.parametrize("directory", [None, "relative", "/absent-state-aap-test"])
def test_explicit_existing_state_directory_is_required(directory):
    with pytest.raises(FilesystemStorageError, match="stage_directory_"):
        stage_content(source, directory=directory)


def test_invalid_body_result_is_never_a_stage(tmp_path):
    class InvalidBody(BytesIO):
        def read(self, maximum):
            return bytearray(b"bytes")

    with pytest.raises(FilesystemStorageError, match="content_mismatch"):
        stage_content(lambda: source(body=InvalidBody()), directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cleanup_failure_marks_the_original_failure_unsettled(tmp_path, monkeypatch):
    original_close = module.close_registered

    def uncertain_close(handle):
        original_close(handle)
        raise OSError("close outcome uncertain")

    monkeypatch.setattr(module, "close_registered", uncertain_close)
    with pytest.raises(FilesystemStorageError, match="content_mismatch") as caught:
        stage_content(source, expected_sha256="0" * 64, directory=tmp_path)
    assert caught.value.storage_operation_unsettled is True
    assert list(tmp_path.iterdir()) == []


def test_stage_close_failure_keeps_responsibility_and_can_be_retried(tmp_path):
    result = stage_content(source, directory=tmp_path)
    original = result.file

    class UncertainClose:
        calls = 0

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("not yet closed")
            original.close()

    wrapper = UncertainClose()
    result.file = wrapper
    try:
        with pytest.raises(OSError) as caught:
            result.close()
        assert caught.value.storage_operation_unsettled is True
        assert not original.closed
        result.close()
        result.close()
        assert original.closed and wrapper.calls == 2
    finally:
        original.close()


def test_repeated_real_read_failures_close_source_and_stage_descriptors(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (root / "sample.bin").write_bytes(b"real source bytes")
    spec = RootSpec("test", str(root))
    anchor = capture_anchor(spec)

    def descriptors():
        result = []
        for name in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except FileNotFoundError:
                continue
            if target == "/" or target.startswith(str(tmp_path)):
                result.append(target)
        return sorted(result)

    before = descriptors()
    for _ in range(20):
        with pytest.raises(FilesystemStorageError, match="content_mismatch"):
            stage_content(
                lambda: content_file(spec, "", "sample.bin", expected_anchor=anchor),
                expected_sha256="0" * 64,
                directory=state,
            )
    assert descriptors() == before
    assert list(state.iterdir()) == []

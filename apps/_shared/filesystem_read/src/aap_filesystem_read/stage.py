"""Bounded full-byte snapshots that become readable only after source revalidation."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
import tempfile
import threading
import time

from .roots import FilesystemStorageError, close_registered, mark_unsettled

MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 10
CHUNK_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass
class VerifiedStage:
    """Own a private temporary file until its physical close succeeds."""

    file: object
    sha256: str
    size: int
    observation: dict
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    def close(self):
        with self._lock:
            if not self._closed:
                close_registered(self.file)
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _validate(expected_sha256, expected_size, max_bytes, timeout_seconds, directory):
    if (
        type(max_bytes) is not int
        or not 0 <= max_bytes <= MAX_BYTES
        or type(timeout_seconds) not in {int, float}
        or not 0 < timeout_seconds <= MAX_SECONDS
        or not math.isfinite(timeout_seconds)
        or (
            expected_sha256 is not None
            and (
                not isinstance(expected_sha256, str)
                or _SHA256.fullmatch(expected_sha256) is None
            )
        )
        or (
            expected_size is not None
            and (type(expected_size) is not int or expected_size < 0)
        )
    ):
        raise FilesystemStorageError("content_read_invalid")
    if expected_size is not None and expected_size > max_bytes:
        raise FilesystemStorageError("content_too_large")
    if directory is None:
        raise FilesystemStorageError("stage_directory_required")
    try:
        target = Path(directory)
    except TypeError:
        raise FilesystemStorageError("stage_directory_invalid") from None
    if not target.is_absolute() or not target.is_dir():
        raise FilesystemStorageError("stage_directory_invalid")
    return target


def stage_content(
    factory,
    expected_sha256=None,
    expected_size=None,
    max_bytes=MAX_BYTES,
    timeout_seconds=MAX_SECONDS,
    stop=None,
    directory=None,
):
    """Copy and verify the entire source before returning an owned binary stage.

    ``directory`` must be the caller's existing private state directory. The
    anonymous stage is transient: process recovery rereads and revalidates the
    source instead of treating local completion as a central acknowledgement.
    Deadlines are cooperative; a blocked physical read must settle before this
    function returns and before the caller releases its admission permit.
    """
    directory = _validate(
        expected_sha256, expected_size, max_bytes, timeout_seconds, directory
    )
    deadline = time.monotonic() + timeout_seconds
    temporary = None
    failure = None
    transferred = False

    def check():
        if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
            raise FilesystemStorageError("content_read_timeout")

    try:
        check()
        temporary = tempfile.TemporaryFile(
            mode="w+b", dir=directory, prefix="aap-agent-stage-"
        )
        digest, size = hashlib.sha256(), 0
        with factory() as (body, observation):
            if (
                not isinstance(observation, dict)
                or type(observation.get("size")) is not int
            ):
                raise FilesystemStorageError("content_read_invalid")
            observed_size = observation["size"]
            if observed_size < 0:
                raise FilesystemStorageError("content_read_invalid")
            if observed_size > max_bytes:
                raise FilesystemStorageError("content_too_large")
            if expected_size is not None and expected_size != observed_size:
                raise FilesystemStorageError("source_changed")
            while True:
                check()
                chunk = body.read(CHUNK_BYTES)
                check()
                if type(chunk) is not bytes or len(chunk) > CHUNK_BYTES:
                    raise FilesystemStorageError("content_mismatch")
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise FilesystemStorageError("content_too_large")
                if size > observed_size:
                    raise FilesystemStorageError("source_changed")
                if temporary.write(chunk) != len(chunk):
                    raise FilesystemStorageError("content_stage_unavailable")
                digest.update(chunk)
            if size != observed_size:
                raise FilesystemStorageError("source_changed")
        # Exiting the source context performs the pinned file/directory checks.
        # No byte from this file is returned before that exit has succeeded.
        check()
        sha256 = digest.hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise FilesystemStorageError("content_mismatch")
        temporary.flush()
        temporary.seek(0)
        check()
        result = VerifiedStage(temporary, sha256, size, observation)
        transferred = True
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if not transferred and temporary is not None:
            try:
                close_registered(temporary)
            except BaseException as exc:
                if failure is not None:
                    mark_unsettled(failure)
                else:
                    raise mark_unsettled(exc)

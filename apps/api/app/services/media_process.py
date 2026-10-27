"""Inherited verified video files and fully settled decoder subprocesses."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
import subprocess

from billiard.exceptions import SoftTimeLimitExceeded


_FORMATS = "mov,matroska,webm,avi,asf,flv"
_MOV_ATOMS = {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"uuid", b"pnot"}
_ASF_HEADER = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_POLICY_OPTIONS = {
    "-protocol_whitelist",
    "-protocol_blacklist",
    "-format_whitelist",
    "-enable_drefs",
    "-use_absolute_path",
}


def _invalid(code="video_decoder_input_invalid"):
    raise ValueError(code)


def _source_fd(pass_fds):
    if (
        not isinstance(pass_fds, tuple)
        or len(pass_fds) != 1
        or type(pass_fds[0]) is not int
        or pass_fds[0] < 3
    ):
        _invalid()
    fd = pass_fds[0]
    try:
        source = os.fstat(fd)
    except (OSError, ValueError):
        _invalid()
    if not stat.S_ISREG(source.st_mode) or source.st_size <= 0:
        _invalid()
    return fd, source


def decoder_input(stage) -> tuple[Path, tuple[int, ...]]:
    """Borrow one open regular stage without copying, duplicating or closing it."""
    try:
        fd = stage.file.fileno()
        expected = stage.size
    except (AttributeError, OSError, ValueError):
        _invalid()
    fd, source = _source_fd((fd,))
    if type(expected) is not int or expected != source.st_size:
        _invalid()
    return Path(f"/proc/self/fd/{fd}"), (fd,)


def _demuxer(fd):
    # Only platform-supported self-contained containers are accepted. In
    # particular no HLS/DASH/concat/image sequence demuxer may open a second
    # unverified file. Header inspection leaves the shared file offset intact.
    header = os.pread(fd, 16, 0)
    if header[4:8] in _MOV_ATOMS:
        return "mov"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "matroska"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "avi"
    if header == _ASF_HEADER:
        return "asf"
    if header.startswith(b"FLV"):
        return "flv"
    _invalid("video_decoder_format_unsupported")


def _decoder_args(args, pass_fds):
    fd, _ = _source_fd(pass_fds)
    if not isinstance(args, (list, tuple)) or not args:
        _invalid()
    command = [os.fspath(value) for value in args]
    binary = Path(command[0]).name
    if binary not in {"ffmpeg", "ffprobe"} or any(
        value in _POLICY_OPTIONS for value in command
    ):
        _invalid()
    path = f"/proc/self/fd/{fd}"
    if binary == "ffmpeg":
        inputs = [index for index, value in enumerate(command) if value == "-i"]
        if len(inputs) != 1 or inputs[0] + 1 >= len(command):
            _invalid()
        insertion = inputs[0]
        if command[insertion + 1] != path:
            _invalid()
    else:
        insertion = len(command) - 1
        if command[insertion] != path or "-i" in command:
            _invalid()
    demuxer = _demuxer(fd)
    options = [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        _FORMATS,
        "-f",
        demuxer,
    ]
    if demuxer == "mov":
        # MOV alone supports external data references. Explicitly refuse both
        # relative and absolute aliases even though the decoder defaults agree.
        options += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    return command[:insertion] + options + command[insertion:]


def _unsettled(exc):
    from app.services.storage_access import mark_unsettled

    return mark_unsettled(exc)


def _complete(call):
    interruption = None
    while True:
        try:
            return call(), interruption
        except (
            asyncio.CancelledError,
            KeyboardInterrupt,
            InterruptedError,
            SoftTimeLimitExceeded,
        ) as exc:
            interruption = interruption or exc


def _settle(process, *, failed):
    failure = None
    uncertain = False

    def attempt(call, *, physical=False):
        nonlocal failure, uncertain
        try:
            value, interrupted = _complete(call)
            failure = failure or interrupted
            return value
        except BaseException as exc:
            failure = failure or exc
            uncertain = uncertain or physical
            return None

    if failed:
        attempt(process.kill, physical=True)
    # Confirm process death before releasing its inherited source or closing
    # pipes. If signaling itself failed, a bounded observation retains unknown
    # responsibility rather than pretending an unkillable process has ended.
    attempt(lambda: process.wait(timeout=1 if uncertain else None), physical=True)
    if process.returncode is None:
        uncertain = True
    elif failed:
        attempt(process.communicate)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            attempt(stream.close, physical=True)
    if uncertain:
        failure = _unsettled(failure or RuntimeError("video_decoder_process_unsettled"))
    return failure


def run_process(args, *, pass_fds: tuple[int, ...] = (), **kwargs):
    """Run a decoder while retaining its inherited source through real cleanup.

    Empty inheritance is exactly the legacy subprocess.run path. Verified FD
    calls own every process and pipe until wait/drain/close has completed, even
    when interrupted by an exception outside subprocess.run's timeout handling.
    """
    if not pass_fds:
        return subprocess.run(args, **kwargs)
    command = _decoder_args(args, pass_fds)
    if (
        kwargs.get("shell")
        or kwargs.get("close_fds") is False
        or kwargs.get("executable") is not None
        or kwargs.get("preexec_fn") is not None
    ):
        _invalid()
    options = dict(kwargs)
    input_ = options.pop("input", None)
    capture_output = options.pop("capture_output", False)
    check = options.pop("check", False)
    timeout = options.pop("timeout", None)
    if input_ is not None:
        if options.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        options["stdin"] = subprocess.PIPE
    if capture_output:
        if options.get("stdout") is not None or options.get("stderr") is not None:
            raise ValueError("stdout and stderr may not be used with capture_output")
        options["stdout"] = options["stderr"] = subprocess.PIPE
    process = None
    failure = None
    result = None
    try:
        try:
            process = subprocess.Popen(command, pass_fds=pass_fds, **options)
        except BaseException as exc:
            raise _unsettled(exc)
        stdout, stderr = process.communicate(input_, timeout=timeout)
        code = process.poll()
        if code is None:
            raise _unsettled(RuntimeError("video_decoder_process_unsettled"))
        if check and code:
            raise subprocess.CalledProcessError(code, command, stdout, stderr)
        result = subprocess.CompletedProcess(command, code, stdout, stderr)
    except BaseException as exc:
        failure = exc
    finally:
        if process is not None:
            closing = _settle(process, failed=failure is not None)
            if closing is not None:
                if failure is None:
                    failure = closing
                elif getattr(closing, "storage_operation_unsettled", False):
                    _unsettled(failure)
    if failure is not None:
        raise failure
    return result

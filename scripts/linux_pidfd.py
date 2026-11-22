"""Linux pidfd operations used by the isolated native and fixture owners."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import select
import sys


_PIDFD_SEND_SIGNAL = 424
_PIDFD_OPEN = 434
_P_PIDFD = 3


def _syscall(number: int, *arguments: object) -> int:
    if sys.platform != "linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    }:
        raise OSError(errno.ENOSYS, "Linux pidfd syscalls are unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *arguments)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def open_pidfd(pid: int) -> int:
    return _syscall(_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0))


def pidfd_send_signal(fd: int, sig: int) -> None:
    _syscall(
        _PIDFD_SEND_SIGNAL,
        ctypes.c_int(fd),
        ctypes.c_int(sig),
        ctypes.c_void_p(),
        ctypes.c_uint(0),
    )


def pidfd_alive(fd: int) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll(0)
    if not events:
        return True
    flags = events[0][1]
    if flags & select.POLLNVAL:
        raise OSError(errno.EBADF, "invalid pidfd")
    if flags & (select.POLLIN | select.POLLHUP):
        return False
    raise OSError(errno.EIO, "pidfd polling failed")


def waitid_pidfd(fd: int, options: int):
    """Return os.waitid's result (or None with WNOHANG); preserve kernel errors."""
    return os.waitid(_P_PIDFD, fd, options)

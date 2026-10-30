"""Fixed stdlib-only probe executed in the owned Python plugin container."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
import stat
import sys


def measured_paths(root: int, assets: list[dict]) -> list[str]:
    """Measure validated relative paths through one pinned directory descriptor."""
    measured = []
    for asset in assets:
        try:
            with ExitStack() as stack:
                directory = root
                parts = asset["path"].split("/")
                for part in parts[:-1]:
                    directory = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    stack.callback(os.close, directory)
                descriptor = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
                stack.callback(os.close, descriptor)
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_size != asset["size_bytes"]
                ):
                    continue
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ) == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) and digest.hexdigest() == asset["sha256"]:
                    measured.append(asset["path"])
        except OSError:
            continue
    return measured


if __name__ == "__main__":
    request = json.load(sys.stdin)
    root = os.open(request["root"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        print(
            json.dumps(measured_paths(root, request["assets"]), separators=(",", ":"))
        )
    finally:
        os.close(root)

"""Control a registered MinIO source's native write fence without application env.

The private descriptor names exact Docker resources. No command adopts a source,
creates a replacement backend, or authorizes an application cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from storage_source_fence_docker import DockerAdapter
from storage_source_fence_state import Controller, FenceError, Journal


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise FenceError("invalid_arguments")


def descriptor(path):
    path = Path(path)
    parent = path.parent
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise FenceError("descriptor_directory_private")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > 65536
        ):
            raise FenceError("descriptor_private")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise FenceError("descriptor_budget")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FenceError("descriptor_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise FenceError("descriptor_invalid") from None
    if not isinstance(value, dict):
        raise FenceError("descriptor_invalid")
    return value, hashlib.sha256(raw).hexdigest()


def parser():
    command = SafeParser(description=__doc__)
    modes = command.add_subparsers(dest="command", required=True)
    for name in ("init", "preview", "seal", "status", "release"):
        mode = modes.add_parser(name)
        mode.add_argument("--config", type=Path, required=True)
        mode.add_argument("--state", type=Path, required=True)
        if name in {"seal", "release"}:
            mode.add_argument("--expected-revision", type=int, required=True)
            mode.add_argument("--operation-id", required=True)
    return command


def bound_state(directory, state):
    # One physical ingress has one journal and lock, even across CLI invocations.
    expected = Path(directory).parent / "fence-state.json"
    if Path(os.path.abspath(state)) != expected:
        raise FenceError("state_path_changed")


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        config, digest = descriptor(args.config)
        adapter = DockerAdapter(config)
        bound_state(adapter.directory, args.state)
        if args.command == "init":
            adapter.preview()
        with Journal(args.state, digest, create=args.command == "init") as journal:
            controller = Controller(journal, adapter)
            if args.command == "preview":
                result = {
                    "schema": "aap.storage-source-fence-preview/v1",
                    "counts": controller.preview(),
                }
            elif args.command in {"seal", "release"}:
                result = getattr(controller, args.command)(
                    args.expected_revision, args.operation_id
                )
            else:
                result = controller.status()
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "interrupted"}), file=sys.stderr)
        return 130
    except Exception as exc:
        code = exc.code if isinstance(exc, FenceError) else "source_fence_failed"
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

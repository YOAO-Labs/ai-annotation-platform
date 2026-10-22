"""Restricted command-line entry point for a manually fenced video restore."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from typing import Any

from .restore import VideoRestoreManager
from .restore_contract import (
    RESTORE_REQUEST_MAX_BYTES,
    RestoreRejected,
    RestoreResult,
    RestoreUnavailable,
    decode_restore_command,
)
from .restore_postgres import VideoRestorePostgresProvider


def _read_regular_file(path_value: str, *, limit: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path_value, flags)
    except OSError:
        raise RestoreUnavailable() from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RestoreUnavailable()
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise RestoreUnavailable()
        return raw, info
    except RestoreUnavailable:
        raise
    except OSError:
        raise RestoreUnavailable() from None
    finally:
        os.close(descriptor)


def _database_url(path_value: str) -> str:
    raw, info = _read_regular_file(path_value, limit=8192)
    if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid() or not raw:
        raise RestoreUnavailable()
    try:
        value = raw.decode("utf-8", errors="strict").rstrip("\r\n")
    except UnicodeError:
        raise RestoreUnavailable() from None
    if not value or any(character.isspace() for character in value):
        raise RestoreUnavailable()
    return value


def _command_file(path_value: str):
    raw, _info = _read_regular_file(path_value, limit=RESTORE_REQUEST_MAX_BYTES)
    return decode_restore_command(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aap-video-restore")
    parser.add_argument("--database-url-file", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--status", metavar="RESTORE_ID")
    action.add_argument("--command-file")
    return parser


def _public_output(value: Any) -> dict[str, Any]:
    if isinstance(value, RestoreResult):
        if value.state in {"pending", "completed"} and value.record is not None:
            return value.record
        if value.state == "rejected" and value.reason is not None:
            return {"reason": value.reason}
        return {"reason": "owner_unavailable"}
    if isinstance(value, dict):
        return value
    return {"reason": "owner_unavailable"}


async def _run(args: argparse.Namespace) -> int:
    try:
        dsn = _database_url(args.database_url_file)
        command = _command_file(args.command_file) if args.command_file else None
    except (RestoreUnavailable, OSError, UnicodeError, ValueError):
        _write_output({"reason": "invalid_operator_input"})
        return 2
    try:
        provider = VideoRestorePostgresProvider(dsn)
        manager = VideoRestoreManager(provider)
    except Exception:
        _write_output({"reason": "owner_unavailable"})
        return 2
    try:
        if args.inspect:
            output = await manager.inspect()
            code = 0
        elif args.status is not None:
            result = await manager.status(args.status)
            output = _public_output(result)
            code = 0 if result.state in {"pending", "completed"} else 2
        else:
            result = await manager.execute(command)
            output = _public_output(result)
            code = 0 if result.state in {"pending", "completed"} else 2
    except RestoreRejected as rejected:
        output = {"reason": rejected.reason}
        code = 2
    except Exception:
        output = {"reason": "owner_unavailable"}
        code = 2
    _write_output(output)
    return code


def _write_output(value: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()


__all__ = ["main"]

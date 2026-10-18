"""Independent stdlib/asyncpg peer for the task-creation SQL protocol tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid

import asyncpg


_SUMMARY_FIELDS = {
    "operation_id",
    "actor_id",
    "dataset_id",
    "project_id",
    "association_id",
    "job_id",
    "mode",
    "chunk_size",
    "state",
    "total",
    "next_chunk",
    "processed",
    "created",
}
_CHUNK_FIELDS = {
    "operation_id",
    "project_id",
    "chunk_index",
    "created",
    "processed",
    "cumulative_created",
    "cumulative_processed",
    "total",
    "next_chunk",
    "complete",
    "project_total",
}


class PeerFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stdout.flush()


def _read_json_line(limit: int) -> object:
    line = sys.stdin.readline(limit + 1)
    if not line or len(line) > limit or not line.endswith("\n"):
        raise PeerFailure("invalid_request")
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise PeerFailure("invalid_request") from error


def _reply_data(raw: object, schema: str) -> dict[str, object]:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise PeerFailure("invalid_reply") from error
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise PeerFailure("contract_mismatch")
    if payload.get("ok") is False:
        error = payload.get("error")
        if (
            set(payload) != {"schema", "ok", "error"}
            or not isinstance(error, dict)
            or set(error) != {"code"}
            or not isinstance(error.get("code"), str)
            or re.fullmatch(r"[a-z_]{1,64}", error["code"]) is None
        ):
            raise PeerFailure("invalid_reply")
        raise PeerFailure(error["code"])
    if (
        payload.get("ok") is not True
        or set(payload) != {"schema", "ok", "data"}
        or not isinstance(payload.get("data"), dict)
    ):
        raise PeerFailure("invalid_reply")
    return payload["data"]


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _validate_summary(
    raw: object, operation_id: uuid.UUID, expected: dict[str, object]
) -> dict[str, object]:
    data = _reply_data(raw, "aap-task-creation/v1")
    if set(data) != _SUMMARY_FIELDS:
        raise PeerFailure("invalid_reply")
    if data["operation_id"] != str(operation_id):
        raise PeerFailure("scope_mismatch")
    for field in (
        "actor_id",
        "dataset_id",
        "project_id",
        "association_id",
        "job_id",
    ):
        if data[field] != expected[field]:
            raise PeerFailure("scope_mismatch")
    if data["mode"] != "generic" or data["chunk_size"] != expected["chunk_size"]:
        raise PeerFailure("scope_mismatch")
    if data["total"] != expected["total"]:
        raise PeerFailure("plan_changed")
    if not all(
        _integer(data[field]) for field in ("next_chunk", "processed", "created")
    ):
        raise PeerFailure("invalid_reply")
    if data["state"] not in {"running", "completed"}:
        raise PeerFailure("plan_not_prepared")
    if data["created"] > data["processed"] or data["processed"] > data["total"]:
        raise PeerFailure("invalid_reply")
    return data


def _validate_chunk(
    raw: object,
    operation_id: uuid.UUID,
    summary: dict[str, object],
) -> dict[str, object]:
    data = _reply_data(raw, "aap-task-creation-chunk/v1")
    if set(data) != _CHUNK_FIELDS:
        raise PeerFailure("invalid_reply")
    if (
        data["operation_id"] != str(operation_id)
        or data["project_id"] != summary["project_id"]
        or data["chunk_index"] != summary["next_chunk"]
        or data["total"] != summary["total"]
        or data["next_chunk"] != summary["next_chunk"] + 1
    ):
        raise PeerFailure("scope_mismatch")
    integer_fields = _CHUNK_FIELDS - {"operation_id", "project_id", "complete"}
    if not all(_integer(data[field]) for field in integer_fields):
        raise PeerFailure("invalid_reply")
    if (
        data["created"] > data["processed"]
        or data["processed"] > data["total"]
        or data["cumulative_created"] != summary["created"] + data["created"]
        or data["cumulative_processed"] != summary["processed"] + data["processed"]
        or data["complete"] is not (data["cumulative_processed"] == data["total"])
    ):
        raise PeerFailure("invalid_reply")
    return data


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True, type=uuid.UUID)
    parser.add_argument("--pause-after-chunk", type=int)
    return parser.parse_args()


async def _wait_for_resume() -> None:
    line = await asyncio.to_thread(sys.stdin.readline, 4097)
    if not line or len(line) > 4096 or not line.endswith("\n"):
        raise PeerFailure("control_closed")
    try:
        control = json.loads(line)
    except json.JSONDecodeError as error:
        raise PeerFailure("invalid_control") from error
    if control != {"resume": True}:
        raise PeerFailure("invalid_control")


async def _run(args: argparse.Namespace) -> int:
    request = _read_json_line(16384)
    if not isinstance(request, dict) or set(request) != {
        "database_url",
        "expected",
    }:
        raise PeerFailure("invalid_request")
    dsn = request["database_url"]
    expected = request["expected"]
    if not isinstance(dsn, str) or not isinstance(expected, dict):
        raise PeerFailure("invalid_request")
    required_expected = {
        "actor_id",
        "dataset_id",
        "project_id",
        "association_id",
        "job_id",
        "chunk_size",
        "total",
    }
    if set(expected) != required_expected:
        raise PeerFailure("invalid_request")

    connection = await asyncpg.connect(dsn, timeout=10, command_timeout=30)
    try:
        current_user = await connection.fetchval("SELECT current_user")
        if current_user != "append_client":
            raise PeerFailure("wrong_database_role")
        _emit({"type": "ready", "current_user": current_user})

        paused = False
        max_chunks = (expected["total"] + expected["chunk_size"] - 1) // expected[
            "chunk_size"
        ]
        for _ in range(max_chunks + 2):
            transaction = connection.transaction()
            await transaction.start()
            try:
                summary = _validate_summary(
                    await connection.fetchval(
                        "SELECT aap_labeling_v1.read_dataset_task_creation($1::uuid)",
                        args.operation_id,
                    ),
                    args.operation_id,
                    expected,
                )
                if summary["state"] == "completed":
                    if (
                        summary["processed"] != expected["total"]
                        or summary["next_chunk"] != max_chunks
                    ):
                        raise PeerFailure("invalid_reply")
                    await transaction.commit()
                    _emit(
                        {
                            "type": "complete",
                            "operation_id": str(args.operation_id),
                            "total": summary["total"],
                            "processed": summary["processed"],
                            "created": summary["created"],
                            "next_chunk": summary["next_chunk"],
                        }
                    )
                    return 0

                chunk = _validate_chunk(
                    await connection.fetchval(
                        "SELECT aap_labeling_v1.advance_dataset_task_creation("
                        "$1::uuid, $2::integer)",
                        args.operation_id,
                        summary["next_chunk"],
                    ),
                    args.operation_id,
                    summary,
                )
                await transaction.commit()
            except BaseException:
                try:
                    await transaction.rollback()
                except Exception:
                    pass
                raise

            _emit(
                {
                    "type": "chunk_committed",
                    "operation_id": str(args.operation_id),
                    "chunk_index": chunk["chunk_index"],
                    "created": chunk["created"],
                    "processed": chunk["processed"],
                    "cumulative_created": chunk["cumulative_created"],
                    "cumulative_processed": chunk["cumulative_processed"],
                    "total": chunk["total"],
                    "next_chunk": chunk["next_chunk"],
                    "complete": chunk["complete"],
                    "project_total": chunk["project_total"],
                }
            )
            if not paused and args.pause_after_chunk == chunk["chunk_index"]:
                paused = True
                await _wait_for_resume()

        raise PeerFailure("cursor_exhausted")
    finally:
        await connection.close()


async def _main() -> int:
    try:
        return await _run(_arguments())
    except PeerFailure as error:
        _emit({"type": "error", "code": error.code})
        return 2
    except Exception:
        _emit({"type": "error", "code": "peer_internal_error"})
        return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

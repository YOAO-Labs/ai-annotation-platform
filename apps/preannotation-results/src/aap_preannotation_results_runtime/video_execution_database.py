"""Restricted, transaction-bound access to the video Jobs owner RPC."""

from __future__ import annotations

import asyncio
from typing import Any

from aap_preannotation_results.video_execution import (
    SCHEMA,
    ClaimRequest,
    GuardRequest,
    RenewRequest,
    ReleaseRequest,
    VideoExecutionFailure,
    decode_native_reply,
    request_bytes,
)
from aap_preannotation_results import contract as c

_SCHEMA = "aap_jobs_v1"
_FUNCTION = "video_execution_step"
_STEP_SQL = f"SELECT {_SCHEMA}.{_FUNCTION}($1::text,$2::text)"
_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"
_REQUESTS = {
    "claim": ClaimRequest,
    "renew": RenewRequest,
    "guard": GuardRequest,
    "release": ReleaseRequest,
}


class VideoExecutionPostgresData:
    """The caller retains its connection, transaction, and writer locks.

    A guard result is usable only inside this exact transaction. This adapter
    deliberately does not expose an operation-per-transaction guard service.
    """

    def __init__(self, connection: Any, transaction_id: str) -> None:
        self._connection = connection
        self._transaction_id = transaction_id

    @classmethod
    async def bind(cls, connection: Any) -> VideoExecutionPostgresData:
        try:
            if connection.is_in_transaction() is not True:
                raise VideoExecutionFailure("storage_unavailable")
            from aap_owner_data.postgres import verify_function_only_role

            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions={
                    (_FUNCTION, "text, text"),
                    ("video_execution_checkpoint_step", "text, text"),
                },
                role="video_execution_client",
            )
            raw = await connection.fetchval(_STEP_SQL, "policy", "{}")
            if type(raw) is str:
                raw = raw.encode("utf-8")
            elif type(raw) is dict:
                raw = c.json_bytes(raw)
            if type(raw) is not bytes or len(raw) > 2 * 1024 * 1024:
                raise VideoExecutionFailure("storage_unavailable")
            policy = c.parse_json(raw)
            if (
                policy
                != {
                    "schema": SCHEMA,
                    "status": "ok",
                    "ready": True,
                    "functions": [f"{_SCHEMA}.{_FUNCTION}(text,text)"],
                }
                or policy["ready"] is not True
            ):
                raise VideoExecutionFailure("storage_unavailable")
            transaction_id = await connection.fetchval(
                "SELECT pg_catalog.pg_current_xact_id()::text"
            )
            if type(transaction_id) is not str or not transaction_id.isdecimal():
                raise VideoExecutionFailure("storage_unavailable")
            return cls(connection, transaction_id)
        except (VideoExecutionFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoExecutionFailure("storage_unavailable") from None

    async def step(self, action: str, request: Any) -> dict:
        if type(action) is not str or type(request) is not _REQUESTS.get(action):
            raise VideoExecutionFailure("invalid_request")
        try:
            if (
                self._connection.is_in_transaction() is not True
                or await self._connection.fetchval(_TRANSACTION_SQL)
                != self._transaction_id
            ):
                raise VideoExecutionFailure("storage_unavailable")
            raw = await self._connection.fetchval(
                _STEP_SQL, action, request_bytes(request).decode("utf-8")
            )
            return decode_native_reply(action, request, raw)
        except (VideoExecutionFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoExecutionFailure("storage_unavailable") from None

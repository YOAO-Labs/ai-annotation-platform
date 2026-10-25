"""Restricted, transaction-bound access to the video checkpoint data owner."""

from __future__ import annotations

import asyncio
from typing import Any

from aap_preannotation_results import contract as c
from aap_preannotation_results.video_execution_journal import (
    NATIVE_FUNCTIONS,
    PolicyRequest,
    VideoJournalFailure,
    decode_native_reply,
    request_bytes,
    validate_request,
)

_SCHEMA = "aap_jobs_v1"
_STEP_SQL = f"SELECT {_SCHEMA}.video_execution_checkpoint_step($1::text,$2::text)"
_FUNCTIONS = {
    ("video_execution_step", "text, text"),
    ("video_execution_checkpoint_step", "text, text"),
}
_XID_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"


class VideoJournalPostgresData:
    """The caller owns the active transaction and validates before committing."""

    def __init__(self, connection: Any, transaction_id: str) -> None:
        self.connection = connection
        self.transaction_id = transaction_id

    @classmethod
    async def bind(cls, connection: Any):
        try:
            if connection.is_in_transaction() is not True:
                raise VideoJournalFailure("storage_unavailable")
            from aap_owner_data.postgres import verify_function_only_role

            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_FUNCTIONS,
                role="video_execution_client",
            )
            reply = decode_native_reply(
                "policy",
                PolicyRequest(),
                await connection.fetchval(_STEP_SQL, "policy", "{}"),
            )
            if reply["functions"] != list(NATIVE_FUNCTIONS):
                raise VideoJournalFailure("storage_unavailable")
            xid = await connection.fetchval(
                "SELECT pg_catalog.pg_current_xact_id()::text"
            )
            if type(xid) is not str or not xid.isdecimal():
                raise VideoJournalFailure("storage_unavailable")
            return cls(connection, xid)
        except (VideoJournalFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoJournalFailure("storage_unavailable") from None

    async def step(self, action: str, request: c.Value):
        request = validate_request(action, request)
        encoded = request_bytes(action, request).decode("utf-8")
        try:
            if (
                self.connection.is_in_transaction() is not True
                or await self.connection.fetchval(_XID_SQL) != self.transaction_id
            ):
                raise VideoJournalFailure("storage_unavailable")
            return decode_native_reply(
                action,
                request,
                await self.connection.fetchval(_STEP_SQL, action, encoded),
            )
        except (VideoJournalFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoJournalFailure("storage_unavailable") from None

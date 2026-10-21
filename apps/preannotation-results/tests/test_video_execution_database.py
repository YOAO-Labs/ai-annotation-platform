import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_preannotation_results.video_execution import (
    ClaimRequest,
    VideoExecutionFailure,
)
from aap_preannotation_results_runtime.video_execution_database import (
    VideoExecutionPostgresData,
)


class Connection:
    def __init__(self):
        self.transaction_id = "41"
        self.active = True
        self.native_mutations = 0

    def is_in_transaction(self):
        return self.active

    async def fetchval(self, statement, *args):
        if statement.endswith("pg_current_xact_id()::text") or statement.endswith(
            "pg_current_xact_id_if_assigned()::text"
        ):
            return self.transaction_id
        if args == ("policy", "{}"):
            return {
                "schema": "aap-video-execution-data/v1",
                "status": "ok",
                "ready": True,
                "functions": ["aap_jobs_v1.video_execution_step(text,text)"],
            }
        self.native_mutations += 1
        raise AssertionError("a stale binding must not send a native mutation")


def test_binding_cannot_carry_writer_authority_across_transactions(monkeypatch):
    async def exercise():
        from aap_owner_data import postgres

        verify = AsyncMock()
        monkeypatch.setattr(postgres, "verify_function_only_role", verify)
        connection = Connection()
        data = await VideoExecutionPostgresData.bind(connection)
        verify.assert_awaited_once_with(
            connection,
            schema="aap_jobs_v1",
            functions={
                ("video_execution_step", "text, text"),
                ("video_execution_checkpoint_step", "text, text"),
            },
            role="video_execution_client",
        )
        connection.transaction_id = "42"
        with pytest.raises(VideoExecutionFailure, match="^storage_unavailable$"):
            await data.step("claim", ClaimRequest(job_id=uuid4(), worker_id=uuid4()))
        assert connection.native_mutations == 0

    asyncio.run(exercise())


def test_broad_database_role_cannot_bind_video_owner(monkeypatch):
    async def exercise():
        from aap_owner_data import postgres

        monkeypatch.setattr(
            postgres,
            "verify_function_only_role",
            AsyncMock(side_effect=PermissionError("broad login")),
        )
        connection = Connection()
        with pytest.raises(VideoExecutionFailure, match="^storage_unavailable$"):
            await VideoExecutionPostgresData.bind(connection)
        assert connection.native_mutations == 0

    asyncio.run(exercise())

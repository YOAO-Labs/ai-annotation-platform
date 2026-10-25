from __future__ import annotations

import pytest

from aap_labeling.video_writer_contract import (
    LABELING_VIDEO_WRITER_DATA_SCHEMA,
    MAX_VIDEO_WRITER_REQUEST_BYTES,
    MAX_VIDEO_WRITER_STAGED_BYTES,
    VIDEO_WRITER_FUNCTION,
    VideoWriterUnavailable,
)
from aap_labeling.video_writer_postgres import VideoWriterPostgresProvider


class FakeConnection:
    def __init__(self):
        self.active = False
        self.xid = "101"
        self.events = []

    def is_in_transaction(self):
        return self.active

    def transaction(self, *, isolation):
        connection = self

        class Transaction:
            async def __aenter__(self):
                connection.events.append(("begin", isolation))
                connection.active = True

            async def __aexit__(self, exc_type, exc, traceback):
                connection.events.append("rollback" if exc_type else "commit")
                connection.active = False

        return Transaction()

    async def fetchval(self, query, *args, timeout=None):
        if query == "SHOW transaction_isolation":
            return "read committed"
        if query.endswith("pg_current_xact_id()::text"):
            return self.xid
        if query.endswith("pg_current_xact_id_if_assigned()::text"):
            return self.xid
        if "video_writer_step" in query:
            action, _request = args
            if action == "policy":
                return {
                    "schema": LABELING_VIDEO_WRITER_DATA_SCHEMA,
                    "ok": True,
                    "data": {
                        "functions": [VIDEO_WRITER_FUNCTION],
                        "max_request_bytes": MAX_VIDEO_WRITER_REQUEST_BYTES,
                        "max_staged_bytes": MAX_VIDEO_WRITER_STAGED_BYTES,
                    },
                }
        raise AssertionError(query)

    async def close(self, *, timeout):
        self.events.append("close")

    def terminate(self):
        self.events.append("terminate")


def allow_function_only_role(monkeypatch):
    import aap_owner_data.postgres

    seen = []

    async def verify(connection, *, schema, functions, role):
        seen.append((connection, schema, functions, role))

    monkeypatch.setattr(aap_owner_data.postgres, "verify_function_only_role", verify)
    return seen


@pytest.mark.asyncio
async def test_owned_provider_opens_read_committed_and_commits_after_manager_scope(
    monkeypatch,
):
    seen = allow_function_only_role(monkeypatch)
    connection = FakeConnection()
    data = VideoWriterPostgresProvider("postgresql://unused")

    async def connect():
        return connection

    monkeypatch.setattr(data, "_connect", connect)
    async with data.transaction() as transaction:
        assert transaction.owns_transaction is True
        reply = await transaction.call("policy", {})
        assert reply["data"]["functions"] == [VIDEO_WRITER_FUNCTION]

    assert connection.events == [("begin", "read_committed"), "commit", "close"]
    assert seen == [
        (
            connection,
            "aap_labeling_v1",
            {("video_writer_step", "text, text")},
            "labeling_video_writer_client",
        )
    ]


@pytest.mark.asyncio
async def test_provider_policy_decodes_caps_inside_owned_transaction(monkeypatch):
    allow_function_only_role(monkeypatch)
    connection = FakeConnection()
    data = VideoWriterPostgresProvider("postgresql://unused")

    async def connect():
        return connection

    monkeypatch.setattr(data, "_connect", connect)
    policy = await data.policy()
    assert policy.functions == (VIDEO_WRITER_FUNCTION,)
    assert policy.max_request_bytes == MAX_VIDEO_WRITER_REQUEST_BYTES
    assert policy.max_staged_bytes == MAX_VIDEO_WRITER_STAGED_BYTES
    assert connection.events[-2:] == ["commit", "close"]


@pytest.mark.asyncio
async def test_borrowed_provider_preserves_caller_transaction_and_fences_xid(
    monkeypatch,
):
    seen = allow_function_only_role(monkeypatch)
    connection = FakeConnection()
    connection.active = True
    data = VideoWriterPostgresProvider.borrowed(connection)

    async with data.transaction() as transaction:
        assert transaction.owns_transaction is False
        await transaction.call("policy", {})
        connection.xid = "102"
        with pytest.raises(VideoWriterUnavailable):
            await transaction.call("policy", {})

    assert connection.active is True
    assert connection.events == []
    assert seen[0][1:] == (
        "aap_labeling_v1",
        {("video_writer_step", "text, text")},
        "labeling_video_writer_client",
    )


@pytest.mark.asyncio
async def test_borrowed_adapter_cannot_be_reused_after_its_context_ends(monkeypatch):
    allow_function_only_role(monkeypatch)
    connection = FakeConnection()
    connection.active = True
    data = VideoWriterPostgresProvider.borrowed(connection)

    async with data.transaction() as transaction:
        await transaction.call("policy", {})

    with pytest.raises(VideoWriterUnavailable):
        await transaction.call("policy", {})
    assert connection.active is True


@pytest.mark.asyncio
async def test_bind_rejects_non_read_committed_and_function_profile_mismatch(
    monkeypatch,
):
    import aap_owner_data.postgres

    connection = FakeConnection()
    connection.active = True
    original_fetchval = connection.fetchval

    async def repeatable_read(query, *args, timeout=None):
        if query == "SHOW transaction_isolation":
            return "repeatable read"
        return await original_fetchval(query, *args, timeout=timeout)

    connection.fetchval = repeatable_read
    allow_function_only_role(monkeypatch)
    data = VideoWriterPostgresProvider.borrowed(connection)
    with pytest.raises(VideoWriterUnavailable):
        async with data.transaction():
            pass

    async def denied(*args, **kwargs):
        raise aap_owner_data.postgres.FunctionRoleUnavailable()

    monkeypatch.setattr(aap_owner_data.postgres, "verify_function_only_role", denied)
    connection.fetchval = original_fetchval
    with pytest.raises(VideoWriterUnavailable):
        async with data.transaction():
            pass

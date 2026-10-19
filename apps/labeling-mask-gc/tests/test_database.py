from __future__ import annotations

import json

import pytest

from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc_runtime.database import AsyncpgMaskGcData


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transactions += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.commits += exc_type is None
        self.connection.rollbacks += exc_type is not None


class FakeConnection:
    def __init__(self, reply):
        self.reply = reply
        self.statements = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return FakeTransaction(self)

    async def execute(self, statement, *args):
        self.statements.append((statement, args))

    async def fetchval(self, statement, *args, **kwargs):
        self.statements.append((statement, args))
        return self.reply


class FakePool:
    def __init__(self, reply):
        self.connection = FakeConnection(reply)

    def acquire(self, *, timeout):
        assert timeout == 5

        class Acquire:
            async def __aenter__(inner_self):
                return self.connection

            async def __aexit__(inner_self, exc_type, exc, traceback):
                return None

        return Acquire()


@pytest.mark.asyncio
async def test_native_call_uses_one_committed_transaction_and_allowlisted_sql():
    reply = {
        "schema": c.SCHEMA,
        "ok": True,
        "data": {
            "active_intents": 1,
            "indeterminate_intents": 0,
            "completed_intents": 3,
            "sweep": None,
        },
    }
    pool = FakePool(json.dumps(reply))
    database = AsyncpgMaskGcData(pool)

    result = await database.call("read_mask_gc_progress", c.ReadMaskGcProgressRequest())

    assert result.active_intents == 1
    connection = pool.connection
    assert connection.transactions == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    native_sql = connection.statements[1][0]
    assert native_sql == "SELECT aap_labeling_v1.read_mask_gc_progress($1::jsonb)"


@pytest.mark.asyncio
async def test_runtime_adapter_does_not_expose_write_gate_functions():
    pool = FakePool("unused")
    database = AsyncpgMaskGcData(pool)

    with pytest.raises(c.MaskGcFailure) as error:
        await database.call(
            "begin_mask_content_write",
            c.BeginMaskContentWriteRequest(
                object_keys=["raster-masks/sha256/a0/a0/" + "a0a0" + "a" * 60 + ".json"]
            ),
        )
    assert error.value.code == "invalid_request"
    assert pool.connection.statements == []


@pytest.mark.asyncio
async def test_native_error_envelope_rolls_back_without_driver_details():
    reply = {
        "schema": c.SCHEMA,
        "ok": False,
        "error": {"code": "lease_lost"},
    }
    pool = FakePool(json.dumps(reply))
    database = AsyncpgMaskGcData(pool)

    with pytest.raises(c.MaskGcFailure) as error:
        await database.call("read_mask_gc_progress", c.ReadMaskGcProgressRequest())
    assert error.value.code == "lease_lost"
    assert pool.connection.rollbacks == 1

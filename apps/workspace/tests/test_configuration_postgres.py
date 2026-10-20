from contextlib import asynccontextmanager

import pytest

from aap_workspace.configuration_contract import ConfigurationUnavailable
from aap_workspace.configuration_postgres import ConfigurationPostgresData


class ClosingFailureConnection:
    def __init__(self):
        self.outcome = None
        self.terminated = False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        except BaseException:
            self.outcome = "rolled_back"
            raise
        else:
            self.outcome = "committed"

    async def close(self, **kwargs):
        raise TimeoutError("Connection close timed out")

    def terminate(self):
        self.terminated = True


async def binding(monkeypatch):
    connection = ClosingFailureConnection()
    data = ConfigurationPostgresData("postgresql://private/test")

    async def connect():
        return connection

    async def verify(connection):
        pass

    monkeypatch.setattr(data, "_connect", connect)
    monkeypatch.setattr(data, "_verify_policy", verify)
    return data, connection


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_reject_a_committed_mutation(monkeypatch):
    data, connection = await binding(monkeypatch)
    async with data.transaction(write=True):
        pass
    assert connection.outcome == "committed"
    assert connection.terminated


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_original_business_error(monkeypatch):
    data, connection = await binding(monkeypatch)
    error = ValueError("Backend pool missing")
    with pytest.raises(ValueError) as caught:
        async with data.transaction(write=True):
            raise error
    assert caught.value is error
    assert connection.outcome == "rolled_back"
    assert connection.terminated


@pytest.mark.asyncio
async def test_readiness_close_error_is_unavailable(monkeypatch):
    data, connection = await binding(monkeypatch)
    with pytest.raises(ConfigurationUnavailable):
        await data.configuration_policy()
    assert connection.terminated

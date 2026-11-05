"""Provider failure must not strand other run-owned child processes."""

import builtins
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.e2e_fixture_backend import FixtureBackends


@pytest.mark.asyncio
async def test_failed_provider_does_not_leave_other_runs_running(monkeypatch):
    # This case exercises child lifecycle, not HTTP reachability or model execution.
    monkeypatch.setenv("PLAYWRIGHT_ML_FIXTURE_HOST", "fixture.invalid")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    providers = FixtureBackends()
    try:
        failed = await providers.start(uuid4())
        other = await providers.start(uuid4())
        failed.process.kill()
        await failed.process.wait()

        with pytest.raises(builtins.ExceptionGroup):
            await providers.close()

        assert failed.process.returncode != 0
        assert other.process.returncode == 0
    finally:
        for provider in providers.runs.values():
            if provider.process is not None and provider.process.returncode is None:
                await provider.close()

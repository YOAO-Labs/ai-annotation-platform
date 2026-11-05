"""Provider failure must not strand other run-owned child processes."""

import builtins
import asyncio
import base64
import secrets
import time
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi import HTTPException

from app.services.e2e_fixture_backend import FixtureBackendRuntime, FixtureBackends
from yoao_protocol_v2.routing import (
    ROUTE_ADMISSION_AUDIENCE,
    ROUTE_ADMISSION_HEADER,
    ROUTE_ADMISSION_PROTOCOL,
    ROUTE_CONTROL_PATH,
    ROUTE_CONTROL_PROTOCOL,
)


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


@pytest.mark.asyncio
async def test_provider_close_does_not_release_held_native_request(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_ML_FIXTURE_HOST", "fixture.invalid")
    provider = await FixtureBackendRuntime(uuid4()).start()
    registry, epoch, command_id = str(uuid4()), str(uuid4()), str(uuid4())
    challenge, key = secrets.token_hex(32), secrets.token_bytes(32)
    provider.control("registry", registry)
    request = close = None
    port = provider.listener.getsockname()[1]
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer " + provider.token},
        timeout=10,
        trust_env=False,
    ) as client:
        try:
            async def control(action):
                response = await client.post(ROUTE_CONTROL_PATH, json={
                    "schema": ROUTE_CONTROL_PROTOCOL,
                    "command_id": command_id,
                    "registry_id": registry,
                    "authority_epoch": epoch,
                    "retired_epoch": None,
                    "challenge": challenge,
                    "action": action,
                    "signing_key": base64.urlsafe_b64encode(key).decode().rstrip("=")
                    if action == "bind" else None,
                })
                assert response.status_code == 200
                return response.json()

            assert (await control("drain"))["phase"] == "drained"
            bound = await control("bind")
            provider.control("hold", None)
            now = int(time.time())
            admission = jwt.encode({
                "schema": ROUTE_ADMISSION_PROTOCOL,
                "aud": ROUTE_ADMISSION_AUDIENCE,
                "registry_id": registry,
                "backend_incarnation": bound["backend_incarnation"],
                "authority_epoch": epoch,
                "scope_id": str(uuid4()),
                "attempt_id": str(uuid4()),
                "operation": "predict",
                "method": "POST",
                "path": "/predict",
                "iat": now,
                "exp": now + 60,
            }, key, algorithm="HS256", headers={"typ": "JWT"})
            request = asyncio.create_task(client.post(
                "/predict", headers={ROUTE_ADMISSION_HEADER: admission},
                json={"task": {"id": str(uuid4())}},
            ))
            async with asyncio.timeout(5):
                while not provider.requests:
                    if request.done():
                        raise AssertionError(f"Provider request ended: {(await request).status_code}")
                    await asyncio.sleep(0.01)
            assert provider.controller.active_requests == 1
            with pytest.raises(HTTPException) as busy:
                provider.control("offline_seal", None)
            assert busy.value.status_code == 409
            assert (await client.get("/health")).status_code == 503
            provider.control("offline_resume", None)
            close = asyncio.create_task(provider.close())
            async with asyncio.timeout(5):
                while not provider.server.should_exit:
                    await asyncio.sleep(0)
            assert not provider.gate.is_set()
            assert not request.done() and not close.done()
            provider.control("release", None)
            assert (await request).status_code == 200
            await close
        finally:
            provider.control("release", None)
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            if close is not None:
                await close
            else:
                await provider.close()

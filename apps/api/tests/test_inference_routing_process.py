"""Installed routing entrypoint restart, replay and selected-owner failure proof."""

import asyncio
import json
import os
from pathlib import Path
import socket
import time
import uuid

import httpx
import pytest

from aap_inference_contract.routing import (
    RoutingScopeView,
    RoutingUnavailable,
    make_routing_command,
    make_routing_runtime_request,
    make_routing_status_request,
)
from aap_inference_contract.routing_client import RoutingHttpClient
from app.config import settings
from app.services.ml_routing.authority import RoutingAuthority
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _cleanup, _connect, _seed

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _acquire(registry_id):
    return make_routing_command(
        "acquire",
        {
            "pool_id": None,
            "registry_id": str(registry_id),
            "project_id": None,
            "mode": "direct",
            "required_instance_id": None,
            "owner": "installed-routing-process-acceptance",
            "operation": "backend_http",
        },
    )


async def _stop(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


async def test_installed_entrypoint_replays_after_restart_and_api_never_falls_back(
    test_engine, monkeypatch, tmp_path
):
    await assert_owned_test_database(test_engine)
    python_value = os.environ.get("AAP_INFERENCE_ROUTING_INSTALLED_PYTHON")
    assert python_value, (
        "qualification requires a separately verified noneditable install"
    )
    python = Path(python_value).absolute()
    assert python.is_file() and ".worktree" in python.parts
    dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    assert dsn, "qualification requires the function-only routing LOGIN"
    owner = await _connect(test_engine)
    world = await _seed(owner)
    before = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) "
            "FROM aap_inference_routing_v1.command_receipts"
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    token = f"installed-routing-{uuid.uuid4().hex}"
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    environment.update(
        INFERENCE_ROUTING_DATABASE_URL=dsn,
        INFERENCE_ROUTING_TOKEN=token,
        INFERENCE_HOST="127.0.0.1",
        INFERENCE_PORT=str(port),
    )
    processes = []
    logs = []

    async def start():
        log = (tmp_path / f"owner-{len(processes)}.log").open("xb")
        logs.append(log)
        process = await asyncio.create_subprocess_exec(
            str(python),
            "-I",
            "-m",
            "aap_inference",
            cwd=tmp_path,
            env=environment,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        processes.append(process)
        async with httpx.AsyncClient(trust_env=False, timeout=0.5) as probe:
            for _ in range(100):
                assert process.returncode is None, (
                    "owned routing process exited before readiness"
                )
                try:
                    response = await probe.get(f"{url}/health")
                    if response.status_code == 200:
                        assert response.json() == {"status": "ready"}
                        return process
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        pytest.fail("owned installed routing process did not become ready")

    try:
        process = await start()
        remote = RoutingHttpClient(url, token, timeout_seconds=5)
        command = _acquire(world["registry_id"])
        first = await remote.execute(command)
        assert first.state == "committed"
        assert isinstance(first.result, RoutingScopeView)
        scope = first.result
        await remote.execute(
            make_routing_command(
                "heartbeat", {"scope_id": str(scope.id), "capability": scope.capability}
            )
        )
        observed = await remote.runtime(
            make_routing_runtime_request([scope.registry_id])
        )
        assert observed.registries[0].open_scopes == 1
        await _stop(process)
        assert process.returncode is not None

        for name, value in {
            "inference_routing_url": url,
            "inference_routing_token": token,
            "inference_routing_token_file": "",
            "inference_routing_timeout_seconds": 1,
        }.items():
            monkeypatch.setattr(settings, name, value)
        # A usable local engine exists. The explicit HTTP selection must still
        # fail when its owner stops, without creating a local replacement scope.
        scopes_before = await owner.fetchval(
            "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
            scope.registry_id,
        )
        with pytest.raises(RoutingUnavailable):
            await RoutingAuthority().acquire(registry_id=scope.registry_id)
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
                scope.registry_id,
            )
            == scopes_before
        )

        restarted = await start()
        assert restarted.pid != process.pid
        replay = await remote.execute(command)
        recovered = await remote.status(make_routing_status_request(command))
        assert replay == first and recovered == first
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
                scope.registry_id,
            )
            == 1
        )
        await remote.execute(
            make_routing_command(
                "close",
                {
                    "scope_id": str(scope.id),
                    "capability": scope.capability,
                    "outcome": "cancel",
                },
            )
        )

        semaphore = asyncio.Semaphore(4)

        async def sample():
            async with semaphore:
                began = time.perf_counter()
                reply = await remote.execute(_acquire(scope.registry_id))
                assert isinstance(reply.result, RoutingScopeView)
                reserved = reply.result
                await remote.execute(
                    make_routing_command(
                        "close",
                        {
                            "scope_id": str(reserved.id),
                            "capability": reserved.capability,
                            "outcome": "cancel",
                        },
                    )
                )
                return (time.perf_counter() - began) * 1000

        durations = await asyncio.gather(*(sample() for _ in range(16)))
        snapshot = await remote.runtime(
            make_routing_runtime_request([scope.registry_id])
        )
        assert snapshot.registries[0].open_scopes == 0
        assert (
            await owner.fetchval(
                "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE registry_id=$1",
                scope.registry_id,
            )
            == 17
        )
        result = {
            "scope": "installed owner Native admission and cancellation; no backend/GPU capacity claim",
            "requests": 16,
            "concurrency": 4,
            "duration_ms": durations,
            "process_restart_replay": True,
            "selected_owner_no_fallback": True,
        }
        (tmp_path / "routing-process-observations.json").write_text(json.dumps(result))
        evidence = os.environ.get("AAP_INFERENCE_ROUTING_PROCESS_EVIDENCE")
        if evidence:
            with Path(evidence).open("x") as stream:
                json.dump(result, stream, indent=2)
                stream.write("\n")
    finally:
        for process in reversed(processes):
            await _stop(process)
        for log in logs:
            log.close()
        try:
            after = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) "
                    "FROM aap_inference_routing_v1.command_receipts"
                )
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.command_receipts WHERE id=ANY($1::uuid[])",
                list(after - before),
            )
            await _cleanup(owner, world)
        finally:
            await owner.close()

"""Actual Native/owner HTTP/peer process proof of scoped backend retirement."""

import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import RoutingRejectedResult, RoutingUnavailable
from aap_inference_contract.routing_client import RoutingHttpClient
from aap_protocol_v2.scope_retirement import SCOPE_RETIREMENT_PATH
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _connect
from tests.test_inference_routing_owner_integration import (
    _available_port,
    _capture_subject,
    _cleanup_world,
    _command,
    _OwnerHttpProxy,
    _seed_world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable module-data environment",
    ),
]


async def _stop_process(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


@pytest.mark.parametrize(
    ("work_mode", "lost_ack"),
    (
        ("handler", False),
        ("handler", True),
        ("background", False),
        ("background", True),
    ),
)
async def test_retire_one_live_scope_preserves_another_and_fences_delayed_tokens(
    test_engine,
    tmp_path,
    work_mode,
    lost_ack,
    *,
    installed_python=None,
    retirement_authority_factory=None,
):
    await assert_owned_test_database(test_engine)
    dsn = os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"]
    registry_id, pool_id, member_id = uuid4(), uuid4(), uuid4()
    world = {"registry_id": registry_id, "pool_id": pool_id, "member_id": member_id}
    peer_port = _available_port()
    backend_token, owner_token = uuid4().hex, uuid4().hex
    gates = tmp_path / "gates"
    gates.mkdir()
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    environment.update(
        AAP_FIXTURE_ROUTE_TOKEN=backend_token,
        AAP_FIXTURE_REGISTRY_ID=str(registry_id),
        AAP_FIXTURE_PORT=str(peer_port),
        AAP_FIXTURE_GATES=str(gates),
    )
    peer_script = Path(__file__).parent / "fixtures/scope_retirement_peer.py"
    peer_log = (tmp_path / "scope-retirement-peer.log").open("xb")
    peer = native = subjects = subject_transaction = owner_process = None
    receipts = []
    proxy = _OwnerHttpProxy(peer_port, drop_path=SCOPE_RETIREMENT_PATH)
    runtime = server = None
    requests = []
    peer_base = f"http://127.0.0.1:{peer_port}"
    async with httpx.AsyncClient(trust_env=False, timeout=15) as http:
        try:
            native = await _connect(test_engine)
            peer = await asyncio.create_subprocess_exec(
                sys.executable,
                str(peer_script),
                cwd=tmp_path,
                env=environment,
                stdout=peer_log,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(15):
                while True:
                    assert peer.returncode is None, (
                        "owned peer stopped before readiness"
                    )
                    try:
                        if (await http.get(peer_base + "/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.025)
            backend_base = await proxy.start()
            owner_port = _available_port()
            owner_base = f"http://127.0.0.1:{owner_port}"
            await _seed_world(
                native,
                registry_id=registry_id,
                pool_id=pool_id,
                member_id=member_id,
                backend_url=backend_base,
                backend_token=backend_token,
            )
            owner_log = tmp_path / "installed-owner.log"

            async def start_installed_owner():
                owner_environment = {
                    key: environment[key]
                    for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
                    if key in environment
                }
                dsn_path, token_path = (
                    tmp_path / "routing-dsn",
                    tmp_path / "routing-token",
                )
                for secret_path, secret in ((dsn_path, dsn), (token_path, owner_token)):
                    secret_path.write_text(secret)
                    secret_path.chmod(0o600)
                owner_environment.update(
                    INFERENCE_HOST="127.0.0.1",
                    INFERENCE_PORT=str(owner_port),
                    INFERENCE_ROUTING_DATABASE_URL_FILE=str(dsn_path),
                    INFERENCE_ROUTING_TOKEN_FILE=str(token_path),
                )
                with owner_log.open("ab") as log:
                    process = await asyncio.create_subprocess_exec(
                        str(installed_python),
                        "-I",
                        "-m",
                        "aap_inference",
                        cwd=tmp_path,
                        env=owner_environment,
                        stdout=log,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                try:
                    async with asyncio.timeout(15):
                        while True:
                            assert process.returncode is None, (
                                "installed owner stopped before readiness"
                            )
                            try:
                                if (
                                    await http.get(owner_base + "/health")
                                ).status_code == 200:
                                    return process
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(0.025)
                except BaseException:
                    await _stop_process(process)
                    raise

            if installed_python is None:
                runtime = RoutingRuntime(dsn)
                assert await runtime.readiness()
                server = PipelineHttpServer(
                    manager=None,
                    data=None,
                    control_token=None,
                    host="127.0.0.1",
                    port=owner_port,
                    routing_manager=runtime.manager,
                    routing_control_token=owner_token,
                    scope_retirement_manager=runtime.scope_retirement_manager,
                )
                await server.start()
            else:
                assert Path(installed_python).is_absolute()
                owner_process = await start_installed_owner()
            routing_client = RoutingHttpClient(
                owner_base, owner_token, timeout_seconds=15
            )
            reconciled = await routing_client.execute(
                _command(
                    receipts,
                    "reconcile",
                    {"registry_id": str(registry_id), "control_id": str(uuid4())},
                )
            )
            assert reconciled.result.phase == "completed"
            original_epoch = reconciled.result.authority_epoch

            async def acquire(label):
                reply = await routing_client.execute(
                    _command(
                        receipts,
                        "acquire",
                        {
                            "pool_id": None,
                            "registry_id": str(registry_id),
                            "project_id": None,
                            "mode": "direct",
                            "required_instance_id": str(registry_id),
                            "owner": "scope-retirement:" + label,
                            "operation": "predict",
                        },
                    )
                )
                assert reply.state == "committed"
                return reply.result

            identity = {
                "url": backend_base,
                "auth_method": "token",
                "auth_token": backend_token,
                "gpu_resource_id": None,
                "extra_params": {"max_concurrency": 2},
            }

            async def begin_command(scope, issuer):
                attempt_id = uuid4()
                subject = await _capture_subject(
                    issuer,
                    scope_id=scope.id,
                    capability=scope.capability,
                    attempt_id=attempt_id,
                    identity=identity,
                )
                return _command(
                    receipts,
                    "begin",
                    {
                        "scope_id": str(scope.id),
                        "capability": scope.capability,
                        "attempt_id": str(attempt_id),
                        "method": "POST",
                        "path": "/predict",
                        "transport_subject": subject,
                    },
                )

            async def begin(scope):
                async with native.transaction():
                    command = await begin_command(scope, native)
                    return command, await routing_client.execute(command)

            old, other = await acquire("old"), await acquire("other")
            _, old_admission = await begin(old)
            _, unused_admission = await begin(old)
            _, other_admission = await begin(other)
            subjects = await _connect(test_engine)
            subject_transaction = subjects.transaction()
            await subject_transaction.start()
            pending_begin = await begin_command(old, subjects)
            for label, admitted, mode in (
                ("old", old_admission, work_mode),
                ("other", other_admission, "handler"),
            ):
                headers = {
                    **admitted.result.headers,
                    "Authorization": "Bearer " + backend_token,
                }
                requests.append(
                    asyncio.create_task(
                        http.post(
                            peer_base + "/predict",
                            headers=headers,
                            json={"label": label, "mode": mode},
                        )
                    )
                )
            async with asyncio.timeout(10):
                while True:
                    observation = (await http.get(peer_base + "/health")).json()
                    if set(observation["entered"]) == {"old", "other"}:
                        assert observation["active_requests"] == 2
                        break
                    await asyncio.sleep(0.025)
            # Lease expiry is not evidence that either process's work ended.
            await native.execute(
                "UPDATE aap_inference_routing_v1.scopes SET heartbeat_deadline="
                "pg_catalog.clock_timestamp()-interval '1 hour' WHERE id=$1",
                old.id,
            )
            retirement = {
                "schema": "aap-inference-scope-retirement/v1",
                "retirement_id": str(uuid4()),
                "scope_id": str(old.id),
                "capability": old.capability,
            }
            owner_headers = {"Authorization": "Bearer " + owner_token}
            retirement_authority = (
                retirement_authority_factory(owner_base, owner_token)
                if retirement_authority_factory is not None
                else None
            )

            async def retire(path="/v1/routes/retire"):
                if retirement_authority is not None:
                    reply = (
                        await retirement_authority.scope_retirement_status(retirement)
                        if path == "/v1/routes/retirement-status"
                        else await retirement_authority.retire_scope(retirement)
                    )
                    result = reply.model_dump(mode="json", by_alias=True)
                    assert backend_token not in json.dumps(result)
                    return result
                response = await http.post(
                    owner_base + path, json=retirement, headers=owner_headers
                )
                response.raise_for_status()
                result = response.json()
                assert backend_token not in response.text
                assert "capability" not in result
                return result

            proxy.drop_next_command_ack = lost_ack
            progress = await retire()
            if lost_ack:
                assert progress["state"] == "unknown"
                control_calls = proxy.counts[SCOPE_RETIREMENT_PATH]
                observed = await retire("/v1/routes/retirement-status")
                assert observed["result"]["phase"] != "completed"
                assert proxy.counts[SCOPE_RETIREMENT_PATH] == control_calls
                progress = await retire()
            assert progress["result"]["phase"] == "draining"
            assert await native.fetchval(
                "SELECT closed FROM aap_inference_routing_v1.scopes WHERE id=$1",
                old.id,
            )
            # This valid bearer was minted before closure and never reached the peer.
            denied = await http.post(
                peer_base + "/predict",
                headers={
                    **unused_admission.result.headers,
                    "Authorization": "Bearer " + backend_token,
                },
                json={"label": "delayed"},
            )
            assert denied.status_code == 401
            new_begin = await routing_client.execute(pending_begin)
            assert isinstance(new_begin.result, RoutingRejectedResult)
            assert new_begin.result.reason in {"scope_closed", "scope_unavailable"}
            await subject_transaction.rollback()
            subject_transaction = None
            await subjects.close(timeout=2)
            subjects = None
            assert not requests[0].done() and not requests[1].done()

            (gates / "old").touch()
            assert (await requests[0]).status_code == 200
            completed = await retire()
            assert completed["result"]["phase"] == "completed"
            assert (await retire())["result"] == completed["result"]
            assert (
                await native.fetchval(
                    "SELECT state FROM aap_inference_routing_v1.scopes WHERE id=$1",
                    old.id,
                )
                == "reconciled"
            )
            assert await native.fetchval(
                "SELECT bool_and(state='reconciled') FROM "
                "aap_inference_routing_v1.attempts WHERE scope_id=$1",
                old.id,
            )
            other_row = await native.fetchrow(
                "SELECT state,closed FROM aap_inference_routing_v1.scopes WHERE id=$1",
                other.id,
            )
            assert tuple(other_row) == ("active", False)
            assert not requests[1].done()
            observation = (await http.get(peer_base + "/health")).json()
            assert observation["phase"] == "bound"
            assert observation["active_requests"] == 1
            assert set(observation["entered"]) == {"old", "other"}
            assert (
                await native.fetchval(
                    "SELECT authority_epoch FROM aap_inference_routing_v1.registry_state "
                    "WHERE registry_id=$1",
                    registry_id,
                )
                == original_epoch
            )
            replacement = await acquire("new")
            assert replacement.id != old.id
            assert replacement.capability != old.capability
            _, replacement_admission = await begin(replacement)
            (gates / "new").touch()
            replacement_response = await http.post(
                peer_base + "/predict",
                headers={
                    **replacement_admission.result.headers,
                    "Authorization": "Bearer " + backend_token,
                },
                json={"label": "new"},
            )
            assert replacement_response.status_code == 200
            assert not requests[1].done()
            if owner_process is not None:
                previous_pid = owner_process.pid
                control_calls = proxy.counts[SCOPE_RETIREMENT_PATH]
                await _stop_process(owner_process)
                owner_process = None
                with pytest.raises(RoutingUnavailable):
                    await routing_client.scope_retirement_status(retirement)
                assert (
                    await native.fetchval(
                        "SELECT count(*) FROM aap_inference_routing_v1.scope_retirements "
                        "WHERE registry_id=$1",
                        registry_id,
                    )
                    == 1
                )
                owner_process = await start_installed_owner()
                assert owner_process.pid != previous_pid
                restarted = await routing_client.scope_retirement_status(retirement)
                assert restarted.state == "committed"
                assert restarted.result.phase == "completed"
                assert (
                    str(restarted.result.retirement_id) == retirement["retirement_id"]
                )
                assert proxy.counts[SCOPE_RETIREMENT_PATH] == control_calls
                assert not requests[1].done()
            assert not proxy.errors
        finally:
            cleanup_errors = []

            async def cleanup(label, action):
                try:
                    await action
                except BaseException as error:
                    cleanup_errors.append((label, type(error).__name__))

            for label in ("old", "other", "new", "delayed"):
                (gates / label).touch()
            if requests:
                await cleanup(
                    "requests",
                    asyncio.wait_for(
                        asyncio.gather(*requests, return_exceptions=True), timeout=20
                    ),
                )
            if server is not None:
                await cleanup("owner server", server.close())
            if runtime is not None:
                await cleanup("owner runtime", runtime.close())
            if owner_process is not None:
                await cleanup("installed owner", _stop_process(owner_process))
            for name in ("routing-dsn", "routing-token"):
                try:
                    (tmp_path / name).unlink(missing_ok=True)
                except OSError as error:
                    cleanup_errors.append((name, type(error).__name__))
            await cleanup("backend proxy", proxy.close())
            if peer is not None:
                await cleanup("backend process", _stop_process(peer))
            try:
                peer_log.close()
            except OSError as error:
                cleanup_errors.append(("peer log", type(error).__name__))
            if subject_transaction is not None:
                await cleanup("source transaction", subject_transaction.rollback())
            if subjects is not None:
                await cleanup("source connection", subjects.close(timeout=2))
            if native is not None:
                await cleanup(
                    "retirement rows",
                    native.execute(
                        "DELETE FROM aap_inference_routing_v1.scope_retirements "
                        "WHERE scope_id IN (SELECT id FROM aap_inference_routing_v1.scopes "
                        "WHERE registry_id=$1)",
                        registry_id,
                    ),
                )
                await cleanup("fixture world", _cleanup_world(native, world, receipts))
                await cleanup("owner connection", native.close(timeout=2))
            assert not cleanup_errors, cleanup_errors

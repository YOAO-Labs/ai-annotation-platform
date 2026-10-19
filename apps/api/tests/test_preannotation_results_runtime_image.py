"""Run the installed executor without application source or writable root storage."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from aap_preannotation_results import contract as c
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_batch_result_consumers_native import _ready, _world


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or not os.environ.get("AAP_PREANNOTATION_RESULT_RUNTIME_IMAGE"),
        reason="requires the owned runtime image and disposable boundary verifier",
    ),
]


def _docker(*args, **options):
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
        **options,
    )
    if result.returncode:
        pytest.fail("owned result-runtime container command failed", pytrace=False)
    return result.stdout.strip()


def _run_owned_image(
    image, identity, arguments, *, body=None, secret=None, python=False
):
    details = json.loads(_docker("image", "inspect", image))[0]
    assert details["Config"]["Labels"]["org.aap.acceptance-id"] == identity
    assert details["Config"]["User"] == "10004:10004"
    name = f"aap-result-runtime-{uuid4().hex}"
    options = [
        "create",
        "--interactive",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=32",
        "--memory=256m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "--label",
        f"org.aap.acceptance-id={identity}",
    ]
    if secret is not None:
        options.extend(
            (
                "--mount",
                f"type=bind,src={secret},dst=/run/secrets/results,readonly",
                "--env",
                "PREANNOTATION_RESULTS_DATABASE_URL_FILE=/run/secrets/results",
            )
        )
    if python:
        options.extend(("--entrypoint=python", "--network=none"))
    container_id = _docker(*options, details["Id"], *arguments)
    assert len(container_id) == 64 and all(
        char in "0123456789abcdef" for char in container_id
    )
    try:
        output = _docker("start", "--attach", "--interactive", container_id, input=body)
        container = json.loads(_docker("inspect", container_id))[0]
        assert container["State"]["ExitCode"] == 0
        assert container["HostConfig"]["ReadonlyRootfs"] is True
        assert all(mount["Destination"] != "/app" for mount in container["Mounts"])
        return output
    finally:
        container = json.loads(_docker("inspect", container_id))[0]
        assert container["Config"]["Labels"]["org.aap.acceptance-id"] == identity
        _docker("rm", "--force", container_id)


async def test_installed_read_only_runtime_claims_and_reads_the_saved_bundle(
    test_engine, tmp_path
):
    image = os.environ["AAP_PREANNOTATION_RESULT_RUNTIME_IMAGE"]
    identity = os.environ["AAP_PREANNOTATION_RESULT_RUNTIME_IDENTITY"]
    diagnostic = await asyncio.to_thread(
        _run_owned_image,
        image,
        identity,
        [
            "-I",
            "-c",
            (
                "import importlib.util,json,os,sys; from pathlib import Path; "
                "import aap_preannotation_results, aap_preannotation_results_runtime; "
                "assert os.getuid()==10004; "
                "assert Path(aap_preannotation_results.__file__).is_relative_to(sys.prefix); "
                "assert not Path('/app').exists(); "
                "assert all(importlib.util.find_spec(m) is None for m in "
                "('app','sqlalchemy','celery','redis','boto3')); "
                "print(json.dumps({'installed':True,'application_dependencies':False}))"
            ),
        ],
        python=True,
    )
    assert json.loads(diagnostic) == {
        "installed": True,
        "application_dependencies": False,
    }
    url = make_url(os.environ["AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"])
    assert (
        url.username == "preannotation_result_client"
        and url.database == test_engine.url.database
    )
    _verify_postgres(url)
    secret = Path(tmp_path / "result-dsn").resolve()
    secret.write_text(
        url.set(drivername="postgresql", host="host.docker.internal").render_as_string(
            hide_password=False
        ),
        encoding="utf-8",
    )
    secret.chmod(0o444)
    try:
        async with _world(test_engine, result_count=1) as world:
            _member, result, requests = await _ready(world)
            await world.execution.close()

            async def invoke(operation, request):
                raw = await asyncio.to_thread(
                    _run_owned_image,
                    image,
                    identity,
                    [operation],
                    body=request.model_dump_json(),
                    secret=secret,
                )
                return c.decode_reply(operation, request, raw)

            claim = await invoke(
                "claim_batch_result_execution",
                c.ClaimBatchResultExecutionRequest(
                    **world.execution.scope,
                    worker_id=uuid4(),
                    execution_id="installed-runtime",
                ),
            )
            saved = await invoke(
                "read_batch_result",
                c.ReadBatchResultRequest(
                    **world.execution.scope,
                    ordinal=1,
                    stage="prepared",
                ),
            )
            assert saved.result.result_id == result.result_id
            assert (
                saved.payload.prediction_id
                == requests[0].batch_result_identity.prediction_id
            )
            assert (
                saved.payload.prediction_created_at
                == requests[0].batch_result_identity.prediction_created_at
            )
            await invoke(
                "release_batch_result_execution",
                c.ReleaseBatchResultExecutionRequest(
                    **world.execution.scope,
                    generation=claim.lease.generation,
                    lease_token=claim.lease.lease_token,
                ),
            )
    finally:
        secret.chmod(0o600)
        secret.unlink()

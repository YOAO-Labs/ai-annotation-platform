"""A restricted native client can take over a saved Python result unchanged."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from aap_preannotation_results import contract as c
from app.application.durable_preannotation import _apply_ready_result
from app.application.preannotation_results import BatchResultExecution
from app.services.prediction import PredictionService
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_batch_result_consumers_native import _counts, _ready, _world


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


def _psql_peer(scope, claim, read):
    url = make_url(os.environ["AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"])
    assert url.username == "preannotation_result_client"
    _verify_postgres(url)
    environment = {**os.environ, "PGPASSWORD": url.password or ""}
    sql = r"""
BEGIN;
SELECT current_user || '|' || session_user || '|' || current_database();
SELECT aap_jobs_v1.claim_batch_result_execution(:'claim'::jsonb) AS claimed \gset
SELECT :'claimed';
SELECT aap_jobs_v1.read_batch_result_execution(
    :'scope'::jsonb || '{"after_ordinal":0,"limit":100}'::jsonb
);
SELECT aap_labeling_v1.read_batch_result(:'read'::jsonb);
SELECT aap_jobs_v1.release_batch_result_execution(
    :'scope'::jsonb || pg_catalog.jsonb_build_object(
        'generation', (:'claimed'::jsonb) #> '{data,lease,generation}',
        'lease_token', (:'claimed'::jsonb) #> '{data,lease,lease_token}'
    )
);
COMMIT;
"""
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        "PGPASSWORD",
        os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"],
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        url.username,
        "-d",
        url.database,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for name, value in (("scope", scope), ("claim", claim), ("read", read)):
        command.extend(("-v", f"{name}={json.dumps(value, default=str)}"))
    result = subprocess.run(
        [*command, "-f", "-"],
        input=sql,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        pytest.fail("restricted batch-result psql peer failed", pytrace=False)
    return result.stdout.splitlines(), url.database


async def test_native_peer_takes_over_saved_results_without_changing_write_identity(
    test_engine,
):
    async with _world(test_engine) as world:
        member, result, requests = await _ready(world)
        expected = [request.batch_result_identity for request in requests]
        generation = world.execution.lease.generation
        await world.execution.close()
        scope = world.execution.scope
        claim = c.ClaimBatchResultExecutionRequest(
            **scope, worker_id=uuid4(), execution_id="native-psql-peer"
        ).model_dump(mode="json")
        read = {
            **scope,
            "ordinal": 1,
            "stage": "prepared",
            "request_index": 0,
            "part_index": None,
        }
        lines, database = await asyncio.to_thread(_psql_peer, scope, claim, read)
        assert (
            lines[0]
            == f"preannotation_result_client|preannotation_result_client|{database}"
        )
        assert len(lines) == 5
        claimed = c.decode_reply(
            "claim_batch_result_execution",
            c.ClaimBatchResultExecutionRequest.model_validate(claim),
            json.loads(lines[1]),
        )
        assert claimed.lease.generation == generation + 1
        assert claimed.progress.success_count == 0
        progress = c.decode_reply(
            "read_batch_result_execution",
            c.ReadBatchResultExecutionRequest.model_validate(scope),
            json.loads(lines[2]),
        )
        assert progress.members[0].state == "ready"
        saved = c.decode_reply(
            "read_batch_result",
            c.ReadBatchResultRequest.model_validate(read),
            json.loads(lines[3]),
        )
        assert saved.result.result_id == result.result_id
        assert saved.payload.prediction_id == expected[0].prediction_id
        assert saved.payload.prediction_created_at == expected[0].prediction_created_at
        release = c.ReleaseBatchResultExecutionRequest(
            **scope,
            generation=claimed.lease.generation,
            lease_token=claimed.lease.lease_token,
        )
        c.decode_reply("release_batch_result_execution", release, json.loads(lines[4]))

        successor = BatchResultExecution(
            world.request,
            world.writers,
            execution_id="python-after-native",
            executor_session_factory=world.executors,
        )
        try:
            await successor.claim()
            resumed_result, resumed_requests = await successor.load(1, stage="prepared")
            assert [
                request.batch_result_identity for request in resumed_requests
            ] == expected
            async with world.writers() as db:
                await _apply_ready_result(
                    successor,
                    db,
                    PredictionService(db),
                    member,
                    resumed_result,
                    resumed_requests,
                    manifest_backed=False,
                )
            assert await _counts(world) == (2, 2, 0, 2)
        finally:
            await successor.close()

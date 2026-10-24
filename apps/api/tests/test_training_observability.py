"""Observation isolation, replay, bounded retention and safe historical downloads."""

import copy
import hashlib
import io
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from training_contract.observability import METRICS, encoded, measurement, utc_now

from app.config import settings
from app.db.models.training_observability import (
    TrainingLogChunk,
    TrainingTelemetrySample,
)
from app.db.models.training_run import TrainingAttempt, TrainingResourceLease
from app.schemas.training_observability import (
    ExecutorObservations,
    LogPage,
    TelemetryPage,
)
from app.schemas.training_run import ExecutorFinish
from app.services import training_observability as observations
from app.services import training_runs as training
from app.services import training_run_storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_project
from tests import test_training_runs as training_fixtures
from tests.test_training_runs import (
    TOKEN,
    claimed_run,
    fence,
    ready_dataset,
)

training_config = training_fixtures.training_config


def telemetry(sequence, boot):
    return {
        "sequence": sequence,
        "producer_boot_id": str(boot),
        "captured_at": utc_now(),
        "phase": "training",
        "phase_elapsed_seconds": 0,
        "metrics": {key: measurement(0) for key in METRICS},
        "progress": {},
    }


def batch(claim, assignment, *, stream=None, samples=(), logs=(), skipped=None):
    return ExecutorObservations(
        **fence(claim, assignment),
        stream_id=stream or uuid.uuid4(),
        request_id=uuid.uuid4(),
        producer_boot_id=claim.boot_id,
        telemetry=list(samples),
        logs=list(logs),
        skipped=skipped or {},
        summary={"protocol": 1, "peaks": {}, "latest": None},
    )


async def test_observations_replay_independently_without_renewing_liveness_or_advancing_status(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    lease = await db_session.get(TrainingResourceLease, run.config["resource_id"])
    before = (
        run.status,
        attempt.status,
        attempt.heartbeat_at,
        lease.last_seen_at,
        lease.updated_at,
        lease.attempt_id,
    )
    body = batch(claim, assignment, samples=[telemetry(1, claim.boot_id)])
    first = await observations.ingest(db_session, attempt.id, body)
    assert await observations.ingest(db_session, attempt.id, body) == first
    assert (
        run.status,
        attempt.status,
        attempt.heartbeat_at,
        lease.last_seen_at,
        lease.updated_at,
        lease.attempt_id,
    ) == before
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TrainingTelemetrySample)
        )
        == 1
    )
    payload = await observations.page(db_session, attempt, "telemetry")
    page = TelemetryPage.model_validate(payload)
    assert page.items[0].metrics["gpu_utilization"].value == 0
    assert page.metric_definitions["gpu_utilization"]["scope"] == "physical_device"
    assert page.metric_definitions["rss_bytes"]["scope"] == "process_group_sum"
    conflicting = body.model_copy(deep=True)
    conflicting.telemetry[0].phase_elapsed_seconds = 1
    with pytest.raises(HTTPException) as error:
        await observations.ingest(db_session, attempt.id, conflicting)
    assert error.value.detail["code"] == "observation_request_conflict"
    wrong_boot = body.model_copy(update={"boot_id": uuid.uuid4()})
    with pytest.raises(HTTPException) as error:
        await observations.ingest(db_session, attempt.id, wrong_boot)
    assert error.value.detail["code"] == "stale_attempt"
    forged = body.model_dump(mode="json")
    forged["request_id"] = str(uuid.uuid4())
    forged["telemetry"] = []
    forged["logs"] = [
        {
            "sequence": 1,
            "producer_boot_id": str(claim.boot_id),
            "captured_at": utc_now(),
            "text": "safe\n",
        }
    ]
    forged["summary"] = {
        "protocol": 1,
        "peaks": {"rss_bytes": 999999},
        "latest": {
            **body.telemetry[0].model_dump(mode="json"),
            "phase": "uploading",
            "producer_boot_id": str(uuid.uuid4()),
            "progress": {"total_epochs": 1000, "completed_epoch": 1000},
        },
    }
    await observations.ingest(
        db_session, attempt.id, ExecutorObservations.model_validate(forged)
    )
    accepted = attempt.observation_state["summary"]
    assert accepted["latest"]["phase"] == "training"
    assert accepted["latest"]["producer_boot_id"] == str(claim.boot_id)
    assert accepted["latest"]["progress"]["total_epochs"] is None
    assert accepted["peaks"]["rss_bytes"] == 0


async def test_restart_producer_identity_and_independent_log_cursor_with_secondary_redaction(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    body = batch(claim, assignment, samples=[telemetry(1, claim.boot_id)])
    await observations.ingest(db_session, run.current_attempt_id, body)
    producer = uuid.uuid4()
    log = {
        "sequence": 1,
        "producer_boot_id": str(producer),
        "captured_at": utc_now(),
        "text": f'{{"password": "secret-value"}} https://store/file?X-Amz-Signature=signed {TOKEN} /state/private\n',
    }
    second = batch(claim, assignment, stream=body.stream_id, logs=[log])
    second.producer_boot_id = producer
    ack = await observations.ingest(db_session, run.current_attempt_id, second)
    assert ack["telemetry_cursor"] == 1 and ack["logs_cursor"] == 1
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    page = LogPage.model_validate(await observations.page(db_session, attempt, "logs"))
    assert page.items[0].producer_boot_id == producer
    for secret in ("secret-value", "signed", TOKEN, "/state"):
        assert secret not in page.items[0].text
    skipped = batch(
        claim,
        assignment,
        stream=body.stream_id,
        skipped={"logs": {"after": 1, "through": 10}},
    )
    await observations.ingest(db_session, attempt.id, skipped)
    tail = await observations.page(db_session, attempt, "logs", after=1)
    assert tail["items"] == [] and tail["next_cursor"] == 10
    assert not tail["has_more"] and tail["latest_cursor"] == 10
    old = batch(
        claim, assignment, stream=uuid.uuid4(), samples=[telemetry(2, producer)]
    )
    with pytest.raises(HTTPException) as error:
        await observations.ingest(db_session, attempt.id, old)
    assert error.value.detail["code"] == "observation_stream_conflict"


async def test_bounded_windows_keep_high_watermarks_gaps_and_hourly_cleanup_summary(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    stream = uuid.uuid4()
    for offset in range(61):
        samples = [
            telemetry(offset * 12 + index + 1, claim.boot_id) for index in range(12)
        ]
        logs = [
            {
                "sequence": offset * 12 + index + 1,
                "producer_boot_id": str(claim.boot_id),
                "captured_at": utc_now(),
                "text": hashlib.shake_256(f"log-{offset}-{index}".encode()).hexdigest(
                    1024
                ),
            }
            for index in range(12)
        ]
        await observations.ingest(
            db_session,
            run.current_attempt_id,
            batch(claim, assignment, stream=stream, samples=samples, logs=logs),
        )
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    for model, count in ((TrainingTelemetrySample, 720), (TrainingLogChunk, 512)):
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.attempt_id == attempt.id)
            )
            == count
        )
    page = await observations.page(db_session, attempt, "telemetry", after=0)
    assert page["earliest_cursor"] == 13 and page["latest_cursor"] == 732
    assert page["gaps"][-1] == {"after": 0, "through": 12, "reason": "retention"}
    with pytest.raises(HTTPException) as error:
        await observations.ingest(
            db_session,
            attempt.id,
            batch(
                claim, assignment, stream=stream, samples=[telemetry(1, claim.boot_id)]
            ),
        )
    assert error.value.detail["code"] == "observation_cursor_retired"
    payload_bytes = await db_session.scalar(
        text(
            "select coalesce(sum(pg_column_size(payload)),0) from training_telemetry_samples where attempt_id=:attempt"
        ),
        {"attempt": attempt.id},
    )
    log_bytes = await db_session.scalar(
        text(
            "select coalesce(sum(pg_column_size(payload)),0) from training_log_chunks where attempt_id=:attempt"
        ),
        {"attempt": attempt.id},
    )
    print(
        f"Bounded PostgreSQL payload storage: telemetry={payload_bytes} bytes, logs={log_bytes} bytes"
    )
    relations = (
        await db_session.execute(
            text(
                "select pg_total_relation_size('training_telemetry_samples'),pg_total_relation_size('training_log_chunks')"
            )
        )
    ).one()
    print(
        f"PostgreSQL tables including indexes/TOAST/free pages: telemetry={relations[0]} bytes, logs={relations[1]} bytes"
    )
    assert len(encoded(attempt.observation_state)) < 16 * 1024
    old = training.now() - timedelta(days=8)
    for model, _ in observations.STREAMS.values():
        rows = (
            await db_session.scalars(
                select(model).where(model.attempt_id == attempt.id)
            )
        ).all()
        for row in rows:
            row.received_at = old
    await db_session.flush()
    assert await observations.cleanup(db_session) == {"telemetry": 720, "logs": 512}
    empty = await observations.page(db_session, attempt, "logs", after=0)
    assert (
        empty["latest_cursor"] == empty["next_cursor"] == 732 and empty["items"] == []
    )
    following = batch(
        claim,
        assignment,
        stream=stream,
        samples=[telemetry(740, claim.boot_id)],
        skipped={"telemetry": {"after": 732, "through": 739}},
    )
    assert (await observations.ingest(db_session, attempt.id, following))[
        "telemetry_cursor"
    ] == 740
    assert (await observations.page(db_session, attempt, "telemetry"))["gaps"][-1][
        "reason"
    ] == "producer_retention"


async def test_public_scope_authentication_and_final_tail_are_attempt_owned(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    internal = (
        f"/api/v1/internal/training/attempts/{run.current_attempt_id}/observations"
    )
    body = batch(claim, assignment, samples=[telemetry(1, claim.boot_id)])
    assert (
        await httpx_client.post(internal, json=body.model_dump(mode="json"))
    ).status_code == 403
    response = await httpx_client.post(
        internal,
        headers={"X-Training-Executor-Token": TOKEN},
        json=body.model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    other = await create_project(db_session, owner_id=owner.id)
    path = f"/api/v1/projects/{other.id}/training-runs/{run.id}/attempts/{run.current_attempt_id}/telemetry"
    assert (
        await httpx_client.get(path, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 404
    await training.finish(
        db_session,
        run.current_attempt_id,
        ExecutorFinish(
            **fence(claim, assignment), status="failed", process_exited=True
        ),
    )
    tail = batch(
        claim, assignment, stream=body.stream_id, samples=[telemetry(2, claim.boot_id)]
    )
    assert (await observations.ingest(db_session, run.current_attempt_id, tail))[
        "telemetry_cursor"
    ] == 2


def test_schema_bounds_reject_boolean_nonfinite_invalid_units_and_encoded_expansion():
    claim = SimpleNamespace(executor_id="owned", boot_id=uuid.uuid4())
    assignment = {"lease_token": "a" * 64}
    payload = batch(
        claim, assignment, samples=[telemetry(1, claim.boot_id)]
    ).model_dump(mode="json")
    for value in (True, float("nan"), -1):
        bad = copy.deepcopy(payload)
        bad["telemetry"][0]["metrics"]["rss_bytes"]["value"] = value
        with pytest.raises(ValidationError):
            ExecutorObservations.model_validate(bad)
    payload["telemetry"][0]["metrics"]["rss_bytes"]["unit"] = "MiB"
    with pytest.raises(ValidationError):
        ExecutorObservations.model_validate(payload)
    with pytest.raises(ValidationError):
        batch(
            claim,
            assignment,
            logs=[
                {
                    "sequence": index + 1,
                    "producer_boot_id": str(claim.boot_id),
                    "captured_at": utc_now(),
                    "text": "\t" * 2048,
                }
                for index in range(32)
            ],
        )


def test_legacy_finish_digest_preserves_existing_nulls_and_structured_diagnostic_is_optional():
    body = ExecutorFinish(
        executor_id="owned",
        boot_id=uuid.uuid4(),
        lease_token="a" * 64,
        status="failed",
        process_exited=True,
    )
    legacy = body.model_dump(mode="json")
    legacy.pop("failure")
    legacy.pop("native_accounting")
    assert (
        body.request_payload() == legacy
        and "error_message" in legacy
        and legacy["error_message"] is None
    )
    assert content_digest(body.request_payload()) == content_digest(legacy)


def test_historical_log_is_verified_and_downloads_content_addressed_redacted_derivative(
    monkeypatch,
):
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    bucket = observations.storage.training_bucket
    run.storage_ref = {
        "schema": 1,
        "purpose": "training-run",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": training_run_storage.prefix_for(run.project_id, run.id),
        "storage_identity": managed_storage_identity(endpoint, bucket),
        "origin": "new",
        "evidence": None,
    }
    attempt = SimpleNamespace(id=uuid.uuid4(), run_id=run.id)
    raw = b'{"password": "secret-value"} https://storage.test/file?X-Amz-Signature=signed /host/private\n'
    original_sha = hashlib.sha256(raw).hexdigest()
    key = f"projects/{run.project_id}/runs/{run.id}/attempts/{attempt.id}/train.log"
    artifact = {
        "key": key,
        "size_bytes": len(raw),
        "sha256": original_sha,
        "role": "log",
    }
    written = []
    monkeypatch.setattr(
        observations.storage.client,
        "get_object",
        lambda **kwargs: {
            "ContentLength": len(raw),
            "Metadata": {"sha256": original_sha, "attempt-id": str(attempt.id)},
            "Body": io.BytesIO(raw),
        },
    )
    monkeypatch.setattr(
        observations.storage.client,
        "put_object",
        lambda **kwargs: written.append(kwargs),
    )
    result = observations.redacted_log(run, attempt, "train.log", artifact)
    assert result["source_sha256"] == original_sha and result["sha256"] != original_sha
    assert written[0]["Key"] != key and written[0]["Key"].startswith(
        key.rsplit("/", 1)[0] + "/redacted-logs/"
    )
    assert (
        written[0]["Metadata"]["sha256"]
        == hashlib.sha256(written[0]["Body"]).hexdigest()
    )
    for secret in (b"secret-value", b"signed", b"/host", b"storage.test"):
        assert secret not in written[0]["Body"]
    assert (
        observations.redacted_log(run, attempt, "train.log", artifact)["key"]
        == result["key"]
    )
    artifact["sha256"] = "0" * 64
    with pytest.raises(HTTPException):
        observations.redacted_log(run, attempt, "train.log", artifact)
    assert len(written) == 2


async def test_recovery_uses_current_checkpoint_metadata_and_known_runtime(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    monkeypatch,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    attempt = await training.finish(
        db_session,
        run.current_attempt_id,
        ExecutorFinish(
            **fence(claim, assignment), status="failed", process_exited=True
        ),
    )
    assert not (await observations.recovery(db_session, run))["eligible"]
    checkpoint = {
        "role": "checkpoint",
        "epoch": 1,
        "key": f"projects/{project.id}/runs/{run.id}/attempts/{attempt.id}/checkpoint.pt",
        "sha256": "1" * 64,
        "size_bytes": 10,
    }
    attempt.artifacts = {"checkpoint.pt": checkpoint}
    monkeypatch.setattr(
        observations.storage.client,
        "head_object",
        lambda **kwargs: {
            "ContentLength": 10,
            "Metadata": {"attempt-id": str(attempt.id), "sha256": "1" * 64},
        },
    )
    assert (await observations.recovery(db_session, run))["eligible"]
    attempt.artifacts = {"checkpoint.pt": {**checkpoint, "key": "other/checkpoint.pt"}}
    assert (
        "checkpoint_unavailable"
        in (await observations.recovery(db_session, run))["reasons"]
    )
    attempt.artifacts = {"checkpoint.pt": checkpoint}
    lease = await db_session.get(TrainingResourceLease, run.config["resource_id"])
    lease.last_seen_at = training.now() - timedelta(minutes=1)
    offline = await observations.recovery(db_session, run)
    assert offline["eligible"] and not offline["executor_online"]
    lease.runtime = {}
    assert (
        "runtime_unknown" in (await observations.recovery(db_session, run))["reasons"]
    )
    lease.runtime = claim.runtime
    monkeypatch.setattr(
        observations.storage.client,
        "head_object",
        lambda **kwargs: {"ContentLength": 11, "Metadata": {}},
    )
    assert (
        "checkpoint_unavailable"
        in (await observations.recovery(db_session, run))["reasons"]
    )


async def test_structured_failure_is_bound_sanitized_and_exactly_replayable(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    body = ExecutorFinish(
        **fence(claim, assignment),
        status="failed",
        process_exited=True,
        error_message=f"{TOKEN} /host/private",
        failure={
            "code": "cuda_oom",
            "stage": "training",
            "config_digest": run.config_digest,
            "diagnostic": f'{{"password": "private"}} {assignment["lease_token"]} https://storage/file?signed=value',
            "source": "native",
            "exception_type": "OutOfMemoryError",
        },
    )
    attempt = await training.finish(db_session, run.current_attempt_id, body)
    assert (
        attempt.failure["code"] == "cuda_oom" and attempt.failure["source"] == "native"
    )
    for secret in (TOKEN, "private", assignment["lease_token"], "signed=value"):
        assert secret not in str(attempt.failure) + str(attempt.error_message)
    assert await training.finish(db_session, attempt.id, body) is attempt
    changed = body.model_copy(deep=True)
    changed.failure.code = "native_exception"
    with pytest.raises(HTTPException) as error:
        await training.finish(db_session, attempt.id, changed)
    assert error.value.status_code == 409


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_optional_summary_identity_is_verified_without_blocking_failed_completion(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    monkeypatch,
    status,
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, claim, assignment = await claimed_run(
        db_session, httpx_client, token, project, dataset
    )
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    attempt.upload_inventory = {
        name: {"name": name, "role": role, "sha256": "a" * 64, "size_bytes": 10}
        for name, role in (
            ("best.pt", "weights"),
            ("checkpoint.pt", "checkpoint"),
            ("metrics.json", "metrics"),
            ("config.json", "config"),
            ("train.log", "log"),
            ("observability.json", "telemetry"),
        )
    }
    monkeypatch.setattr(training, "verify_artifact", lambda *args, bucket: None)
    summary = {
        "protocol": 1,
        "run_id": str(run.id),
        "attempt_id": str(uuid.uuid4()),
        "config_digest": run.config_digest,
    }
    monkeypatch.setattr(
        observations.storage.client,
        "get_object",
        lambda **kwargs: {"Body": io.BytesIO(json.dumps(summary).encode())},
    )
    body = ExecutorFinish(
        **fence(claim, assignment), status=status, process_exited=True
    )
    if status == "succeeded":
        with pytest.raises(HTTPException) as error:
            await training.finish(db_session, attempt.id, body)
        assert error.value.status_code == 422
        summary["attempt_id"] = str(attempt.id)
    completed = await training.finish(db_session, attempt.id, body)
    assert completed.status == status
    assert ("observability.json" in completed.artifacts) is (status == "succeeded")


@pytest.mark.parametrize("corrupt", [False, True])
async def test_derived_log_download_serializes_deletion_and_observation_cascade(
    test_engine, monkeypatch, corrupt
):
    import asyncio
    import threading
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.api.v1.training_runs import download_artifact, delete_run
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingRun
    from tests.factory import create_user

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    raw = b"password=private https://storage/file?signed=value /host/private\n"
    async with maker() as seed:
        owner = await create_user(
            seed, "super_admin", f"log-race-{uuid.uuid4()}@test.local", "Log race"
        )
        project, dataset = await ready_dataset(seed, owner)
        run = TrainingRun(
            project_id=project.id,
            dataset_id=dataset.id,
            creator_id=owner.id,
            name="Log race",
            status="failed",
            config={},
            config_digest="a" * 64,
            request_id=uuid.uuid4(),
            request_digest="b" * 64,
        )
        seed.add(run)
        await seed.flush()
        await training_run_storage.allocate(seed, run)
        attempt = TrainingAttempt(run_id=run.id, ordinal=1, status="failed")
        seed.add(attempt)
        await seed.flush()
        artifact = {
            "role": "log",
            "key": f"projects/{project.id}/runs/{run.id}/attempts/{attempt.id}/train.log",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
        attempt.artifacts = {"train.log": artifact}
        run.current_attempt_id = attempt.id
        for model, _ in observations.STREAMS.values():
            seed.add(
                model(
                    attempt_id=attempt.id,
                    sequence=1,
                    stream_id=uuid.uuid4(),
                    producer_boot_id=uuid.uuid4(),
                    captured_at=training.now(),
                    content_digest="c" * 64,
                    payload={},
                )
            )
        await seed.commit()
    entered, release = threading.Event(), threading.Event()
    writes, signs, deletions = [], [], []
    delete_pid = None

    def read(**kwargs):
        entered.set()
        assert release.wait(10)
        return {
            "ContentLength": len(raw),
            "Body": io.BytesIO(raw),
            "Metadata": {
                "attempt-id": str(attempt.id),
                "sha256": "0" * 64 if corrupt else artifact["sha256"],
            },
        }

    monkeypatch.setattr(observations.storage.client, "get_object", read)
    monkeypatch.setattr(
        observations.storage.client,
        "put_object",
        lambda **kwargs: writes.append(kwargs),
    )
    monkeypatch.setattr(
        observations.storage,
        "generate_download_url",
        lambda key, **kwargs: signs.append(key) or "https://download/derived",
    )
    monkeypatch.setattr(
        observations.storage,
        "delete_prefix",
        lambda prefix, **kwargs: deletions.append(prefix),
    )

    async def reading():
        async with maker() as db:
            try:
                return await download_artifact(
                    run.id, attempt.id, "train.log", project, owner, db
                )
            except HTTPException as error:
                return error

    async def deleting():
        nonlocal delete_pid
        async with maker() as db:
            delete_pid = await db.scalar(text("select pg_backend_pid()"))
            await delete_run(run.id, project, owner, db)

    read_task = asyncio.create_task(reading())
    delete_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        delete_task = asyncio.create_task(deleting())

        async def blocked():
            async with maker() as observer:
                while delete_pid is None or not await observer.scalar(
                    text("select cardinality(pg_blocking_pids(:pid)) > 0"),
                    {"pid": delete_pid},
                ):
                    await asyncio.sleep(0.01)

        await asyncio.wait_for(blocked(), 5)
        assert not deletions and not delete_task.done()
        release.set()
        result = await asyncio.wait_for(read_task, 5)
        await asyncio.wait_for(delete_task, 5)
        if corrupt:
            assert isinstance(result, HTTPException) and result.status_code == 409
            assert not writes and not signs
        else:
            assert result["source_sha256"] == artifact["sha256"]
            assert signs == [writes[0]["Key"]] and "/redacted-logs/" in signs[0]
            assert b"private" not in writes[0]["Body"]
        assert deletions == [f"projects/{project.id}/runs/{run.id}/"]
        async with maker() as verify:
            for model, _ in observations.STREAMS.values():
                assert (
                    await verify.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.attempt_id == attempt.id)
                    )
                    == 0
                )
    finally:
        release.set()
        await asyncio.gather(
            read_task, *([delete_task] if delete_task else []), return_exceptions=True
        )
        async with maker() as cleanup:
            await cleanup.execute(delete(TrainingRun).where(TrainingRun.id == run.id))
            await cleanup.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.id == dataset.id
                )
            )
            await cleanup.execute(delete(Project).where(Project.id == project.id))
            await cleanup.execute(delete(User).where(User.id == owner.id))
            await cleanup.commit()

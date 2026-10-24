"""Finite plans, fenced budget settlement, serial admission and final evidence."""

import asyncio
import copy
import hashlib
import io
import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from training_contract.budget import POLICY_DIGEST, receipt
from training_contract.catalog import supported_identities

from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingResourceLease,
    TrainingRun,
)
from app.db.models.training_study import TrainingStudy, TrainingTrial
from app.db.models.user import User
from app.schemas.training_run import ExecutorFinish
from app.schemas.training_study import TrainingStudyCreate
from app.services import training_studies as studies, training_runs as runs
from app.services.training_dataset_bridge import content_digest
from tests.conftest import _create_user
from tests.test_training_parameters import configuration
from tests.test_training_runs import ready_dataset, executor_claim, fence, create_run
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401
from tests.test_training_evaluations import evidence

pytestmark = pytest.mark.usefixtures("training_config_fixture")


def body(dataset, **options):
    return {
        "base": {
            "name": "Bounded search",
            "dataset_id": str(dataset.id),
            "request_id": str(uuid.uuid4()),
            "configuration": configuration(epochs=3, optimizer="SGD"),
        },
        "grid": {"lr0": [0.01, 0.02]},
        "objective": "metrics/mAP50-95",
        "direction": "maximize",
        "budgets": {
            "max_trials": 2,
            "max_planned_epochs": 6,
            "max_native_seconds": 10.0,
        },
        **options,
    }


def native_claim(*, qualified=True):
    claim = executor_claim()
    claim.runtime.update(executor_protocol=2, supported_configs=supported_identities())
    if qualified:
        claim.runtime["native_budget_policy_digest"] = POLICY_DIGEST
    return claim


async def setup(db, client, owner, token, **options):
    project, dataset = await ready_dataset(db, owner)
    path = f"/api/v1/projects/{project.id}/training-studies"
    headers = {"Authorization": "Bearer " + token}
    payload = body(dataset, **options)
    preview = await client.post(path + ":preview", headers=headers, json=payload)
    assert preview.status_code == 200, preview.text
    payload["preview_digest"] = preview.json()["preview_digest"]
    response = await client.post(path, headers=headers, json=payload)
    assert response.status_code == 202, response.text
    study = await db.get(TrainingStudy, uuid.UUID(response.json()["id"]))
    return project, dataset, study, path, headers, payload, response.json()


async def finish(
    db,
    claim,
    assignment,
    *,
    seconds=1.0,
    state="exact",
    status="failed",
    deadline=False,
):
    accounting = receipt(
        assignment["native_budget"],
        state,
        seconds=seconds if state != "unknown" else None,
        exit_code=1 if state == "exact" else None,
        deadline=deadline,
        reason="deadline" if deadline else None,
    )
    return await runs.finish(
        db,
        uuid.UUID(assignment["attempt_id"]),
        ExecutorFinish(
            **fence(claim, assignment),
            status=status,
            process_exited=True,
            native_accounting=accounting,
        ),
    )


async def test_preview_is_complete_start_is_idempotent_and_child_is_ordinary(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    _, _, study, path, headers, payload, result = await setup(
        db_session, httpx_client, owner, token
    )
    assert [trial["values"] for trial in result["trials"]] == [
        {"lr0": 0.01},
        {"lr0": 0.02},
    ]
    assert (
        result["plan"]["planned_epochs"] == 6
        and result["accounting"]["reserved_seconds"] == 10
    )
    assert [trial["status"] for trial in result["trials"]] == ["queued", "pending"]
    repeated = await httpx_client.post(path, headers=headers, json=payload)
    assert repeated.status_code == 202 and repeated.json()["id"] == str(study.id)
    changed = copy.deepcopy(payload)
    changed["budgets"]["max_native_seconds"] = 9.0
    assert (
        await httpx_client.post(path, headers=headers, json=changed)
    ).status_code == 409
    for _ in range(3):
        await studies.coordinate(db_session, study.id)
    assert len(await studies.children(db_session, study.id)) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TrainingTrial)
            .where(TrainingTrial.study_id == study.id)
        )
        == 2
    )


@pytest.mark.parametrize(
    "change",
    [
        {"grid": {"lr0": [0.01, 0.02, 0.03]}},
        {
            "budgets": {
                "max_trials": 2,
                "max_planned_epochs": 5,
                "max_native_seconds": 10.0,
            }
        },
        {"grid": {"lr0": [0.01, 0.01]}},
        {"grid": {"lr0": [True, 0.01]}},
        {"grid": {"device": [0]}},
        {
            "grid": {"warmup_momentum": [0.5]},
            "base_values": {"epochs": 3, "optimizer": "AdamW"},
        },
        {"grid": {"epochs": [1], "warmup_epochs": [3]}},
        {"base_model": "yolo8n"},
        {"objective": "metrics/mAP75"},
        {"objective": "metrics/AR100"},
    ],
)
async def test_invalid_or_over_budget_grid_is_rejected_without_any_study(
    db_session, super_admin, httpx_client, change
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    payload = body(dataset)
    payload.update(
        {
            key: value
            for key, value in change.items()
            if key not in {"base_values", "base_model"}
        }
    )
    if "base_model" in change:
        payload["base"]["base_model"] = change["base_model"]
    if "base_values" in change:
        payload["base"]["configuration"] = configuration(**change["base_values"])
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-studies:preview",
        headers={"Authorization": "Bearer " + token},
        json=payload,
    )
    assert response.status_code == 422, response.text
    assert not await db_session.scalar(
        select(TrainingStudy.id).where(TrainingStudy.project_id == project.id)
    )


async def test_preview_changes_require_review_and_old_executor_skips_budget_tasks(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    path = f"/api/v1/projects/{project.id}/training-studies"
    headers = {"Authorization": "Bearer " + token}
    payload = body(dataset)
    preview = await httpx_client.post(path + ":preview", headers=headers, json=payload)
    payload["preview_digest"] = preview.json()["preview_digest"]
    payload["grid"]["lr0"][0] = 0.03
    assert (
        await httpx_client.post(path, headers=headers, json=payload)
    ).status_code == 409
    payload["grid"]["lr0"][0] = 0.01
    created = await httpx_client.post(path, headers=headers, json=payload)
    assert created.status_code == 202
    assert await runs.claim(db_session, native_claim(qualified=False)) is None
    claim = native_claim()
    assignment = await runs.claim(db_session, claim)
    replay = await runs.claim(db_session, claim)
    assert (
        replay["native_budget"] == assignment["native_budget"]
        and replay["attempt_id"] == assignment["attempt_id"]
    )


async def test_fenced_accounting_is_mandatory_immutable_and_releases_only_known_balance(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    _, _, study, _, _, _, _ = await setup(db_session, httpx_client, owner, token)
    claim = native_claim()
    assignment = await runs.claim(db_session, claim)
    with pytest.raises(HTTPException) as absent:
        await runs.finish(
            db_session,
            uuid.UUID(assignment["attempt_id"]),
            ExecutorFinish(
                **fence(claim, assignment), status="failed", process_exited=True
            ),
        )
    assert absent.value.status_code == 422
    attempt = await finish(db_session, claim, assignment, seconds=3.5)
    await finish(db_session, claim, assignment, seconds=3.5)
    with pytest.raises(HTTPException) as changed:
        await finish(db_session, claim, assignment, seconds=3.0)
    assert changed.value.status_code == 409
    await studies.coordinate(db_session, study.id)
    second = (await studies.describe(db_session, study))["trials"][1]
    child = await db_session.get(TrainingRun, uuid.UUID(second["run_id"]))
    next_attempt = await db_session.get(TrainingAttempt, child.current_attempt_id)
    assert next_attempt.native_budget["allowance_seconds"] == 6.5
    assert attempt.native_accounting["native_seconds"] == 3.5


@pytest.mark.parametrize(
    "state,seconds,expected",
    [
        ("unknown", None, "blocked"),
        ("exact", 10.0, "budget_exhausted"),
        ("exact", 12.5, "budget_exhausted"),
    ],
)
async def test_unknown_and_exhausted_accounting_prevents_next_child(
    db_session, super_admin, httpx_client, state, seconds, expected
):
    owner, token = super_admin
    _, _, study, _, _, _, _ = await setup(db_session, httpx_client, owner, token)
    claim = native_claim()
    assignment = await runs.claim(db_session, claim)
    await finish(
        db_session,
        claim,
        assignment,
        state=state,
        seconds=seconds,
        deadline=state == "exact",
    )
    await studies.coordinate(db_session, study.id)
    result = await studies.describe(db_session, study)
    assert result["status"] == expected and result["trials"][1]["run_id"] is None
    assert result["accounting"]["overrun_seconds"] == (2.5 if seconds == 12.5 else 0)
    assert bool(result["accounting"]["unknown_attempt_ids"]) == (state == "unknown")


@pytest.mark.parametrize("started", [False, True])
async def test_cancel_study_stops_admission_and_preserves_owned_lease(
    db_session, super_admin, httpx_client, started
):
    owner, token = super_admin
    _, _, study, path, headers, _, result = await setup(
        db_session, httpx_client, owner, token
    )
    claim = native_claim()
    assignment = await runs.claim(db_session, claim) if started else None
    response = await httpx_client.post(path + f"/{study.id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    assert not response.json()["scheduling"]
    assert response.json()["trials"][1]["status"] == "skipped"
    child = await db_session.get(TrainingRun, uuid.UUID(result["trials"][0]["run_id"]))
    attempt = await db_session.get(TrainingAttempt, child.current_attempt_id)
    if started:
        lease = await db_session.get(TrainingResourceLease, child.config["resource_id"])
        assert child.status == "cancelling" and lease.attempt_id == attempt.id
        await finish(db_session, claim, assignment, seconds=2)
        assert lease.attempt_id is None
    else:
        assert attempt.native_accounting["state"] == "not_started"
    await studies.coordinate(db_session, study.id)
    assert (
        len(await studies.children(db_session, study.id)) == 1
        and study.status == "cancelled"
    )


async def test_lost_child_blocks_next_and_manual_queue_shares_same_resource(
    db_session, super_admin, httpx_client
):
    from datetime import timedelta

    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    manual, _ = await create_run(httpx_client, token, project, dataset)
    # PostgreSQL now() is transaction-stable in this rollback fixture. Give the
    # earlier manual request a distinct timestamp before testing FIFO order.
    manual_row = await db_session.get(TrainingRun, uuid.UUID(manual["id"]))
    manual_row.created_at = await db_session.scalar(select(func.now())) - timedelta(
        seconds=1
    )
    await db_session.flush()
    _, _, study, _, _, _, _ = await setup(db_session, httpx_client, owner, token)
    claim = native_claim()
    assignment = await runs.claim(db_session, claim)
    assert assignment["run_id"] == manual["id"] and "native_budget" not in assignment
    assert await runs.claim(db_session, native_claim()) is None
    await runs.finish(
        db_session,
        uuid.UUID(assignment["attempt_id"]),
        ExecutorFinish(
            **fence(claim, assignment), status="failed", process_exited=True
        ),
    )
    claim = native_claim()
    assignment = await runs.claim(db_session, claim)
    attempt = await db_session.get(TrainingAttempt, uuid.UUID(assignment["attempt_id"]))
    attempt.heartbeat_at = runs.now() - timedelta(seconds=1000)
    await db_session.flush()
    assert await runs.claim(db_session, native_claim()) is None
    await studies.coordinate(db_session, study.id)
    assert (
        attempt.status == "lost"
        and len(await studies.children(db_session, study.id)) == 1
    )


async def test_resume_re_reserves_current_study_balance_after_later_trial_consumes_it(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, _, study, _, headers, _, _ = await setup(
        db_session, httpx_client, owner, token
    )
    first_claim = native_claim()
    first = await runs.claim(db_session, first_claim)
    first_attempt = await finish(db_session, first_claim, first, seconds=2)
    first_run = await db_session.get(TrainingRun, first_attempt.run_id)
    first_attempt.artifacts = {
        "checkpoint.pt": {
            "role": "checkpoint",
            "epoch": 1,
            "key": f"projects/{project.id}/runs/{first_run.id}/attempts/{first_attempt.id}/checkpoint.pt",
            "sha256": "f" * 64,
            "size_bytes": 10,
        }
    }
    monkeypatch.setattr(
        runs.storage.client,
        "head_object",
        lambda **kwargs: {
            "ContentLength": 10,
            "Metadata": {"attempt-id": str(first_attempt.id), "sha256": "f" * 64},
        },
    )
    await db_session.flush()
    await studies.coordinate(db_session, study.id)
    path = f"/api/v1/projects/{project.id}/training-runs/{first_run.id}/resume"
    assert (await httpx_client.post(path, headers=headers)).status_code == 409
    second_claim = native_claim()
    second = await runs.claim(db_session, second_claim)
    await finish(db_session, second_claim, second, seconds=5)
    await studies.coordinate(db_session, study.id)
    assert study.status == "completed"
    response = await httpx_client.post(path, headers=headers)
    assert response.status_code == 202, response.text
    resumed = await db_session.get(
        TrainingAttempt, uuid.UUID(response.json()["current_attempt_id"])
    )
    assert resumed.native_budget["allowance_seconds"] == 3.0
    assert response.json()["config_digest"] == first_run.config_digest
    replay = await runs.claim(db_session, native_claim())
    assert (
        replay["native_budget"]["allowance_seconds"] == 3.0
        and replay["resume"]["epoch"] == 1
    )


async def test_authority_revocation_cancels_queued_child_and_protected_deletion_has_no_storage_side_effect(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, dataset, study, _, headers, _, result = await setup(
        db_session, httpx_client, owner, token
    )
    calls = []
    monkeypatch.setattr(
        runs.storage, "delete_prefix", lambda *args, **kwargs: calls.append(args)
    )
    assert (
        await httpx_client.delete(
            f"/api/v1/projects/{project.id}/training-datasets/{dataset.id}",
            headers=headers,
        )
    ).status_code == 409
    owner.is_active = False
    await db_session.flush()
    await studies.coordinate(db_session, study.id)
    child = (await studies.children(db_session, study.id))[0]
    assert child.status == "cancelled" and not study.scheduling
    owner.is_active = True
    await db_session.flush()
    assert (
        await httpx_client.delete(
            f"/api/v1/projects/{project.id}/training-runs/{child.id}", headers=headers
        )
    ).status_code == 409
    assert calls == []


async def test_verified_final_reports_rank_only_within_compatible_identity_and_corruption_remains_visible(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    _, _, study, _, _, _, _ = await setup(db_session, httpx_client, owner, token)
    objects = {}
    for index in range(2):
        run = (await studies.children(db_session, study.id))[-1]
        # Select by trial ordinal because UUID order is intentionally unrelated.
        trial = await db_session.scalar(
            select(TrainingTrial).where(
                TrainingTrial.study_id == study.id, TrainingTrial.ordinal == index + 1
            )
        )
        run = await db_session.get(TrainingRun, trial.run_id)
        attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
        await runs.mark_terminal(db_session, run, attempt, "succeeded")
        report, _, _, _, weights = evidence()
        report["validation_digest"] = run.config["validation_digest"]
        report["per_class"] = [{"index": 0, "name": "car", "mAP50": 0.2}]
        descriptor = report["comparison"]
        descriptor["config_digest"] = run.config_digest
        descriptor["benchmark"]["classes"] = run.config["classes"]
        descriptor["evaluator"]["options"]["imgsz"] = run.config["parameters"]["imgsz"]
        report["summary"]["metrics/mAP50-95(B)"] = 0.2 + index * 0.3
        raw = json.dumps(report).encode()
        prefix = f"projects/{run.project_id}/runs/{run.id}/attempts/{attempt.id}/"
        key = prefix + "metrics.json"
        objects[key] = (raw, str(attempt.id))
        attempt.artifacts = {
            "metrics.json": {
                "role": "metrics",
                "key": key,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            },
            "best.pt": {"role": "weights", **weights, "key": prefix + "best.pt"},
        }
        await db_session.flush()
        await studies.coordinate(db_session, study.id)
    monkeypatch.setattr(
        runs.storage.client,
        "get_object",
        lambda **kw: {
            "Body": io.BytesIO(objects[kw["Key"]][0]),
            "Metadata": {"attempt-id": objects[kw["Key"]][1]},
        },
    )
    result = await studies.describe(db_session, study)
    assert len(result["rankings"]) == 1
    assert result["rankings"][0]["trial_ids"] == [
        result["trials"][1]["id"],
        result["trials"][0]["id"],
    ]
    second_key = next(
        key
        for key in objects
        if f"/attempts/{result['trials'][1]['result']['attempt_id']}/" in key
    )
    raw, identity = objects[second_key]
    incompatible = json.loads(raw)
    incompatible["comparison"]["evaluator"]["options"]["conf"] = 0.002
    changed_raw = json.dumps(incompatible).encode()
    objects[second_key] = (changed_raw, identity)
    second_attempt = await db_session.get(TrainingAttempt, uuid.UUID(identity))
    second_attempt.artifacts = {
        **second_attempt.artifacts,
        "metrics.json": {
            **second_attempt.artifacts["metrics.json"],
            "sha256": hashlib.sha256(changed_raw).hexdigest(),
            "size_bytes": len(changed_raw),
        },
    }
    await db_session.flush()
    separate = await studies.describe(db_session, study)
    assert len(separate["rankings"]) == 2 and all(
        len(group["trial_ids"]) == 1 for group in separate["rankings"]
    )
    raw = changed_raw
    objects[second_key] = (b"x" * len(raw), identity)
    corrupted = await studies.describe(db_session, study)
    assert (
        corrupted["trials"][1]["result"] is None
        and corrupted["trials"][1]["result_error"]
    )
    assert corrupted["rankings"][0]["trial_ids"] == [corrupted["trials"][0]["id"]]


async def test_concurrent_coordinators_and_fenced_finish_do_not_require_study_lock(
    test_engine,
    monkeypatch,
):
    from app.config import settings

    monkeypatch.setattr(settings, "training_node_id", f"study-{uuid.uuid4().hex}")
    resource_id = runs.resource_id()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_id = user_id = study_id = None
    try:
        async with maker() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith(
                "_test"
            )
            owner, _ = await _create_user(
                db,
                "project_admin",
                f"study-{uuid.uuid4().hex}@test.local",
                "Study owner",
            )
            project, dataset = await ready_dataset(db, owner)
            project_id, user_id = project.id, owner.id
            request = TrainingStudyCreate.model_validate(body(dataset))
            plan, digest = await studies.freeze_plan(db, project.id, request)
            study_id = uuid.UUID(plan["study_id"])
            db.add(
                TrainingStudy(
                    id=study_id,
                    project_id=project.id,
                    creator_id=owner.id,
                    dataset_id=dataset.id,
                    request_id=request.base.request_id,
                    request_digest=content_digest(request.request_payload()),
                    plan_digest=digest,
                    name="Concurrent",
                    plan=plan,
                    authorization={},
                )
            )
            await db.flush()
            for trial in plan["trials"]:
                db.add(
                    TrainingTrial(
                        id=uuid.UUID(trial["id"]),
                        study_id=study_id,
                        ordinal=trial["ordinal"],
                        config=trial["config"],
                        config_digest=trial["config_digest"],
                        values=trial["values"],
                    )
                )
            await db.commit()

        async def coordinate():
            async with maker() as db:
                await studies.coordinate(db, study_id)
                await db.commit()

        await asyncio.wait_for(asyncio.gather(coordinate(), coordinate()), 10)
        async with maker() as held:
            await studies.load(held, project_id, study_id, lock=True)
            assert len(await studies.children(held, study_id)) == 1

            async def execute():
                async with maker() as db:
                    claim = native_claim()
                    claim.resource_id = resource_id
                    assignment = await runs.claim(db, claim)
                    assert assignment
                    await finish(db, claim, assignment, seconds=1)
                    await db.commit()

            await asyncio.wait_for(execute(), 5)
            await held.rollback()
        await coordinate()
        async with maker() as db:
            assert len(await studies.children(db, study_id)) == 2
    finally:
        if project_id:
            async with maker() as db:
                await db.execute(
                    delete(TrainingResourceLease).where(
                        TrainingResourceLease.resource_id == resource_id
                    )
                )
                await db.execute(
                    delete(TrainingTrial).where(TrainingTrial.study_id == study_id)
                )
                await db.execute(
                    delete(TrainingRun).where(TrainingRun.project_id == project_id)
                )
                await db.execute(
                    delete(TrainingStudy).where(TrainingStudy.id == study_id)
                )
                await db.execute(
                    delete(AsyncJob).where(AsyncJob.project_id == project_id)
                )
                await db.execute(
                    delete(TrainingDatasetVersion).where(
                        TrainingDatasetVersion.project_id == project_id
                    )
                )
                await db.execute(delete(Project).where(Project.id == project_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()

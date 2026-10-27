"""Retained source qualification, frozen reuse and independent checkpoint resume."""

import asyncio
import hashlib
import io
import json
import uuid
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from training_contract.catalog import supported_identities
from training_contract.initialization import POLICY_DIGEST

from app.db.models.learning_policy import LearningPolicy
from app.db.models.model_decision import ModelDecision
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.project import Project
from app.db.models.user import User
from app.schemas.trained_model import TrainedModelCreate
from app.schemas.training_run import ExecutorFinish, TrainingRunCreate
from app.schemas.training_recipe import TrainingRecipeWrite
from app.services import (
    trained_models,
    training_initialization as source,
    training_run_storage,
    training_runs as runs,
)
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import content_digest
from tests.test_training_parameters import configuration
from tests.test_training_runs import ready_dataset, executor_claim, fence
from tests.conftest import _create_user
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401
from tests.test_learning_policies import build_candidate
from tests.test_learning_policies import frozen_source as frozen_source_fixture  # noqa: F401

pytestmark = pytest.mark.usefixtures("training_config_fixture")


@pytest.fixture
def objects(monkeypatch):
    values = {}

    def get_object(*, Bucket, Key):
        raw, attempt_id = values[Bucket, Key]
        return {
            "ContentLength": len(raw),
            "Metadata": {"attempt-id": str(attempt_id)},
            "Body": io.BytesIO(raw),
        }

    def head_object(*, Bucket, Key):
        result = get_object(Bucket=Bucket, Key=Key)
        result["Body"].close()
        return {key: value for key, value in result.items() if key != "Body"}

    monkeypatch.setattr(storage.client, "get_object", get_object)
    monkeypatch.setattr(storage.client, "head_object", head_object)
    return values


async def seed(db, owner, objects, classes=None):
    project, dataset = await ready_dataset(db, owner)
    if classes is not None:
        dataset.manifest = {**dataset.manifest, "classes": classes}
    config = await runs.frozen_config(
        db,
        project.id,
        TrainingRunCreate(
            name="Source", dataset_id=dataset.id, request_id=uuid.uuid4()
        ),
    )
    run = TrainingRun(
        project_id=project.id,
        creator_id=owner.id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Source run",
        status="succeeded",
        config=config,
        config_digest=content_digest(config),
    )
    db.add(run)
    await db.flush()
    await training_run_storage.allocate(db, run)
    attempt = TrainingAttempt(
        run_id=run.id,
        ordinal=1,
        status="succeeded",
        runtime={
            "python": "3.12.3",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "cuda": "13.2",
            "image_digest": config["image_digest"],
            "frameworks": {"ultralytics": "8.4.177"},
        },
    )
    db.add(attempt)
    await db.flush()
    artifacts = {}
    report = {
        "summary": {"mAP50": 0.3},
        "validation_digest": config["validation_digest"],
    }
    for name, role, raw in (
        ("best.pt", "weights", b"retained native source"),
        ("metrics.json", "metrics", json.dumps(report).encode()),
    ):
        key = f"projects/{project.id}/runs/{run.id}/attempts/{attempt.id}/{name}"
        objects[run.storage_ref["bucket"], key] = raw, attempt.id
        artifacts[name] = {
            "name": name,
            "role": role,
            "key": key,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    attempt.artifacts = artifacts
    run.current_attempt_id = attempt.id
    model = await trained_models.register(
        db,
        project,
        owner,
        TrainedModelCreate(
            request_id=uuid.uuid4(),
            name="Retained source",
            run_id=run.id,
            attempt_id=attempt.id,
            bindings=dataset.manifest["classes"],
        ),
    )
    return project, dataset, run, attempt, model


def headers(token):
    return {"Authorization": "Bearer " + token}


def path(project):
    return f"/api/v1/projects/{project.id}/training-runs"


async def eligibility(client, token, project, dataset, model, **extra):
    return await client.get(
        path(project) + f"/initialization-sources/{model.id}",
        headers=headers(token),
        params={
            "framework": "ultralytics",
            "base_model": "yolo11n",
            "dataset_id": str(dataset.id),
            **extra,
        },
    )


def body(dataset, pin):
    return {
        "name": "New weights-only experiment",
        "dataset_id": str(dataset.id),
        "framework": "ultralytics",
        "base_model": "yolo11n",
        "request_id": str(uuid.uuid4()),
        "configuration": configuration(
            epochs=3, batch=1, imgsz=128, optimizer="SGD", close_mosaic=0
        ),
        "initialization": source.reference(pin),
    }


async def create(client, token, project, payload):
    checked = await client.post(
        path(project) + ":preflight", headers=headers(token), json=payload
    )
    assert checked.status_code == 200, checked.text
    payload["preflight_digest"] = checked.json()["config_digest"]
    accepted = await client.post(path(project), headers=headers(token), json=payload)
    assert accepted.status_code == 202, accepted.text
    return accepted.json()


def claim():
    value = executor_claim()
    value.runtime.update(
        executor_protocol=2,
        supported_configs=supported_identities(),
        torch="2.14.1+cu132",
        torchvision="0.29.1+cu132",
        python="3.12.3",
        cuda="13.2",
        initialization_policy_digest=POLICY_DIGEST,
    )
    return value


async def test_exact_source_frozen_preflight_archive_and_accepted_replay(
    db_session, project_admin, httpx_client, objects
):
    owner, token = project_admin
    project, dataset, original, attempt, model = await seed(db_session, owner, objects)
    db_session.add(
        ModelDecision(
            project_id=project.id,
            model_version_id=model.id,
            creator_id=owner.id,
            creator_label=owner.name,
            sequence=1,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            disposition="archived",
            note="Retain weights",
        )
    )
    await db_session.flush()
    result = await eligibility(httpx_client, token, project, dataset, model)
    assert result.status_code == 200 and result.json()["eligible"], result.text
    assert result.json()["storage_verified"] is False
    pin = result.json()["pin"]
    assert pin["run_id"] == str(original.id) and pin["attempt_id"] == str(attempt.id)
    payload = body(dataset, pin)
    created = await create(httpx_client, token, project, payload)
    assert created["id"] != str(original.id)
    assert created["initialization_model_id"] == str(model.id)
    assert created["config"]["initialization"] == pin
    assert created["config"]["base_model"] == original.config["base_model"]
    lineage = await httpx_client.get(
        f"/api/v1/projects/{project.id}/model-lineage/run/{created['id']}",
        headers=headers(token),
    )
    edge = next(
        item
        for item in lineage.json()["inputs"]
        if item["relation"] == "weights_initialization"
    )
    assert edge["target"]["id"] == str(model.id)
    del objects[original.storage_ref["bucket"], model.artifact["key"]]
    replay = await httpx_client.post(
        path(project), headers=headers(token), json=payload
    )
    assert replay.status_code == 202 and replay.json()["id"] == created["id"]
    changed = await httpx_client.post(
        path(project),
        headers=headers(token),
        json={**payload, "initialization": {"kind": "catalog"}},
    )
    assert changed.status_code == 409
    assert await db_session.get(
        TrainingAttempt, uuid.UUID(created["current_attempt_id"])
    )


async def test_foreign_source_and_target_class_order_are_rejected(
    db_session, project_admin, annotator, httpx_client, objects
):
    owner, token = project_admin
    project, dataset, _, _, model = await seed(
        db_session, owner, objects, ["car", "person"]
    )
    other, other_data, _, _, foreign = await seed(db_session, owner, objects)
    cross = await eligibility(httpx_client, token, project, dataset, foreign)
    assert cross.status_code == 404
    dataset.status = "pending"
    cross_pending = await eligibility(httpx_client, token, project, dataset, foreign)
    assert cross_pending.status_code == 404
    dataset.status = "ready"
    denied = await eligibility(httpx_client, annotator[1], project, dataset, model)
    assert denied.status_code == 404
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    # Use an independently ready target rather than rewriting the immutable source.
    target = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot_digest="b" * 64,
        snapshot={},
        name="Different classes",
        status="ready",
        manifest={
            "schema": 1,
            "content_digest": "c" * 64,
            "validation_digest": "d" * 64,
            "classes": ["person", "car"],
        },
        package_key="owned/other.zip",
        package_sha256="e" * 64,
    )
    db_session.add(target)
    await db_session.flush()
    mismatch = await eligibility(httpx_client, token, project, target, model)
    assert mismatch.status_code == 200 and not mismatch.json()["eligible"]
    assert any("顺序" in item for item in mismatch.json()["reasons"])
    rejected = await httpx_client.post(
        path(project) + ":preflight", headers=headers(token), json=body(target, pin)
    )
    assert rejected.status_code == 409
    assert other.id != project.id and other_data.id != dataset.id


@pytest.mark.parametrize(
    "problem", ["corrupt", "missing", "oversized", "changed_pin", "variant"]
)
async def test_source_problems_never_fall_back_to_catalog(
    db_session, project_admin, httpx_client, objects, problem
):
    owner, token = project_admin
    project, dataset, run, attempt, model = await seed(db_session, owner, objects)
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    payload = body(dataset, pin)
    if problem == "corrupt":
        objects[run.storage_ref["bucket"], model.artifact["key"]] = (
            b"changed native source",
            attempt.id,
        )
    elif problem == "missing":
        del objects[run.storage_ref["bucket"], model.artifact["key"]]
    elif problem == "oversized":
        model.artifact = {**model.artifact, "size_bytes": 2 * 1024**3 + 1}
        attempt.artifacts = {**attempt.artifacts, "best.pt": model.artifact}
    elif problem == "changed_pin":
        payload["initialization"]["source_digest"] = "0" * 64
    else:
        payload["base_model"] = "yolo11s"
    response = await httpx_client.post(
        path(project) + ":preflight", headers=headers(token), json=payload
    )
    assert response.status_code in {409, 422}, response.text
    assert run.config.get("initialization") is None
    assert model.run_id == run.id


async def test_claim_requires_capability_and_verifies_source_before_gpu_assignment(
    db_session, project_admin, httpx_client, objects
):
    owner, token = project_admin
    project, dataset, _, _, model = await seed(db_session, owner, objects)
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    created = await create(httpx_client, token, project, body(dataset, pin))
    old = claim()
    old.runtime.pop("initialization_policy_digest")
    assert await runs.claim(db_session, old) is None
    current = claim()
    assignment = await runs.claim(db_session, current)
    assert assignment["run_id"] == created["id"]
    assert assignment["initialization"]["sha256"] == model.artifact["sha256"]
    assert assignment["initialization"]["source_digest"] == pin["source_digest"]
    assert assignment["resume"] is None


async def test_source_lost_after_acceptance_fails_initial_claim_but_resume_uses_own_checkpoint(
    db_session, project_admin, httpx_client, objects
):
    owner, token = project_admin
    project, dataset, source_run, _, model = await seed(db_session, owner, objects)
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    created = await create(httpx_client, token, project, body(dataset, pin))
    run = await db_session.get(TrainingRun, uuid.UUID(created["id"]))
    first = await db_session.get(TrainingAttempt, run.current_attempt_id)
    del objects[source_run.storage_ref["bucket"], model.artifact["key"]]
    assert await runs.claim(db_session, claim()) is None
    assert run.status == "failed" and first.failure["code"] == "input_mismatch"
    first.artifacts = {
        "checkpoint-0001.pt": {
            "role": "checkpoint",
            "key": f"projects/{project.id}/runs/{run.id}/attempts/{first.id}/checkpoint-0001.pt",
            "sha256": "1" * 64,
            "size_bytes": 100,
            "epoch": 1,
        }
    }
    following = await runs.new_attempt(db_session, run, resume_from=first.id)
    assignment = await runs.claim(db_session, claim())
    assert assignment["attempt_id"] == str(following.id)
    assert assignment["resume"]["key"].endswith("checkpoint-0001.pt")
    assert "initialization" not in assignment
    assert run.config["initialization"] == pin


def test_omitted_and_catalog_requests_keep_legacy_run_and_recipe_hashes():
    legacy = TrainingRunCreate(
        name="Legacy", dataset_id=uuid.uuid4(), request_id=uuid.uuid4()
    )
    explicit = TrainingRunCreate(
        **legacy.model_dump(exclude={"configuration", "parameters", "initialization"}),
        initialization={"kind": "catalog"},
    )
    assert legacy.request_payload() == explicit.request_payload()
    legacy_recipe = TrainingRecipeWrite(
        request_id=uuid.uuid4(),
        name="Legacy recipe",
        configuration={
            "framework": "ultralytics",
            "base_model": "yolo11n",
            "configuration": configuration(epochs=3),
        },
    )
    old = legacy_recipe.model_dump(mode="json")
    old["configuration"].pop("initialization")
    assert legacy_recipe.request_payload() == old
    catalog = legacy_recipe.model_copy(deep=True)
    catalog.configuration.initialization = TrainingRunCreate(
        **explicit.model_dump(exclude={"parameters", "configuration"})
    ).initialization
    assert catalog.request_payload() == old


@pytest.mark.usefixtures("frozen_source_fixture")
async def test_recipe_clone_policy_keep_exact_pin_and_source_deletion_precedes_storage(
    db_session, project_admin, httpx_client, objects, monkeypatch
):
    owner, token = project_admin
    project, dataset, original, _, model = await seed(db_session, owner, objects)
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    payload = body(dataset, pin)
    created = await create(httpx_client, token, project, payload)
    run = await db_session.get(TrainingRun, uuid.UUID(created["id"]))
    run.status = "succeeded"
    recipe_path = f"/api/v1/projects/{project.id}/training-recipes"
    recipe_body = {
        "request_id": str(uuid.uuid4()),
        "name": "Warm-start recipe",
        "source_run_id": str(run.id),
    }
    saved = await httpx_client.post(
        recipe_path, headers=headers(token), json=recipe_body
    )
    assert saved.status_code == 201, saved.text
    recipe = saved.json()
    assert recipe["revision"]["snapshot"]["initialization"] == pin
    assert recipe["revision"]["initialization_model_id"] == str(model.id)
    clone = await httpx_client.get(
        recipe_path + f"/clone/{run.id}", headers=headers(token)
    )
    assert (
        clone.status_code == 200 and clone.json()["snapshot"]["initialization"] == pin
    )
    origin = {
        "kind": "recipe",
        "recipe_id": recipe["id"],
        "revision_id": recipe["revision"]["id"],
        "digest": recipe["revision"]["snapshot_digest"],
    }
    policy_body = {
        "expected_revision": 0,
        "enabled": True,
        "parent_id": str(dataset.id),
        "framework": "ultralytics",
        "base_model": "yolo11n",
        "configuration": payload["configuration"],
        "initialization": payload["initialization"],
        "origin": origin,
        "min_changed_samples": 1,
        "check_interval_minutes": 1,
        "cooldown_minutes": 0,
    }
    policy_path = f"/api/v1/projects/{project.id}/learning-policy"
    result = await httpx_client.put(
        policy_path, headers=headers(token), json=policy_body
    )
    assert result.status_code == 200, result.text
    policy = await db_session.get(LearningPolicy, uuid.UUID(result.json()["id"]))
    assert (
        policy.config["initialization"] == pin
        and policy.initialization_model_id == model.id
    )
    _, _, child = await build_candidate(db_session, policy)
    assert (
        child
        and child.config["initialization"] == pin
        and child.initialization_model_id == model.id
    )
    dropped = await httpx_client.put(
        policy_path,
        headers=headers(token),
        json={
            key: value
            for key, value in {**policy_body, "expected_revision": 1}.items()
            if key != "initialization"
        },
    )
    assert dropped.status_code == 409
    deletion = Mock()
    monkeypatch.setattr(storage, "delete_prefix", deletion)
    removed = await httpx_client.delete(
        path(project) + f"/{original.id}", headers=headers(token)
    )
    assert removed.status_code == 409 and not deletion.called
    reverse = await httpx_client.get(
        f"/api/v1/projects/{project.id}/model-lineage/model/{model.id}",
        headers=headers(token),
        params={"relation": "initialized_runs"},
    )
    assert {item["id"] for item in reverse.json()["items"]} == {
        str(run.id),
        str(child.id),
    }


async def test_success_receipt_must_prove_fresh_loading(
    db_session, project_admin, httpx_client, objects
):
    owner, token = project_admin
    project, dataset, _, _, model = await seed(db_session, owner, objects)
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    created = await create(httpx_client, token, project, body(dataset, pin))
    executor = claim()
    assignment = await runs.claim(db_session, executor)
    run = await db_session.get(TrainingRun, uuid.UUID(created["id"]))
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    evidence = {
        "policy_digest": POLICY_DIGEST,
        "source_digest": pin["source_digest"],
        "source_sha256": pin["artifact"]["sha256"],
        "mode": "weights_only",
        "source_tensor_digest": "a" * 64,
        "loaded_tensor_digest": "a" * 64,
        "tensor_count": 499,
        "start_epoch": 0,
        "optimizer_state_count": 0,
        "scheduler_last_epoch": -1,
        "scaler_state": {},
        "ema_updates": 0,
        "best_fitness": None,
        "best_weights_present": False,
    }
    options = {
        "data": "/state/data.yaml",
        "model": "/state/initialization.pt",
        "project": "/state/attempt",
        "name": "framework",
        "exist_ok": False,
        "resume": False,
    }
    observed = {**run.config["configuration"]["effective_values"], **options}
    receipt = {
        "config_digest": run.config_digest,
        "observed": observed,
        "runtime_options": options,
        "verification": runs.verify_native(run.config, observed, options),
        "evidence": {"initialization": evidence},
    }

    def inventory(observation):
        values = {}
        for name, role, raw in [
            ("best.pt", "weights", b"new weights"),
            ("checkpoint.pt", "checkpoint", b"own checkpoint"),
            ("metrics.json", "metrics", b"{}"),
            ("config.json", "config", b"{}"),
            ("train.log", "log", b"complete"),
            ("native-config.json", "config", json.dumps(observation).encode()),
        ]:
            key = f"projects/{project.id}/runs/{run.id}/attempts/{attempt.id}/{name}"
            objects[run.storage_ref["bucket"], key] = raw, attempt.id
            values[name] = {
                "name": name,
                "role": role,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        attempt.upload_inventory = values

    completion = ExecutorFinish(
        **fence(executor, assignment), status="succeeded", process_exited=True
    )
    for invalid in [
        None,
        {**evidence, "start_epoch": 4},
        {**evidence, "loaded_tensor_digest": "0" * 64},
        {**evidence, "mode": "checkpoint_resume"},
    ]:
        inventory({**receipt, "evidence": {"initialization": invalid}})
        with pytest.raises(HTTPException) as caught:
            await runs.finish(db_session, attempt.id, completion)
        assert caught.value.status_code == 422 and run.status == "preparing"
        assert (await runs.lock_resource(db_session)).attempt_id == attempt.id
    inventory(receipt)
    assert (await runs.finish(db_session, attempt.id, completion)).status == "succeeded"
    assert (await runs.lock_resource(db_session)).attempt_id is None


async def test_withdrawn_actor_cannot_start_accepted_initialized_run(
    db_session, project_admin, httpx_client, objects
):
    owner, token = project_admin
    project, dataset, _, _, model = await seed(db_session, owner, objects)
    pin, _ = await source.source_pin(db_session, project.id, model.id)
    created = await create(httpx_client, token, project, body(dataset, pin))
    owner.is_active = False
    await db_session.flush()
    assert await runs.claim(db_session, claim()) is None
    run = await db_session.get(TrainingRun, uuid.UUID(created["id"]))
    assert run.status == "failed" and "权限" in run.error_message
    assert (await runs.lock_resource(db_session)).attempt_id is None


async def test_concurrent_source_acceptance_retains_model_before_delete(
    test_engine, objects
):
    """A real FK waiter cannot delete the candidate after its pin is accepted."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_id = user_id = None
    accepted = asyncio.Event()
    release = asyncio.Event()
    deleting = asyncio.Event()
    delete_pid = []
    tasks = []
    try:
        async with maker() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith(
                "_test"
            )
            owner, _ = await _create_user(
                db,
                "project_admin",
                f"initialization-{uuid.uuid4().hex}@test.local",
                "Initialization owner",
            )
            project, dataset, _, _, model = await seed(db, owner, objects)
            project_id, user_id, dataset_id, source_id = (
                project.id,
                owner.id,
                dataset.id,
                model.id,
            )
            await db.commit()

        async def accept():
            async with maker() as db:
                pin, _ = await source.source_pin(db, project_id, source_id)
                config = await runs.frozen_config(
                    db, project_id, TrainingRunCreate(**body(dataset, pin))
                )
                run = TrainingRun(
                    project_id=project_id,
                    creator_id=user_id,
                    dataset_id=dataset_id,
                    initialization_model_id=source_id,
                    request_id=uuid.uuid4(),
                    request_digest="a" * 64,
                    name="Concurrent accepted source",
                    config=config,
                    config_digest=content_digest(config),
                )
                db.add(run)
                await db.flush()
                await training_run_storage.allocate(db, run)
                accepted.set()
                await release.wait()
                await db.commit()

        async def remove():
            await accepted.wait()
            async with maker() as db:
                delete_pid.append(await db.scalar(text("SELECT pg_backend_pid()")))
                deleting.set()
                with pytest.raises(IntegrityError):
                    await db.execute(
                        delete(TrainedModelVersion).where(
                            TrainedModelVersion.id == source_id
                        )
                    )
                await db.rollback()

        tasks = [asyncio.create_task(accept()), asyncio.create_task(remove())]
        await asyncio.wait_for(deleting.wait(), 10)
        async with maker() as observer:
            for _ in range(100):
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": delete_pid[0]}
                )
                if blockers:
                    break
                await asyncio.sleep(0.02)
            assert blockers, "Source deletion must wait for accepted source reference"
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        async with maker() as db:
            assert await db.get(TrainedModelVersion, source_id)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if project_id:
            async with maker() as db:
                await db.execute(
                    delete(TrainingRun).where(
                        TrainingRun.project_id == project_id,
                        TrainingRun.initialization_model_id.is_not(None),
                    )
                )
                await db.execute(
                    delete(TrainedModelVersion).where(
                        TrainedModelVersion.project_id == project_id
                    )
                )
                await db.execute(
                    delete(TrainingRun).where(TrainingRun.project_id == project_id)
                )
                await db.execute(
                    delete(TrainingDatasetVersion).where(
                        TrainingDatasetVersion.project_id == project_id
                    )
                )
                await db.execute(delete(Project).where(Project.id == project_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()

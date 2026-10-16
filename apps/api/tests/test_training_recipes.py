"""Immutable reuse, source independence, digest compatibility and explicit adoption."""

import copy
import uuid

import pytest
from sqlalchemy import delete, select
from app.config import settings
from app.db.models.learning_policy import LearningPolicy
from app.db.models.training_recipe import TrainingRecipeRevision
from app.db.models.training_run import TrainingRun
from app.services import training_claims as claims
from tests.test_training_parameters import configuration, new_v2
from tests.test_training_runs import ready_dataset
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401
from tests.test_learning_policies import build_candidate, manifest
from tests.test_learning_policies import frozen_source as frozen_source_fixture  # noqa: F401

pytestmark = pytest.mark.usefixtures("training_config_fixture")


def headers(token):
    return {"Authorization": "Bearer " + token}


def recipe_body(**values):
    return {
        "request_id": str(uuid.uuid4()),
        "name": "AdamW recipe",
        "configuration": {
            "framework": "ultralytics",
            "base_model": "yolo11n",
            "configuration": configuration(
                optimizer="AdamW", epochs=4, mosaic=0.5, close_mosaic=0, **values
            ),
        },
    }


async def create_recipe(client, token, project, body=None):
    path = f"/api/v1/projects/{project.id}/training-recipes"
    body = body or recipe_body()
    response = await client.post(path, json=body, headers=headers(token))
    assert response.status_code == 201, response.text
    return response.json(), path, body


def reference(recipe):
    return {
        "kind": "recipe",
        "recipe_id": recipe["id"],
        "revision_id": recipe["revision"]["id"],
        "digest": recipe["revision"]["snapshot_digest"],
    }


async def test_immutable_history_cas_archive_and_idempotent_replay(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, _ = await ready_dataset(db_session, owner)
    first, path, body = await create_recipe(httpx_client, token, project)
    pinned = copy.deepcopy(first["revision"])
    update = {**recipe_body(), "expected_head_revision": 1, "name": "Second"}
    update["configuration"]["configuration"]["values"]["mosaic"] = 0.25
    result = await httpx_client.post(
        f"{path}/{first['id']}/revisions", headers=headers(token), json=update
    )
    assert result.status_code == 201, result.text
    second = result.json()
    assert second["head_revision"] == 2
    stale = await httpx_client.post(
        f"{path}/{first['id']}/revisions",
        headers=headers(token),
        json={**update, "request_id": str(uuid.uuid4())},
    )
    assert stale.status_code == 409
    old = await httpx_client.get(
        f"{path}/{first['id']}?revision_id={pinned['id']}", headers=headers(token)
    )
    assert old.json()["revision"] == pinned
    history = await httpx_client.get(
        f"{path}/{first['id']}/revisions?limit=1", headers=headers(token)
    )
    assert history.json()["total"] == 2 and len(history.json()["items"]) == 1
    archived = await httpx_client.post(
        f"{path}/{first['id']}/archive?expected_head_revision=2", headers=headers(token)
    )
    assert archived.status_code == 200
    assert (await httpx_client.get(path, headers=headers(token))).json()["total"] == 0
    assert (
        await httpx_client.get(path + "?include_archived=true", headers=headers(token))
    ).json()["total"] == 1
    replay = await httpx_client.post(path, headers=headers(token), json=body)
    assert replay.status_code == 201 and replay.json()["revision"] == pinned
    mismatch = await httpx_client.post(
        path, headers=headers(token), json={**body, "name": "Other"}
    )
    assert mismatch.status_code == 409


async def test_recipe_scope_attempt_relationship_and_legacy_rejection(
    db_session, super_admin, annotator, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    other_project, other_dataset = await ready_dataset(db_session, owner)
    first, path, _ = await create_recipe(httpx_client, token, project)
    forbidden = await httpx_client.get(path, headers=headers(annotator[1]))
    assert forbidden.status_code == 404
    cross = await httpx_client.get(
        f"/api/v1/projects/{other_project.id}/training-recipes/{first['id']}",
        headers=headers(token),
    )
    assert cross.status_code == 404
    run, _ = await new_v2(httpx_client, token, project, dataset)
    other, _ = await new_v2(httpx_client, token, other_project, other_dataset)
    bad = await httpx_client.get(
        f"{path}/clone/{run['id']}?attempt_id={other['current_attempt_id']}",
        headers=headers(token),
    )
    assert bad.status_code == 404
    clone = await httpx_client.get(
        f"{path}/clone/{run['id']}?attempt_id={run['current_attempt_id']}",
        headers=headers(token),
    )
    assert clone.status_code == 200, clone.text
    value = clone.json()
    assert value["source_dataset_id"] == str(dataset.id)
    assert set(value["snapshot"]) == {
        "framework",
        "framework_version",
        "image_digest",
        "base_model",
        "configuration",
    }
    assert value["origin"]["digest"] == run["config_digest"]
    assert not any(
        key in value["snapshot"]
        for key in (
            "request_id",
            "dataset_id",
            "artifacts",
            "runtime",
            "auto_trigger_id",
        )
    )
    record = await db_session.get(TrainingRun, uuid.UUID(run["id"]))
    record.config = {
        key: val for key, val in record.config.items() if key != "configuration"
    }
    await db_session.commit()
    legacy = await httpx_client.get(f"{path}/clone/{run['id']}", headers=headers(token))
    assert legacy.status_code == 409 and "六项" in legacy.text


async def test_sources_pin_identity_and_modified_runs_keep_execution_digest(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    recipe, path, body = await create_recipe(httpx_client, token, project)
    run_path = f"/api/v1/projects/{project.id}/training-runs"
    payload = {
        "name": "Modified",
        "dataset_id": str(dataset.id),
        "request_id": str(uuid.uuid4()),
        "configuration": body["configuration"]["configuration"],
        "origin": reference(recipe),
    }
    plain = await httpx_client.post(
        run_path + ":preflight",
        json={key: value for key, value in payload.items() if key != "origin"},
        headers=headers(token),
    )
    exact = await httpx_client.post(
        run_path + ":preflight", json=payload, headers=headers(token)
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["config_digest"] == plain.json()["config_digest"]
    assert exact.json()["origin"]["modified"] is False
    payload["configuration"] = copy.deepcopy(payload["configuration"])
    payload["configuration"]["values"]["mosaic"] = 0.25
    checked = await httpx_client.post(
        run_path + ":preflight", json=payload, headers=headers(token)
    )
    assert checked.status_code == 200 and checked.json()["origin"]["modified"] is True
    payload["preflight_digest"] = checked.json()["config_digest"]
    created = await httpx_client.post(run_path, json=payload, headers=headers(token))
    assert created.status_code == 202, created.text
    assert created.json()["origin"]["modified"] is True
    assert "origin" not in created.json()["config"]
    assert (
        created.json()["config"]["configuration"]["effective_values"]["optimizer"]
        == "AdamW"
    )
    await httpx_client.post(
        f"{path}/{recipe['id']}/archive?expected_head_revision=1",
        headers=headers(token),
    )
    assert (
        await httpx_client.post(run_path, json=payload, headers=headers(token))
    ).json()["id"] == created.json()["id"]
    rejected = await httpx_client.post(
        run_path + ":preflight",
        json={**payload, "request_id": str(uuid.uuid4())},
        headers=headers(token),
    )
    assert rejected.status_code == 409
    changed = copy.deepcopy(settings.training_catalog_json)
    changed["ultralytics"]["base_models"]["yolo11n"]["sha256"] = "f" * 64
    monkeypatch.setattr(settings, "training_catalog_json", changed)
    detail = await httpx_client.get(f"{path}/{recipe['id']}", headers=headers(token))
    assert detail.json()["compatible"] is False
    assert (
        await httpx_client.post(run_path, json=payload, headers=headers(token))
    ).status_code == 202


async def test_saved_verified_source_survives_source_deletion(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    run, _ = await new_v2(httpx_client, token, project, dataset)
    path = f"/api/v1/projects/{project.id}/training-recipes"
    body = {
        "request_id": str(uuid.uuid4()),
        "name": "Verified source",
        "source_run_id": run["id"],
        "source_attempt_id": run["current_attempt_id"],
    }
    assert (
        await httpx_client.post(path, json=body, headers=headers(token))
    ).status_code == 409
    record = await db_session.get(TrainingRun, uuid.UUID(run["id"]))
    record.status = "succeeded"
    await db_session.commit()
    recipe, _, _ = await create_recipe(httpx_client, token, project, body)
    await db_session.execute(delete(TrainingRun).where(TrainingRun.id == record.id))
    await db_session.commit()
    detail = await httpx_client.get(f"{path}/{recipe['id']}", headers=headers(token))
    assert detail.json()["source_available"] is False
    assert detail.json()["compatible"] is True
    assert detail.json()["revision"]["origin"]["run_id"] == run["id"]
    replay = await httpx_client.post(path, json=body, headers=headers(token))
    assert (
        replay.status_code == 201
        and replay.json()["revision"]["id"] == recipe["revision"]["id"]
    )
    clone = await httpx_client.get(f"{path}/clone/{run['id']}", headers=headers(token))
    assert clone.status_code == 404


async def test_policy_pins_revision_and_archive_does_not_break_edits_or_execution(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    recipe, path, source = await create_recipe(httpx_client, token, project)
    policy_path = f"/api/v1/projects/{project.id}/learning-policy"
    body = {
        "expected_revision": 0,
        "enabled": False,
        "parent_id": str(dataset.id),
        "framework": "ultralytics",
        "base_model": "yolo11n",
        "configuration": source["configuration"]["configuration"],
        "origin": reference(recipe),
        "min_changed_samples": 10,
        "check_interval_minutes": 5,
        "cooldown_minutes": 60,
    }
    result = await httpx_client.put(policy_path, json=body, headers=headers(token))
    assert result.status_code == 200, result.text
    pinned = copy.deepcopy(result.json()["config"])
    policy = await db_session.scalar(
        select(LearningPolicy).where(LearningPolicy.project_id == project.id)
    )
    assert claims.policy_template_current(policy)
    updated = {**recipe_body(), "expected_head_revision": 1}
    updated["configuration"]["configuration"]["values"]["mosaic"] = 0.1
    second = await httpx_client.post(
        f"{path}/{recipe['id']}/revisions", json=updated, headers=headers(token)
    )
    assert second.status_code == 201
    assert (await httpx_client.get(policy_path, headers=headers(token))).json()[
        "config"
    ] == pinned
    await httpx_client.post(
        f"{path}/{recipe['id']}/archive?expected_head_revision=2",
        headers=headers(token),
    )
    edited = await httpx_client.put(
        policy_path,
        json={**body, "expected_revision": 1, "min_changed_samples": 20},
        headers=headers(token),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["config"]["configuration"] == pinned["configuration"]
    assert edited.json()["config"]["origin"] == pinned["origin"]
    await db_session.refresh(policy)
    assert claims.policy_template_current(policy)
    adoption = {
        **body,
        "expected_revision": 2,
        "origin": reference(second.json()),
        "configuration": updated["configuration"]["configuration"],
    }
    assert (
        await httpx_client.put(policy_path, json=adoption, headers=headers(token))
    ).status_code == 409
    changed = copy.deepcopy(body)
    changed["expected_revision"] = 2
    changed["configuration"]["values"]["mosaic"] = 0.2
    assert (
        await httpx_client.put(policy_path, json=changed, headers=headers(token))
    ).status_code == 409
    downgraded = {
        key: value
        for key, value in body.items()
        if key not in ("configuration", "origin")
    }
    downgraded["expected_revision"] = 2
    assert (
        await httpx_client.put(policy_path, json=downgraded, headers=headers(token))
    ).status_code == 409
    assert (
        await db_session.scalar(
            select(TrainingRecipeRevision).where(
                TrainingRecipeRevision.id == uuid.UUID(recipe["revision"]["id"])
            )
        )
    ).snapshot == recipe["revision"]["snapshot"]


@pytest.mark.usefixtures("frozen_source_fixture")
async def test_pinned_policy_keeps_generating_original_snapshot_after_update_and_archive(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    dataset.manifest = manifest()
    await db_session.flush()
    recipe, path, source = await create_recipe(httpx_client, token, project)
    body = {
        "expected_revision": 0,
        "enabled": True,
        "parent_id": str(dataset.id),
        "framework": "ultralytics",
        "base_model": "yolo11n",
        "configuration": source["configuration"]["configuration"],
        "origin": reference(recipe),
        "min_changed_samples": 1,
        "check_interval_minutes": 5,
        "cooldown_minutes": 0,
    }
    result = await httpx_client.put(
        f"/api/v1/projects/{project.id}/learning-policy",
        json=body,
        headers=headers(token),
    )
    assert result.status_code == 200, result.text
    policy = await db_session.get(LearningPolicy, uuid.UUID(result.json()["id"]))
    pinned = copy.deepcopy(policy.config)
    updated = {**recipe_body(), "expected_head_revision": 1}
    updated["configuration"]["configuration"]["values"]["mosaic"] = 0.1
    assert (
        await httpx_client.post(
            f"{path}/{recipe['id']}/revisions", json=updated, headers=headers(token)
        )
    ).status_code == 201
    assert (
        await httpx_client.post(
            f"{path}/{recipe['id']}/archive?expected_head_revision=2",
            headers=headers(token),
        )
    ).status_code == 200
    trigger, _, run = await build_candidate(db_session, policy)
    assert run is not None and run.status == "queued"
    assert trigger.policy_revision == 1
    assert run.origin == pinned["origin"]
    assert run.config["configuration"] == pinned["configuration"]
    assert run.config["configuration"]["effective_values"]["mosaic"] == 0.5

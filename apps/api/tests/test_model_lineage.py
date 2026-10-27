"""Bounded provenance, ownership, candidate history and serving independence."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.model_lineage import create_decision
from app.api.v1.trained_models import publish_model
from app.db.models.model_decision import ModelDecision
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.sample_evaluation import SampleEvaluationRun
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_recipe import TrainingRecipe, TrainingRecipeRevision
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.db.models.user import User
from app.schemas.model_lineage import ModelDecisionCreate
from app.schemas.trained_model import TrainedModelCreate, ModelReleaseCreate
from app.services import model_lineage as service, trained_models
from tests.trained_model_reference_fixtures import build_reference
from app.application.project_deletion import (
    delete_project as delete_project_transaction,
)
from tests.conftest import _create_user
from tests.factory import create_membership, create_task
from tests.test_trained_models import (
    controlled_artifacts,  # noqa: F401
    seed_source,
    ready_deployment,
)  # noqa: F401


pytestmark = pytest.mark.usefixtures("controlled_artifacts")


async def seed(db, owner):
    project, run, attempt = await seed_source(db, owner)
    body = TrainedModelCreate(
        request_id=uuid.uuid4(),
        name="Candidate",
        run_id=run.id,
        attempt_id=attempt.id,
        bindings=["car", "person"],
    )
    model = await trained_models.register(db, project, owner, body)
    return project, run, attempt, model


def url(project, kind, node_id):
    return f"/api/v1/projects/{project.id}/model-lineage/{kind}/{node_id}"


async def get(client, token, project, kind, node_id, **params):
    response = await client.get(
        url(project, kind, node_id),
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_lightweight_exact_sources_and_lazy_navigation(
    httpx_client, db_session, project_admin, monkeypatch
):
    owner, token = project_admin
    project, run, attempt, model = await seed(db_session, owner)
    run.config = {**run.config, "secret": "SHOULD_NOT_RENDER"}
    attempt.runtime = {"auth_token": "SHOULD_NOT_RENDER"}
    monkeypatch.setattr(
        trained_models.storage,
        "generate_download_url",
        Mock(side_effect=AssertionError("lineage must be DB-only")),
    )
    result = await get(httpx_client, token, project, "model", model.id)
    assert result["items"] == []
    assert {v["relation"]: v["target"]["id"] for v in result["inputs"]} == {
        "experiment": str(run.id),
        "exact_attempt": str(attempt.id),
        "training_data": str(run.dataset_id),
    }
    assert "SHOULD_NOT_RENDER" not in str(result)
    assert result["node"]["facts"]["weights_sha256"] == "2" * 64
    result = await get(httpx_client, token, project, "attempt", attempt.id)
    assert result["node"]["facts"]["run_id"] == str(run.id)
    result = await get(httpx_client, token, project, "run", run.id, relation="attempts")
    assert result["items"][0]["id"] == str(attempt.id)
    run.config = {**run.config, "framework": {"auth_token": "SHOULD_NOT_RENDER"}}
    model.provenance = {**model.provenance, "config_digest": ["SHOULD_NOT_RENDER"]}
    await db_session.flush()
    for kind, node_id in (("run", run.id), ("model", model.id)):
        malformed = await get(httpx_client, token, project, kind, node_id)
        assert "SHOULD_NOT_RENDER" not in str(malformed)


@pytest.mark.asyncio
async def test_project_authority_and_missing_roots(
    httpx_client, db_session, project_admin, annotator
):
    owner, token = project_admin
    project, run, attempt, model = await seed(db_session, owner)
    foreign, _, _, foreign_model = await seed(db_session, owner)
    for kind, node_id in (
        ("model", foreign_model.id),
        ("run", foreign_model.run_id),
        ("attempt", foreign_model.attempt_id),
        ("dataset", uuid.uuid4()),
    ):
        r = await httpx_client.get(
            url(project, kind, node_id), headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 404
    user, user_token = annotator
    await create_membership(
        db_session, project_id=project.id, user_id=user.id, role="annotator"
    )
    r = await httpx_client.get(
        url(project, "model", model.id),
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 403
    assert (await httpx_client.get(url(project, "model", model.id))).status_code == 401


@pytest.mark.asyncio
async def test_independent_model_foreign_keys_and_malformed_provenance_are_gaps(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, run, attempt, model = await seed(db_session, owner)
    _, _, foreign_attempt, _ = await seed(db_session, owner)
    model.attempt_id = foreign_attempt.id
    model.provenance = {"dataset_id": "malformed", "validation_digest": "retained"}
    result = await get(httpx_client, token, project, "model", model.id)
    assert all(v["target"] is None and v["gap"] for v in result["inputs"])
    assert result["node"]["facts"]["frozen_validation_digest"] == "retained"
    assert str(foreign_attempt.run_id) not in str(result)


@pytest.mark.asyncio
async def test_configuration_clone_checkpoint_resume_and_legacy_gaps(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, run, first, _ = await seed(db_session, owner)
    second = TrainingAttempt(
        run_id=run.id, ordinal=2, status="succeeded", resume_from_id=first.id
    )
    db_session.add(second)
    await db_session.flush()
    run.current_attempt_id = second.id
    run.origin = {
        "kind": "clone",
        "run_id": str(uuid.uuid4()),
        "attempt_id": "invalid",
        "digest": run.config_digest,
    }
    result = await get(httpx_client, token, project, "attempt", second.id)
    assert next(i for i in result["inputs"] if i["relation"] == "checkpoint_resume")[
        "target"
    ]["id"] == str(first.id)
    result = await get(httpx_client, token, project, "run", run.id)
    assert next(i for i in result["inputs"] if i["relation"] == "configuration_clone")[
        "gap"
    ]
    assert not any(i["relation"] == "checkpoint_resume" for i in result["inputs"])
    first.resume_from_id = second.id
    result = await get(httpx_client, token, project, "attempt", first.id)
    assert next(i for i in result["inputs"] if i["relation"] == "checkpoint_resume")[
        "gap"
    ]
    run.current_attempt_id = uuid.uuid4()
    result = await get(httpx_client, token, project, "run", run.id)
    assert (
        next(i for i in result["inputs"] if i["relation"] == "current_attempt")[
            "target"
        ]
        is None
    )


@pytest.mark.asyncio
async def test_recipe_links_validate_owner_and_digest(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, run, _, _ = await seed(db_session, owner)
    recipe = TrainingRecipe(project_id=project.id)
    db_session.add(recipe)
    await db_session.flush()
    revision = TrainingRecipeRevision(
        project_id=project.id,
        recipe_id=recipe.id,
        ordinal=1,
        name="Recipe",
        snapshot={},
        snapshot_digest="a" * 64,
        request_id=uuid.uuid4(),
        request_digest="b" * 64,
    )
    db_session.add(revision)
    await db_session.flush()
    run.origin = {
        "kind": "recipe",
        "recipe_id": str(recipe.id),
        "revision_id": str(revision.id),
        "digest": revision.snapshot_digest,
    }
    result = await get(httpx_client, token, project, "run", run.id)
    assert next(i for i in result["inputs"] if i["relation"] == "recipe_configuration")[
        "target"
    ]["id"] == str(revision.id)
    run.origin = {**run.origin, "digest": "wrong"}
    result = await get(httpx_client, token, project, "run", run.id)
    assert next(i for i in result["inputs"] if i["relation"] == "recipe_configuration")[
        "gap"
    ]
    foreign, _, _, _ = await seed(db_session, owner)
    revision.project_id = foreign.id
    r = await httpx_client.get(
        url(project, "recipe_revision", revision.id),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_keyset_pagination_ties_inserts_cursor_binding_and_deep_parent(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, run, _, _ = await seed(db_session, owner)
    parent = await db_session.get(TrainingDatasetVersion, run.dataset_id)
    timestamp = datetime.now(timezone.utc)
    for index in range(23):
        db_session.add(
            TrainingDatasetVersion(
                project_id=project.id,
                parent_id=parent.id,
                name=f"Child {index}",
                request_id=uuid.uuid4(),
                request_digest="a" * 64,
                snapshot_digest="b" * 64,
                snapshot={},
                created_at=timestamp,
            )
        )
    await db_session.flush()
    first = await get(
        httpx_client,
        token,
        project,
        "dataset",
        parent.id,
        relation="child_datasets",
        limit=20,
    )
    assert len(first["items"]) == 20 and first["next_cursor"]
    db_session.add(
        TrainingDatasetVersion(
            project_id=project.id,
            parent_id=parent.id,
            name="New",
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            snapshot_digest="b" * 64,
            snapshot={},
            created_at=timestamp + timedelta(seconds=1),
        )
    )
    await db_session.flush()
    second = await get(
        httpx_client,
        token,
        project,
        "dataset",
        parent.id,
        relation="child_datasets",
        cursor=first["next_cursor"],
    )
    assert len(second["items"]) == 3 and second["next_cursor"] is None
    assert not ({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})
    for limit, expected_count, has_more in (
        (1, 1, True),
        (21, 21, True),
        (100, 24, False),
    ):
        page = await get(
            httpx_client,
            token,
            project,
            "dataset",
            parent.id,
            relation="child_datasets",
            limit=limit,
        )
        assert len(page["items"]) == expected_count
        assert bool(page["next_cursor"]) is has_more
    for params in (
        {"relation": "runs", "cursor": first["next_cursor"]},
        {"cursor": first["next_cursor"]},
        {"relation": "bad"},
        {"relation": "child_datasets", "cursor": "%%%"},
        {"limit": 0},
        {"limit": 101},
    ):
        r = await httpx_client.get(
            url(project, "dataset", parent.id),
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        assert r.status_code == 422, params
    current = parent
    for index in range(45):
        child = TrainingDatasetVersion(
            project_id=project.id,
            parent_id=current.id,
            name=f"Depth {index}",
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            snapshot_digest="b" * 64,
            snapshot={},
        )
        db_session.add(child)
        await db_session.flush()
        current = child
    result = await get(httpx_client, token, project, "dataset", current.id)
    assert len(result["inputs"]) == 1 and len(str(result)) < 2500
    current.parent_id = current.id
    result = await get(httpx_client, token, project, "dataset", current.id)
    assert result["inputs"][0]["gap"]


async def release_row(db, project, model, deployment, generation, **extra):
    row = ModelRelease(
        project_id=project.id,
        model_version_id=model.id if model else None,
        deployment_id=deployment.id if deployment else None,
        request_id=uuid.uuid4(),
        request_digest="r" * 64,
        generation=generation,
        snapshot={},
        **extra,
    )
    db.add(row)
    await db.flush()
    if model:
        row.snapshot = build_reference(model, deployment, row)
    return row


@pytest.mark.asyncio
async def test_release_previous_and_rollback_edges_use_exact_historical_targets(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, _, _, model = await seed(db_session, owner)
    _, deployment = await ready_deployment(db_session, project, model)
    first = await release_row(db_session, project, model, deployment, 1)
    second = await release_row(
        db_session, project, None, None, 2, previous_release_id=first.id
    )
    rollback = await release_row(
        db_session,
        project,
        model,
        deployment,
        3,
        previous_release_id=second.id,
        rollback_from_id=first.id,
    )
    result = await get(httpx_client, token, project, "release", rollback.id)
    links = {i["relation"]: i["target"]["id"] for i in result["inputs"]}
    assert links["previous_selection"] == str(second.id) and links[
        "rollback_target"
    ] == str(first.id)
    result = await get(
        httpx_client, token, project, "release", first.id, relation="rollback_uses"
    )
    assert result["items"][0]["id"] == str(rollback.id)
    result = await get(httpx_client, token, project, "release", second.id)
    assert not any(i["relation"] == "published_candidate" for i in result["inputs"])
    invalid = await release_row(
        db_session, project, None, None, 4, rollback_from_id=first.id
    )
    result = await get(
        httpx_client, token, project, "release", first.id, relation="rollback_uses"
    )
    assert [i["id"] for i in result["items"]] == [str(rollback.id)]
    invalid_result = await get(httpx_client, token, project, "release", invalid.id)
    assert next(
        i for i in invalid_result["inputs"] if i["relation"] == "rollback_target"
    )["gap"]
    first.previous_release_id = rollback.id
    result = await get(httpx_client, token, project, "release", first.id)
    assert next(i for i in result["inputs"] if i["relation"] == "previous_selection")[
        "gap"
    ]


@pytest.mark.asyncio
async def test_evaluation_and_deployment_validate_cross_parent_references(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, run, _, model = await seed(db_session, owner)
    foreign, _, _, other = await seed(db_session, owner)
    _, deployment = await ready_deployment(db_session, foreign, other)
    evaluation = SampleEvaluationRun(
        project_id=project.id,
        dataset_id=run.dataset_id,
        model_version_id=model.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Mismatch",
        spec={},
        spec_digest="b" * 64,
        authorization={},
    )
    db_session.add(evaluation)
    await db_session.flush()
    result = await get(httpx_client, token, project, "evaluation", evaluation.id)
    assert next(
        i for i in result["inputs"] if i["relation"] == "evaluation_deployment"
    )["gap"]
    assert "controlled-" not in str(result)
    assert (
        await httpx_client.get(
            url(project, "deployment", deployment.id),
            headers={"Authorization": f"Bearer {token}"},
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_append_decisions_idempotency_pagination_archive_and_current_release(
    httpx_client, db_session, project_admin
):
    owner, token = project_admin
    project, _, _, model = await seed(db_session, owner)
    _, deployment = await ready_deployment(db_session, project, model)
    release = await release_row(db_session, project, model, deployment, 1)
    db_session.add(
        ProjectModelSelection(
            project_id=project.id, release_id=release.id, generation=1
        )
    )
    await db_session.flush()
    base = f"/api/v1/projects/{project.id}/trained-models"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "request_id": str(uuid.uuid4()),
        "disposition": "archived",
        "note": "  retain historical benchmark  ",
    }
    first = await httpx_client.post(
        f"{base}/{model.id}/decisions", json=body, headers=headers
    )
    assert first.status_code == 201, first.text
    assert (
        first.json()["note"] == "retain historical benchmark"
        and first.json()["sequence"] == 1
    )
    replay = await httpx_client.post(
        f"{base}/{model.id}/decisions", json=body, headers=headers
    )
    assert replay.json()["id"] == first.json()["id"]
    conflict = await httpx_client.post(
        f"{base}/{model.id}/decisions",
        json={**body, "note": "changed"},
        headers=headers,
    )
    assert conflict.status_code == 409
    default = (await httpx_client.get(base, headers=headers)).json()
    assert default["items"][0]["archived"] and default["items"][0]["is_current_release"]
    selection = await db_session.get(ProjectModelSelection, (project.id, "detect"))
    assert selection.generation == 1 and selection.release_id == release.id
    selection.release_id = None
    await db_session.flush()
    assert (await httpx_client.get(base, headers=headers)).json()["total"] == 0
    assert (
        await httpx_client.get(base, headers=headers, params={"archive": "only"})
    ).json()["total"] == 1
    assert (
        await httpx_client.get(f"{base}/{model.id}", headers=headers)
    ).status_code == 200
    for index in range(2, 24):
        await service.append_decision(
            db_session,
            project.id,
            model.id,
            owner,
            ModelDecisionCreate(
                request_id=uuid.uuid4(),
                disposition="research",
                note=f"evidence {index}",
            ),
        )
    result = (
        await httpx_client.get(f"{base}/{model.id}/decisions", headers=headers)
    ).json()
    assert [i["sequence"] for i in result["items"]] == list(range(23, 3, -1))
    older = (
        await httpx_client.get(
            f"{base}/{model.id}/decisions",
            headers=headers,
            params={"before": result["next_before"]},
        )
    ).json()
    assert [i["sequence"] for i in older["items"]] == [3, 2, 1]
    for limit, expected_count, next_before in (
        (1, 1, 23),
        (21, 21, 3),
        (100, 23, None),
    ):
        page = await httpx_client.get(
            f"{base}/{model.id}/decisions", headers=headers, params={"limit": limit}
        )
        assert page.status_code == 200, page.text
        assert len(page.json()["items"]) == expected_count
        assert page.json()["next_before"] == next_before
    for limit in (0, 101):
        rejected = await httpx_client.get(
            f"{base}/{model.id}/decisions", headers=headers, params={"limit": limit}
        )
        assert rejected.status_code == 422, limit
    assert (await httpx_client.get(base, headers=headers)).json()["total"] == 1
    bad = await httpx_client.post(
        f"{base}/{model.id}/decisions",
        json={**body, "request_id": str(uuid.uuid4()), "note": " "},
        headers=headers,
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_candidate_decisions_protect_source_before_deletion(
    db_session, project_admin, httpx_client, monkeypatch
):
    owner, token = project_admin
    project, run, _, model = await seed(db_session, owner)
    await service.append_decision(
        db_session,
        project.id,
        model.id,
        owner,
        ModelDecisionCreate(
            request_id=uuid.uuid4(), disposition="archived", note="historical"
        ),
    )
    delete_prefix = Mock(
        side_effect=AssertionError("storage deletion preceded reference guard")
    )
    from app.services.storage import storage_service

    monkeypatch.setattr(storage_service, "delete_prefix", delete_prefix)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (
        f"/api/v1/projects/{project.id}/training-runs/{run.id}",
        f"/api/v1/projects/{project.id}/training-datasets/{run.dataset_id}",
        f"/api/v1/projects/{project.id}",
    ):
        response = await httpx_client.delete(path, headers=headers)
        assert response.status_code == 409, response.text
    assert not delete_prefix.called
    with pytest.raises(Exception) as error:
        await delete_project_transaction(db_session, project.id, actor_id=owner.id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_inconsistent_legacy_attempt_reference_blocks_storage_deletion(
    httpx_client, db_session, project_admin, monkeypatch
):
    owner, token = project_admin
    project, _, _, model = await seed(db_session, owner)
    source_project, source_run, source_attempt = await seed_source(db_session, owner)
    model.attempt_id = source_attempt.id
    await db_session.flush()
    from app.services.storage import storage_service

    deletion = Mock(side_effect=AssertionError("must guard attempt FK before S3"))
    monkeypatch.setattr(storage_service, "delete_prefix", deletion)
    response = await httpx_client.delete(
        f"/api/v1/projects/{source_project.id}/training-runs/{source_run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409, response.text
    assert not deletion.called


@pytest.mark.asyncio
async def test_retained_prediction_counts_require_full_controlled_identity(
    db_session, project_admin, httpx_client
):
    owner, token = project_admin
    project, _, _, model = await seed(db_session, owner)
    _, deployment = await ready_deployment(db_session, project, model)
    task = await create_task(db_session, project_id=project.id)
    ref = build_reference(model, deployment)
    for mutation in (
        None,
        {"sha256": "wrong"},
        {"binding_digest": "wrong"},
        {"classes_digest": "wrong"},
        {"project_id": str(uuid.uuid4())},
        {"reference_digest": None},
    ):
        prediction = Prediction(
            project_id=project.id,
            task_id=task.id,
            model_version=f"trained:{model.id}",
            result=[],
        )
        db_session.add(prediction)
        await db_session.flush()
        db_session.add(
            PredictionMeta(
                prediction_id=prediction.id,
                prediction_created_at=prediction.created_at,
                extra={"trained_model": ref | (mutation or {})},
            )
        )
    legacy = Prediction(
        project_id=project.id,
        task_id=task.id,
        model_version=f"trained:{model.id}",
        result=[],
    )
    db_session.add(legacy)
    await db_session.flush()
    result = await get(
        httpx_client, token, project, "model", model.id, relation="predictions"
    )
    assert result["usage"]["retained_count"] == 1 and not result["usage"]["truncated"]
    model.artifact = {}
    await db_session.flush()
    result = await get(
        httpx_client, token, project, "model", model.id, relation="predictions"
    )
    assert result["usage"]["retained_count"] is None
    assert result["usage"]["unavailable_reason"] == "candidate_identity_incomplete"


@pytest.mark.asyncio
async def test_real_concurrent_decisions_and_publication_preserve_both_notes(
    test_engine,
):
    """Separate committed connections prove the shared advisory/model locks."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_id = user_id = registry_id = pool_id = None
    try:
        async with maker() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith(
                "_test"
            )
            owner, _ = await _create_user(
                db,
                "project_admin",
                f"lineage-{uuid.uuid4().hex}@test.local",
                "Lineage owner",
            )
            project, _, _, model = await seed(db, owner)
            project_id, user_id, model_id = project.id, owner.id, model.id
            registry, deployment = await ready_deployment(db, project, model)
            registry_id = registry.id
            pool_id = await db.scalar(
                select(MLBackendServicePool.id).where(
                    MLBackendServicePool.legacy_instance_id == registry.id
                )
            )
            release = await release_row(db, project, model, deployment, 1)
            db.add(
                ProjectModelSelection(
                    project_id=project.id, release_id=release.id, generation=1
                )
            )
            await db.commit()

        async def decision(note):
            async with maker() as db:
                project, user = (
                    await db.get(Project, project_id),
                    await db.get(User, user_id),
                )
                return await create_decision(
                    model_id,
                    ModelDecisionCreate(
                        request_id=uuid.uuid4(), disposition="research", note=note
                    ),
                    project,
                    user,
                    db,
                )

        async def publication():
            async with maker() as db:
                project, user = (
                    await db.get(Project, project_id),
                    await db.get(User, user_id),
                )
                return await publish_model(
                    ModelReleaseCreate(request_id=uuid.uuid4(), expected_generation=1),
                    project,
                    user,
                    db,
                )

        first, second, published = await asyncio.gather(
            decision("first"), decision("second"), publication()
        )
        assert {first.sequence, second.sequence} == {1, 2}
        async with maker() as db:
            selection = await db.get(ProjectModelSelection, (project_id, "detect"))
            assert selection.generation == 2 and selection.release_id == published.id
            notes = (
                await db.scalars(
                    select(ModelDecision).where(ModelDecision.project_id == project_id)
                )
            ).all()
            assert {n.note for n in notes} == {"first", "second"}
    finally:
        if project_id:
            async with maker() as db:
                await db.execute(
                    delete(ProjectModelSelection).where(
                        ProjectModelSelection.project_id == project_id
                    )
                )
                await db.execute(
                    delete(ModelRelease).where(ModelRelease.project_id == project_id)
                )
                await db.execute(
                    delete(ModelDecision).where(ModelDecision.project_id == project_id)
                )
                await db.execute(
                    delete(ModelDeployment).where(
                        ModelDeployment.model_version_id == model_id
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
                if pool_id:
                    await db.execute(
                        delete(MLBackendServicePool).where(
                            MLBackendServicePool.id == pool_id
                        )
                    )
                if registry_id:
                    await db.execute(
                        delete(MLBackendRegistry).where(
                            MLBackendRegistry.id == registry_id
                        )
                    )
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()

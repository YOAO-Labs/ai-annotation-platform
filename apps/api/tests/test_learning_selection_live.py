"""Exercise Learning evidence and controlled admission on ordinary PostgreSQL LOGINs."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.db.models.active_learning import ActiveLearningAttempt
from app.db.models.learning_policy import LearningTrigger
from app.db.models.training_run import TrainingRun
from app.deps import get_db
from app.schemas.training_run import TrainingRunCreate
from app.services import (
    active_learning_originals,
    training_claims,
    training_runs,
    training_studies,
)
from app.services.training_dataset_bridge import content_digest
from app.services.training_datasets import bind_training_authority
from app.workers.active_learning import run_round
from tests.active_learning_fixtures import complete, current_round, seed
from tests.factory import create_api_key, create_user
from tests.test_training_recipes import recipe_body
from tests.test_training_runs import GPU, TOKEN, executor_claim, ready_dataset
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401
from tests.test_training_studies import body as study_body, finish, native_claim
from tests.test_workspace_project_authority_locking import _api_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data verifier and actual LOGINs",
    ),
]

_EMBEDDED = "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"
_OWNER_ONLY = (
    "active_learning_original_history",
    "active_learning_original_decisions",
    "active_learning_pool_item_count",
    "active_learning_candidate_item_count",
)


def ordinary_url(test_engine, selector):
    verified = _api_url(test_engine)
    url = make_url(os.environ[selector])
    assert (url.host, url.port, url.database) == (
        verified.host,
        verified.port,
        verified.database,
    )
    return url


@pytest.mark.parametrize(
    "selector",
    (
        _EMBEDDED,
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "AAP_MODULE_DATA_LEGACY_RUNTIME_URL",
    ),
    ids=("lan-embedded", "lan-isolated-issuer", "worktree"),
)
async def test_learning_selection_execute_matrix(test_engine, selector):
    url = ordinary_url(test_engine, selector)
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert (
                await db.execute(text("SELECT current_user, session_user"))
            ).one() == (url.username, url.username)
            rows = (
                await db.execute(
                    text(r"""
                        SELECT p.proname::text AS name, p.prosecdef, p.proconfig,
                               has_function_privilege(p.oid, 'EXECUTE') AS allowed,
                               EXISTS (SELECT 1 FROM aclexplode(p.proacl) AS acl
                                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') AS public_execute
                        FROM pg_catalog.pg_proc AS p
                        WHERE p.pronamespace = 'yoao_learning_v1'::regnamespace
                          AND (p.proname LIKE 'selection\_%' OR p.proname::text = ANY(:owner_only))
                    """),
                    {"owner_only": _OWNER_ONLY},
                )
            ).all()
            assert {row.name for row in rows if not row.allowed} == set(_OWNER_ONLY)
            assert sum(row.allowed for row in rows) == 70
            assert all(
                row.prosecdef
                and row.proconfig == ["search_path=pg_catalog, pg_temp"]
                and not row.public_execute
                for row in rows
            )
            for statement in (
                "SELECT spec FROM public.active_learning_rounds LIMIT 0",
                "UPDATE public.active_learning_candidates SET evidence = NULL WHERE false",
                "INSERT INTO public.active_learning_feedback DEFAULT VALUES",
                "SELECT revision FROM public.learning_policies LIMIT 0",
                "UPDATE public.training_trials SET run_id = NULL WHERE false",
                "DELETE FROM public.training_recipe_revisions WHERE false",
                "SELECT yoao_learning_v1.active_learning_original_history()",
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(text(statement))
                assert denied.value.orig.sqlstate == "42501", statement
    finally:
        await engine.dispose()


@pytest.fixture
async def learning_login(test_engine, app_module):
    url = ordinary_url(test_engine, _EMBEDDED)
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def connect():
        async with factory() as db:
            assert (
                await db.execute(text("SELECT current_user, session_user"))
            ).one() == (url.username, url.username)
            yield db

    app_module.dependency_overrides[get_db] = connect
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://selection.test",
        ) as client:
            yield client, factory
    finally:
        app_module.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def new_manager(db):
    owner = await create_user(
        db, "super_admin", f"selection-{uuid4().hex}@example.test", "Selection owner"
    )
    token = create_access_token(subject=str(owner.id), role="super_admin")
    return owner, {"Authorization": f"Bearer {token}"}


@pytest.mark.usefixtures("training_config_fixture")
async def test_recipe_history_and_study_budget_via_real_login(
    test_engine, learning_login, monkeypatch
):
    client, factory = learning_login
    node = f"selection-{uuid4().hex}"
    monkeypatch.setattr(settings, "training_node_id", node)
    async with async_sessionmaker(
        test_engine, expire_on_commit=False
    ).begin() as seed_db:
        owner, headers = await new_manager(seed_db)
        project, dataset = await ready_dataset(seed_db, owner)
    root = f"/api/v1/projects/{project.id}"
    path = root + "/training-recipes"
    body = recipe_body()
    original = await client.post(path, headers=headers, json=body)
    assert original.status_code == 201, original.text
    item = original.json()
    revisions = f"{path}/{item['id']}/revisions"
    update = {**recipe_body(), "expected_head_revision": 1, "name": "Second"}
    second = await client.post(revisions, headers=headers, json=update)
    assert second.status_code == 201 and second.json()["head_revision"] == 2, (
        second.text
    )
    stale = await client.post(
        revisions, headers=headers, json={**update, "request_id": str(uuid4())}
    )
    assert stale.status_code == 409, stale.text
    replay = await client.post(path, headers=headers, json=body)
    assert replay.status_code == 201 and replay.json()["revision"] == item["revision"]

    path = root + "/training-studies"
    payload = study_body(dataset)
    preview = await client.post(path + ":preview", headers=headers, json=payload)
    assert preview.status_code == 200, preview.text
    payload["preview_digest"] = preview.json()["preview_digest"]
    created = await client.post(path, headers=headers, json=payload)
    assert created.status_code == 202, created.text
    study = created.json()
    claim = native_claim().model_copy(update={"resource_id": f"{node}/{GPU}"})
    claimed = await client.post(
        "/api/v1/internal/training/claim",
        headers={"x-training-executor-token": TOKEN},
        json=claim.model_dump(mode="json"),
    )
    assert claimed.status_code == 200, claimed.text
    attempt = claimed.json()["assignment"]
    assert attempt["run_id"] == study["trials"][0]["run_id"]
    assert attempt["native_budget"]["allowance_seconds"] == 10.0
    async with factory.begin() as db:
        await finish(db, claim, attempt, seconds=3.5)
        await finish(db, claim, attempt, seconds=3.5)
        await training_studies.coordinate(
            db, UUID(study["id"]), training_authority=bind_training_authority(db)
        )
    reread = await client.get(f"{path}/{study['id']}", headers=headers)
    assert reread.status_code == 200, reread.text
    result = reread.json()
    assert (
        result["plan_digest"] == study["plan_digest"]
        and result["plan"] == study["plan"]
    )
    assert result["accounting"]["consumed_seconds"] == 3.5
    assert result["accounting"]["reserved_seconds"] == 6.5
    cancelled = await client.post(f"{path}/{study['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and not cancelled.json()["scheduling"], (
        cancelled.text
    )


@pytest.mark.usefixtures("training_config_fixture")
async def test_cancelled_trigger_is_rejected_at_executor_claim(
    test_engine, learning_login, monkeypatch
):
    client, factory = learning_login
    node = f"selection-{uuid4().hex}"
    monkeypatch.setattr(settings, "training_node_id", node)
    owner_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner_factory.begin() as seed_db:
        owner, headers = await new_manager(seed_db)
        project, dataset = await ready_dataset(seed_db, owner)
    policy_path = f"/api/v1/projects/{project.id}/learning-policy"
    response = await client.put(
        policy_path,
        headers=headers,
        json={
            "expected_revision": 0,
            "enabled": True,
            "parent_id": str(dataset.id),
            "base_model": "yolo11n",
            "parameters": {"epochs": 3},
            "min_changed_samples": 1,
            "check_interval_minutes": 1,
            "cooldown_minutes": 0,
        },
    )
    assert response.status_code == 200, response.text
    policy = response.json()
    trigger_id = uuid4()
    async with owner_factory.begin() as seed_db:
        seed_db.add(
            LearningTrigger(
                id=trigger_id,
                policy_id=UUID(policy["id"]),
                project_id=project.id,
                policy_revision=policy["revision"],
                dataset_id=dataset.id,
                source_digest=dataset.snapshot_digest,
                status="queued",
            )
        )
    body = TrainingRunCreate(
        name="Automatic candidate",
        dataset_id=dataset.id,
        base_model="yolo11n",
        request_id=trigger_id,
        parameters={"epochs": 3},
    )
    async with factory.begin() as db:
        config = await training_runs.frozen_config(db, project.id, body)
        run = await training_runs.create_policy_child(
            db,
            project_id=project.id,
            creator_id=owner.id,
            dataset_id=dataset.id,
            initialization_model_id=None,
            auto_trigger_id=trigger_id,
            request_digest=content_digest(body.request_payload()),
            name=body.name,
            config=config,
            config_digest=content_digest(config),
            origin=None,
            authorization={"api_key_id": None},
        )
        assert await training_claims.policy_claim_allowed(db, run)
    async with owner_factory.begin() as seed_db:
        trigger = await seed_db.get(LearningTrigger, trigger_id)
        trigger.status = "cancelled"
    claim = executor_claim().model_copy(update={"resource_id": f"{node}/{GPU}"})
    result = await client.post(
        "/api/v1/internal/training/claim",
        headers={"x-training-executor-token": TOKEN},
        json=claim.model_dump(mode="json"),
    )
    assert result.status_code == 200 and result.json()["assignment"] is None, (
        result.text
    )
    async with owner_factory() as observer:
        assert (await observer.get(TrainingRun, run.id)).status == "cancelled"
    current = await client.get(policy_path, headers=headers)
    assert (
        current.status_code == 200
        and current.json()["enabled"]
        and current.json()["revision"] == policy["revision"]
    )


async def test_active_feedback_replay_via_real_login(
    test_engine, learning_login, monkeypatch
):
    client, _ = learning_login
    owner_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner_factory() as seed_db:
        owner, headers = await new_manager(seed_db)
        world = await seed(seed_db, owner, monkeypatch)
        key, token = await create_api_key(
            seed_db, owner.id, "Selection", sorted(active_learning_originals.SCOPES)
        )
        await seed_db.commit()
    selection_headers = {"Authorization": f"Bearer {token}"}
    root = f"/api/v1/projects/{world.project.id}/active-learning-rounds"
    selection = world.selection.model_dump(mode="json")
    preview = await client.post(
        root + ":preview", headers=selection_headers, json=selection
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["candidate_count"] == len(world.tasks)
    assert {
        (bucket, object_key)
        for operation, bucket, object_key in world.originals.calls
        if operation == "get"
    } == {(replica.bucket, replica.object_key) for replica in world.replicas.values()}

    published = []
    monkeypatch.setattr(
        run_round, "apply_async", lambda **values: published.append(values)
    )
    created = await client.post(
        root,
        headers=selection_headers,
        json={
            **selection,
            "request_id": str(uuid4()),
            "name": "Human selection",
            "preview_digest": preview.json()["preview_digest"],
        },
    )
    assert created.status_code == 202, created.text
    round_id = UUID(created.json()["id"])
    attempt_id = UUID(created.json()["current_attempt_id"])
    assert [message["args"] for message in published] == [[str(attempt_id)]]
    async with owner_factory() as seed_db:
        round = await current_round(seed_db, round_id)
        assert round.spec["original_contract"] == 1
        assert round.authorization["api_key_id"] == str(key.id)
        attempt = await seed_db.get(ActiveLearningAttempt, attempt_id)
        assert attempt is not None
        report = await complete(seed_db, round, attempt)
        await seed_db.commit()
    selected = next(row["ordinal"] for row in report["ranking"] if row["recommended"])
    decision = await client.post(
        f"{root}/{round_id}/decisions",
        headers=selection_headers,
        json={"request_id": str(uuid4()), "action": "accept", "ordinals": [selected]},
    )
    assert decision.status_code == 200 and decision.json()["accepted_batch_id"], (
        decision.text
    )
    path = f"{root}/{round_id}/feedback"
    body = {"request_id": str(uuid4())}
    first = await client.post(path, headers=headers, json=body)
    assert first.status_code == 200, first.text
    replay = await client.post(path, headers=headers, json=body)
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text
    changed = await client.post(
        path,
        headers=headers,
        json={
            **body,
            "evaluations": [
                {"evaluation_id": str(uuid4()), "attempt_id": str(uuid4())}
            ],
        },
    )
    assert (
        changed.status_code == 409
        and changed.json()["detail"]["code"] == "request_conflict"
    )
    history = await client.get(path, headers=headers)
    assert history.status_code == 200 and history.json() == {
        "total": 1,
        "items": [first.json()],
    }

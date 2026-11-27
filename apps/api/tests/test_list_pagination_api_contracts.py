"""HTTP regressions for list scoping, totals, and supported page sizes."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api.v1 import active_learning, model_lineage, sample_evaluations
from app.deps import get_current_user, get_db, require_project_owner
from app.schemas.sample_evaluation import EvaluationSamplePage

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def list_api():
    """Keep real routing/response validation while replacing database dependencies."""
    db = MagicMock()
    db.scalars = AsyncMock(return_value=MagicMock())
    db.scalar = AsyncMock()
    project = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), role="super_admin", is_active=True)

    async def fake_db():
        yield db

    async def fake_project():
        return project

    async def fake_user():
        return user

    app = FastAPI()
    for module in (active_learning, model_lineage, sample_evaluations):
        app.include_router(module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[require_project_owner] = fake_project
    app.dependency_overrides[get_current_user] = fake_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, db, project


@pytest.mark.parametrize("resource", ["decisions", "feedback"])
async def test_round_lists_count_and_page_only_the_requested_round(
    db_session, super_admin, resource
):
    from app.db.models.active_learning import (
        ActiveLearningDecision,
        ActiveLearningFeedback,
        ActiveLearningRound,
    )
    from app.services.training_dataset_bridge import content_digest
    from tests.sample_evaluation_fixtures import Objects, seed

    actor = super_admin[0]
    world = await seed(db_session, actor, Objects())
    rounds = [
        ActiveLearningRound(
            id=uuid.uuid4(),
            project_id=world.project.id,
            creator_id=actor.id,
            model_version_id=world.model.id,
            deployment_id=world.deployment.id,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            name=f"round {i}",
            status="failed",
            spec={"schema": 1, "pool": []},
            spec_digest="b" * 64,
        )
        for i in range(2)
    ]
    db_session.add_all(rounds)
    await db_session.flush()
    ids = [uuid.uuid4() for _ in range(5)]
    model = (
        ActiveLearningDecision if resource == "decisions" else ActiveLearningFeedback
    )
    for i, identity in enumerate(ids):
        snapshot = {}
        fields = dict(
            id=identity,
            round_id=rounds[0 if i < 3 else 1].id,
            actor_id=actor.id,
            request_id=uuid.uuid4(),
            request_digest="c" * 64,
            snapshot=snapshot,
            snapshot_digest=content_digest(snapshot),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
        )
        if resource == "decisions":
            fields["action"] = "reject"
        db_session.add(model(**fields))
    await db_session.flush()

    async def real_db():
        yield db_session

    async def project():
        return world.project

    async def user():
        return actor

    app = FastAPI()
    app.include_router(active_learning.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = real_db
    app.dependency_overrides[require_project_owner] = project
    app.dependency_overrides[get_current_user] = user
    base = f"/api/v1/projects/{world.project.id}/active-learning-rounds"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for round_id, offset, expected, total in (
            (rounds[0].id, 0, [ids[2], ids[1]], 3),
            (rounds[0].id, 2, [ids[0]], 3),
            (rounds[0].id, 3, [], 3),
            (rounds[1].id, 0, [ids[4], ids[3]], 2),
        ):
            response = await client.get(
                f"{base}/{round_id}/{resource}", params={"limit": 2, "offset": offset}
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == total
            assert [item["id"] for item in payload["items"]] == [
                str(i) for i in expected
            ]

        async def wrong_project():
            return SimpleNamespace(id=uuid.uuid4())

        app.dependency_overrides[require_project_owner] = wrong_project
        response = await client.get(f"{base}/{rounds[0].id}/{resource}")
        assert response.status_code == 404


@pytest.mark.parametrize("resource", ["decisions", "feedback"])
async def test_round_lists_do_not_query_counts_for_inaccessible_rounds(
    list_api, monkeypatch, resource
):
    client, db, project = list_api
    monkeypatch.setattr(
        active_learning.service,
        "load_round",
        AsyncMock(side_effect=HTTPException(status_code=404, detail="round missing")),
    )

    response = await client.get(
        f"/api/v1/projects/{project.id}/active-learning-rounds/{uuid.uuid4()}/{resource}"
    )

    assert response.status_code == 404
    db.scalars.assert_not_awaited()
    db.scalar.assert_not_awaited()


@pytest.mark.parametrize("resource", ["decisions", "feedback"])
@pytest.mark.parametrize("limit,expected_status", [(None, 200), (101, 422)])
async def test_round_list_default_and_oversized_pages(
    list_api, monkeypatch, resource, limit, expected_status
):
    client, db, project = list_api
    round_id = uuid.uuid4()
    load = AsyncMock(return_value=SimpleNamespace(id=round_id))
    page = AsyncMock(return_value=[])
    count = AsyncMock(return_value=0)
    monkeypatch.setattr(active_learning.service, "load_round", load)
    monkeypatch.setattr(
        active_learning.data,
        "decisions_page" if resource == "decisions" else "feedback_page",
        page,
    )
    monkeypatch.setattr(active_learning.data, "receipt_count", count)

    response = await client.get(
        f"/api/v1/projects/{project.id}/active-learning-rounds/{round_id}/{resource}",
        params={} if limit is None else {"limit": limit},
    )

    assert response.status_code == expected_status
    if expected_status == 200:
        page.assert_awaited_once_with(db, round_id, 20, 0)
        count.assert_awaited_once()
    else:
        load.assert_not_awaited()
        page.assert_not_awaited()
        count.assert_not_awaited()


@pytest.mark.parametrize("resource", ["lineage", "decisions"])
@pytest.mark.parametrize("limit,expected_status", [(None, 200), (100, 200), (101, 422)])
async def test_lineage_lists_accept_supported_sizes_and_preserve_cursor(
    list_api, monkeypatch, resource, limit, expected_status
):
    client, db, project = list_api
    node_id = uuid.uuid4()
    if resource == "lineage":
        call = AsyncMock(
            return_value={
                "node": {
                    "kind": "run",
                    "id": node_id,
                    "name": "Training run",
                    "created_at": "2026-10-21T00:00:00Z",
                },
                "inputs": [],
                "sections": ["attempts"],
                "relation": "attempts",
                "items": [],
                "next_cursor": "next-page",
            }
        )
        monkeypatch.setattr(model_lineage.service, "lineage", call)
        path = f"model-lineage/run/{node_id}"
        params = {"relation": "attempts", "cursor": "previous-page"}
        arguments = (db, project.id, "run", node_id, "attempts", "previous-page")
    else:
        call = AsyncMock(return_value={"items": [], "next_before": 40})
        monkeypatch.setattr(model_lineage.service, "decisions", call)
        path = f"trained-models/{node_id}/decisions"
        params = {"before": 80}
        arguments = (db, project.id, node_id, 80)
    if limit is not None:
        params["limit"] = limit

    response = await client.get(f"/api/v1/projects/{project.id}/{path}", params=params)

    assert response.status_code == expected_status
    if expected_status == 200:
        call.assert_awaited_once_with(*arguments, limit or 20)
        if resource == "lineage":
            assert response.json()["next_cursor"] == "next-page"
        else:
            assert response.json()["next_before"] == 40
    else:
        call.assert_not_awaited()


@pytest.fixture
def sample_report(monkeypatch):
    run = SimpleNamespace(
        id=uuid.uuid4(), spec={"classes": ["car"]}, spec_digest="a" * 64
    )
    attempt = SimpleNamespace(id=uuid.uuid4(), report={"sha256": "b" * 64})
    rows = [
        {
            "ordinal": ordinal,
            "task_id": uuid.uuid4(),
            "sha256": "a" * 64,
            "label_sha256": "b" * 64,
            "width": 640,
            "height": 480,
            "status": "pending",
            "truth": [],
            "request_id": uuid.uuid4(),
        }
        for ordinal in range(32)
    ]
    load = AsyncMock(return_value=run)
    monkeypatch.setattr(sample_evaluations.service, "load_run", load)
    monkeypatch.setattr(
        sample_evaluations, "attempt_for", AsyncMock(return_value=attempt)
    )
    monkeypatch.setattr(
        sample_evaluations.service,
        "read_report",
        AsyncMock(return_value={"samples": rows}),
    )
    return run, attempt, rows, load


@pytest.mark.parametrize(
    "limit,offset,expected_status", [(None, 0, 200), (100, 20, 200), (101, 0, 422)]
)
async def test_sample_pages_validate_real_response_model_at_supported_sizes(
    list_api, sample_report, limit, offset, expected_status
):
    client, db, project = list_api
    run, attempt, rows, load = sample_report
    params = {"offset": offset}
    if limit is not None:
        params["limit"] = limit

    response = await client.get(
        f"/api/v1/projects/{project.id}/sample-evaluations/"
        f"{run.id}/attempts/{attempt.id}/samples",
        params=params,
    )

    assert response.status_code == expected_status
    if expected_status == 200:
        body = response.json()
        effective_limit = limit or 12
        assert body["total"] == len(rows)
        assert body["limit"] == effective_limit
        assert body["offset"] == offset
        assert [row["ordinal"] for row in body["items"]] == list(
            range(32)[offset : offset + effective_limit]
        )
        load.assert_awaited_once_with(db, project.id, run.id)
    else:
        load.assert_not_awaited()


async def test_sample_total_counts_filtered_report_even_after_last_page(
    list_api, sample_report
):
    client, _, project = list_api
    run, attempt, rows, _ = sample_report
    for row in rows[:8]:
        row.update(status="failed", error_code="source_unavailable")

    response = await client.get(
        f"/api/v1/projects/{project.id}/sample-evaluations/"
        f"{run.id}/attempts/{attempt.id}/samples",
        params={"error": "failed", "offset": 10, "limit": 100},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 8
    assert response.json()["items"] == []


async def test_larger_page_size_preserves_thirty_two_sample_report_bound(sample_report):
    run, attempt, rows, _ = sample_report
    with pytest.raises(ValidationError, match="at most 32 items"):
        EvaluationSamplePage.model_validate(
            {
                "evaluation_id": run.id,
                "attempt_id": attempt.id,
                "report_sha256": attempt.report["sha256"],
                "spec_digest": run.spec_digest,
                "total": 33,
                "offset": 0,
                "limit": 100,
                "items": rows + rows[:1],
            }
        )

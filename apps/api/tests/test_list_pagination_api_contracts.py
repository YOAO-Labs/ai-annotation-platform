"""Database-free HTTP regressions for list totals and supported page sizes."""

import uuid
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
@pytest.mark.parametrize("total,row_count", [(230, 2), (230, 0), (None, 0)])
async def test_round_lists_count_all_scoped_rows_before_pagination(
    list_api, monkeypatch, resource, total, row_count
):
    client, db, project = list_api
    round_id = uuid.uuid4()
    loaded_round = SimpleNamespace(id=round_id)
    load = AsyncMock(return_value=loaded_round)
    monkeypatch.setattr(active_learning.service, "load_round", load)
    rows = [SimpleNamespace(id=uuid.uuid4()) for _ in range(row_count)]
    db.scalars.return_value.all.return_value = rows
    db.scalar.return_value = total
    serializer = MagicMock(side_effect=lambda row: {"id": str(row.id)})
    if resource == "decisions":
        monkeypatch.setattr(active_learning.service, "decision_output", serializer)
        model = active_learning.Decision
    else:
        monkeypatch.setattr(active_learning.feedback, "output", serializer)
        model = active_learning.Feedback

    response = await client.get(
        f"/api/v1/projects/{project.id}/active-learning-rounds/{round_id}/{resource}",
        params={"limit": 100, "offset": 100},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [{"id": str(row.id)} for row in rows],
        "total": total or 0,
    }
    load.assert_awaited_once_with(db, project.id, round_id)
    page_query = db.scalars.call_args.args[0]
    count_query = db.scalar.call_args.args[0]
    sql = str(page_query.compile(compile_kwargs={"literal_binds": True}))
    table = model.__tablename__
    assert "LIMIT 100 OFFSET 100" in sql
    assert str(page_query._order_by_clause) == (
        f"{table}.created_at DESC, {table}.id DESC"
    )
    assert list(page_query.compile().params.values()).count(round_id) == 1
    assert list(count_query.compile().params.values()) == [round_id]
    assert str(count_query.whereclause) == str(page_query.whereclause)
    count_sql = str(count_query)
    assert "count(*)" in count_sql
    assert f"FROM {table}" in count_sql
    assert "LIMIT" not in count_sql and "OFFSET" not in count_sql


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
    monkeypatch.setattr(active_learning.service, "load_round", load)
    db.scalars.return_value.all.return_value = []
    db.scalar.return_value = 0

    response = await client.get(
        f"/api/v1/projects/{project.id}/active-learning-rounds/{round_id}/{resource}",
        params={} if limit is None else {"limit": limit},
    )

    assert response.status_code == expected_status
    if expected_status == 200:
        sql = str(
            db.scalars.call_args.args[0].compile(compile_kwargs={"literal_binds": True})
        )
        assert "LIMIT 20 OFFSET 0" in sql
    else:
        load.assert_not_awaited()
        db.scalars.assert_not_awaited()
        db.scalar.assert_not_awaited()


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

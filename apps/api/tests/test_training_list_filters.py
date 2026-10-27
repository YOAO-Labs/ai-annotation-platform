"""Project training list search, UTC date bounds and filtered pagination."""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from training_contract.dataset import DETECTION_CONTRACT, SEGMENTATION_CONTRACT

from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun, TrainingAttempt
from app.db.models.trained_model import TrainedModelVersion
from app.db.models.model_decision import ModelDecision
from app.deps import get_db, require_project_owner
from app.services.training_list_filters import (
    created_date_bounds,
    literal_search_pattern,
)
from tests.factory import create_project


START = datetime(2026, 10, 18, tzinfo=timezone.utc)
END = datetime.combine(date(2026, 10, 19), time.max, timezone.utc)


@pytest.fixture
async def validation_client():
    from app.api.v1.training_datasets import list_versions
    from app.api.v1.training_runs import list_runs
    from app.api.v1.trained_models import list_models

    app = FastAPI()
    app.get("/training-runs")(list_runs)
    app.get("/training-datasets")(list_versions)
    app.get("/trained-models")(list_models)
    app.dependency_overrides[require_project_owner] = lambda: SimpleNamespace(
        id=uuid.uuid4()
    )
    app.dependency_overrides[get_db] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def test_literal_search_and_utc_calendar_bounds():
    assert literal_search_pattern("  COCO_8%\\  ") == "%COCO\\_8\\%\\\\%"
    assert literal_search_pattern(" \t ") is None
    assert created_date_bounds(START.date(), END.date()) == (START, END)
    assert created_date_bounds(None, None) == (None, None)
    assert created_date_bounds(date.max, date.max)[1] == datetime.max.replace(
        tzinfo=timezone.utc
    )
    with pytest.raises(HTTPException) as error:
        created_date_bounds(END.date(), START.date())
    assert error.value.status_code == 422


async def version(
    db, project, *, name="Frozen", task="detect", status="ready", **extra
):
    row = TrainingDatasetVersion(
        project_id=project.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name=name,
        snapshot={
            **(DETECTION_CONTRACT if task == "detect" else SEGMENTATION_CONTRACT),
            "classes": ["car"],
        },
        snapshot_digest="b" * 64,
        manifest={"summary": {"samples": 8}},
        status=status,
        created_at=START,
        **extra,
    )
    db.add(row)
    await db.flush()
    return row


async def run(
    db, project, dataset, *, name="Round", created_at=START, config=None, **extra
):
    row = TrainingRun(
        project_id=project.id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name=name,
        config=config or {"framework": "ultralytics", "base_model": {"id": "yolo11n"}},
        config_digest="b" * 64,
        status="succeeded",
        created_at=created_at,
        **extra,
    )
    db.add(row)
    await db.flush()
    return row


async def test_run_filters_precede_count_and_stable_pagination(
    db_session, super_admin, httpx_client, monkeypatch
):
    from app.services import training_adapters

    monkeypatch.setattr(training_adapters, "catalog", lambda: {"enabled": False})
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    dataset = await version(db_session, project)
    midpoint = START + timedelta(days=1, hours=12)
    matching = [
        await run(db_session, project, dataset, created_at=START),
        await run(
            db_session,
            project,
            dataset,
            created_at=END,
            config={
                "task": "image-detection",
                "framework": "ultralytics",
                "base_model": {"id": "yolo11n"},
            },
        ),
        await run(db_session, project, dataset, created_at=midpoint),
        await run(
            db_session,
            project,
            dataset,
            name="YOLO11N named experiment",
            created_at=midpoint,
            config={
                "framework": "ultralytics",
                "base_model": {"id": "other"},
                "configuration": {"identity": {"task": "image-detection"}},
            },
        ),
    ]
    for created_at in (
        START - timedelta(microseconds=1),
        END + timedelta(microseconds=1),
    ):
        await run(db_session, project, dataset, created_at=created_at)
    for changed in (
        {"task": "segment"},
        {"framework": "libreyolo"},
        {"base_model": {"id": "other"}},
    ):
        await run(
            db_session,
            project,
            dataset,
            config={
                "framework": "ultralytics",
                "base_model": {"id": "yolo11n"},
                **changed,
            },
        )
    failed = await run(db_session, project, dataset)
    failed.status = "failed"
    foreign = await create_project(db_session, owner_id=owner.id)
    await run(db_session, foreign, await version(db_session, foreign))
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-runs",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "q": " YOLO11N ",
            "status": "succeeded",
            "task": "detect",
            "framework": "ultralytics",
            "created_from": "2026-10-18",
            "created_to": "2026-10-19",
            "limit": 2,
            "offset": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(matching)
    ordered = sorted(
        matching, key=lambda item: (item.created_at, item.id), reverse=True
    )
    assert [item["id"] for item in response.json()["items"]] == [
        str(item.id) for item in ordered[1:3]
    ]


async def test_dataset_literal_search_dates_and_filtered_pagination(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    name = "COCO_8%\\ fixture"
    matching = []
    for created_at in (START, END, END - timedelta(hours=1), END - timedelta(hours=1)):
        row = await version(db_session, project, name=name)
        row.created_at = created_at
        matching.append(row)
    await version(db_session, project, name="COCOx8xxxx fixture")
    await version(db_session, project, name=name, status="failed")
    await version(db_session, project, name=name, task="segment")
    for created_at in (
        START - timedelta(microseconds=1),
        END + timedelta(microseconds=1),
    ):
        row = await version(db_session, project, name=name)
        row.created_at = created_at
    foreign = await create_project(db_session, owner_id=owner.id)
    await version(db_session, foreign, name=name)
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/training-datasets",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "q": " COCO_8%\\ ",
            "status": "ready",
            "task": "detect",
            "created_from": "2026-10-18",
            "created_to": "2026-10-19",
            "limit": 2,
            "offset": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(matching)
    ordered = sorted(
        matching, key=lambda item: (item.created_at, item.id), reverse=True
    )
    assert [item["id"] for item in response.json()["items"]] == [
        str(item.id) for item in ordered[1:3]
    ]


@pytest.mark.parametrize("endpoint", ["training-runs", "training-datasets"])
@pytest.mark.parametrize(
    "parameters",
    [
        {"q": "x" * 121},
        {"status": "unknown"},
        {"task": "unknown"},
        {"created_from": "2026-13-01"},
        {"created_to": "not-a-date"},
        {"created_from": "0"},
        {"created_to": "2026-10-19T00:00:00Z"},
        {"created_from": "2026-10-19", "created_to": "2026-10-18"},
    ],
)
async def test_training_list_filters_reject_invalid_values(
    validation_client, endpoint, parameters
):
    response = await validation_client.get(f"/{endpoint}", params=parameters)
    assert response.status_code == 422, response.text


async def test_run_list_filter_rejects_unknown_framework(
    validation_client,
):
    response = await validation_client.get(
        "/training-runs", params={"framework": "unknown"}
    )
    assert response.status_code == 422, response.text


async def test_model_filters_precede_count_pagination_and_archive_selection(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    dataset = await version(db_session, project)
    experiment = await run(db_session, project, dataset)
    attempt = TrainingAttempt(run_id=experiment.id, ordinal=1, status="succeeded")
    db_session.add(attempt)
    await db_session.flush()
    name = "COCO_8%\\ candidate"

    async def candidate(
        project_id=project.id, created_at=START, framework="ultralytics", label=name
    ):
        row = TrainedModelVersion(
            project_id=project_id,
            run_id=experiment.id,
            attempt_id=attempt.id,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            name=label,
            framework=framework,
            framework_version="fixture",
            artifact={},
            classes=["car"],
            bindings=["car"],
            binding_digest="b" * 64,
            provenance={},
            evaluation={},
            created_at=created_at,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    matching = [
        await candidate(created_at=at)
        for at in (START, END, END, END - timedelta(hours=1))
    ]
    await candidate(label="COCOx8xxxx candidate")
    await candidate(framework="libreyolo")
    await candidate(created_at=START - timedelta(microseconds=1))
    await candidate(created_at=END + timedelta(microseconds=1))
    foreign = await create_project(db_session, owner_id=owner.id)
    await candidate(project_id=foreign.id)
    archived = await candidate()
    db_session.add(
        ModelDecision(
            project_id=project.id,
            model_version_id=archived.id,
            creator_label="fixture",
            sequence=1,
            request_id=uuid.uuid4(),
            request_digest="c" * 64,
            disposition="archived",
            note="Archive candidate",
        )
    )
    await db_session.flush()
    base = f"/api/v1/projects/{project.id}/trained-models"
    params = {
        "q": " COCO_8%\\ ",
        "framework": "ultralytics",
        "created_from": "2026-10-18",
        "created_to": "2026-10-19",
        "limit": 2,
        "offset": 1,
    }
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.get(base, headers=headers, params=params)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 4
    ordered = sorted(
        matching, key=lambda item: (item.created_at, item.id), reverse=True
    )
    assert [item["id"] for item in response.json()["items"]] == [
        str(item.id) for item in ordered[1:3]
    ]
    for archive, total in (("include", 5), ("only", 1)):
        response = await httpx_client.get(
            base, headers=headers, params={**params, "archive": archive, "offset": 0}
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == total
        if archive == "only":
            assert response.json()["items"][0]["id"] == str(archived.id)


@pytest.mark.parametrize(
    "parameters",
    [
        {"q": "x" * 121},
        {"framework": "unknown"},
        {"created_from": "2026-13-01"},
        {"created_to": "not-a-date"},
        {"created_from": "0"},
        {"created_to": "2026-10-19T00:00:00Z"},
        {"created_from": "2026-10-19", "created_to": "2026-10-18"},
    ],
)
async def test_model_filters_reject_invalid_values(validation_client, parameters):
    response = await validation_client.get("/trained-models", params=parameters)
    assert response.status_code == 422, response.text

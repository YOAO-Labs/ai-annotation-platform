from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_catalog.associations_contract import (
    ASSOCIATIONS_SCHEMA,
    DatasetProjectsResult,
    LinkedProjectSnapshot,
    ListDatasetProjectsRequest,
    ListProjectDatasetsRequest,
    ProjectDatasetSummary,
    ProjectDatasetsResult,
)
from app.api.v1 import datasets as dataset_routes
from app.api.v1 import projects as project_routes
from app.schemas.project import ProjectOut
from app.services import assets_catalog


def _reply(data: dict) -> dict:
    return {"schema": ASSOCIATIONS_SCHEMA, "ok": True, "data": data}


def _failure(code: str) -> dict:
    return {"schema": ASSOCIATIONS_SCHEMA, "ok": False, "error": {"code": code}}


def _db(payload: dict) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = SimpleNamespace(scalar_one=lambda: payload)
    return session


def _project(project_id: uuid.UUID) -> LinkedProjectSnapshot:
    now = datetime.now(UTC)
    return LinkedProjectSnapshot(
        id=project_id,
        organization_id=None,
        display_id="P-100",
        name="Project",
        type_label="Detection",
        type_key="image-detection",
        data_type="image",
        owner_id=uuid.uuid4(),
        status="in_progress",
        ai_enabled=False,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
        label_config={},
        sampling="sequence",
        maximum_annotations=1,
        show_overlap_first=False,
        iou_dedup_threshold=0.7,
        box_threshold=0.35,
        text_threshold=0.25,
        ai_interactive_enabled=True,
        raster_mask_native_editing_enabled=True,
        mask_qc_config={},
        point_cloud_quality_config={},
        rendering_config={},
        video_sampling={},
        video_collaboration={},
        scene_mode=False,
        prefer_same_scene_continuation=False,
        scene_continuation_window_min=30,
        default_variants={},
        preannotate_pipeline=None,
        annotation_guide=None,
        guide_assets=[],
        guide_assets_revision=1,
        model_version=None,
        task_lock_ttl_seconds=300,
        due_date=None,
        created_at=now,
        updated_at=now,
        total_tasks=9,
        completed_tasks=3,
        review_tasks=2,
        in_progress_tasks=4,
        batch_summary={"total": 9, "assigned": 4, "in_review": 2},
    )


@pytest.mark.asyncio
async def test_dataset_projects_local_call_uses_actor_and_parent_scope():
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    project = _project(uuid.uuid4())
    db = _db(
        _reply(
            {
                "dataset_id": str(dataset_id),
                "projects": [project.model_dump(mode="json")],
            }
        )
    )

    result = await assets_catalog.AssetsCatalogGateway(db).list_dataset_projects(
        ListDatasetProjectsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert result.dataset_id == dataset_id
    sql, params = db.execute.await_args.args
    assert "aap_assets_v1.list_dataset_projects" in str(sql)
    assert params == {"actor_id": actor_id, "dataset_id": dataset_id}


@pytest.mark.asyncio
async def test_project_datasets_local_call_uses_current_actor_and_scope():
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    linked_at = datetime(2026, 10, 18, 8, 30, tzinfo=UTC)
    row = ProjectDatasetSummary(
        id=uuid.uuid4(),
        display_id="D-20",
        name="Dataset",
        data_type="image",
        linked_at=linked_at,
        items_count=12,
        tasks_in_project=7,
    )
    db = _db(
        _reply(
            {
                "project_id": str(project_id),
                "datasets": [row.model_dump(mode="json")],
            }
        )
    )

    result = await assets_catalog.AssetsCatalogGateway(db).list_project_datasets(
        ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
    )

    assert result.datasets[0].linked_at == linked_at
    sql, params = db.execute.await_args.args
    assert "aap_assets_v1.list_project_datasets" in str(sql)
    assert params == {"actor_id": actor_id, "project_id": project_id}


@pytest.mark.asyncio
async def test_remote_association_call_is_authenticated_and_never_falls_back(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)

    monkeypatch.setattr(
        assets_catalog.settings, "assets_catalog_url", "http://catalog.local"
    )
    monkeypatch.setattr(assets_catalog.settings, "assets_catalog_token", "s" * 40)
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/catalog/dataset-projects"
        assert request.headers["Authorization"] == "Bearer " + "s" * 40
        assert json.loads(request.content) == {
            "actor_id": str(actor_id),
            "dataset_id": str(dataset_id),
        }
        return httpx.Response(
            200,
            json=_reply({"dataset_id": str(dataset_id), "projects": []}),
        )

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(assets_catalog.httpx, "AsyncClient", make_client)
    result = await assets_catalog.AssetsCatalogGateway(db).list_dataset_projects(
        ListDatasetProjectsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert result.projects == []
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_association_failures_keep_existing_http_status_and_bearer_header():
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()

    with pytest.raises(HTTPException) as hidden:
        await assets_catalog.AssetsCatalogGateway(
            _db(_failure("project_missing"))
        ).list_project_datasets(
            ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail == "项目不存在"

    with pytest.raises(HTTPException) as inactive:
        await assets_catalog.AssetsCatalogGateway(
            _db(_failure("account_not_active"))
        ).list_project_datasets(
            ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
        )
    assert inactive.value.status_code == 401
    assert inactive.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
async def test_association_gateway_rejects_parent_mismatch_and_duplicate_rows():
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    dataset_id = uuid.uuid4()

    with pytest.raises(assets_catalog.CatalogGatewayError) as wrong_parent:
        await assets_catalog.AssetsCatalogGateway(
            _db(_reply({"project_id": str(uuid.uuid4()), "datasets": []}))
        ).list_project_datasets(
            ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
        )
    assert wrong_parent.value.status_code == 502

    duplicate = ProjectDatasetSummary(
        id=dataset_id,
        display_id="D-20",
        name="Dataset",
        data_type="image",
        linked_at=None,
        items_count=1,
        tasks_in_project=0,
    ).model_dump(mode="json")
    with pytest.raises(assets_catalog.CatalogGatewayError) as duplicate_rows:
        await assets_catalog.AssetsCatalogGateway(
            _db(
                _reply(
                    {
                        "project_id": str(project_id),
                        "datasets": [duplicate, duplicate],
                    }
                )
            )
        ).list_project_datasets(
            ListProjectDatasetsRequest(actor_id=actor_id, project_id=project_id)
        )
    assert duplicate_rows.value.status_code == 502


@pytest.mark.asyncio
async def test_native_association_database_failure_is_sanitized_as_unavailable():
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = SQLAlchemyError("private database detail")

    with pytest.raises(assets_catalog.CatalogGatewayError) as failure:
        await assets_catalog.AssetsCatalogGateway(db).list_project_datasets(
            ListProjectDatasetsRequest(actor_id=uuid.uuid4(), project_id=uuid.uuid4())
        )

    assert failure.value.status_code == 503
    assert failure.value.detail == "Assets catalog service is unavailable"


@pytest.mark.asyncio
async def test_dataset_route_keeps_bare_project_defaults_without_dataset_precheck(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    project_id = uuid.uuid4()
    snapshot = _project(project_id)
    gateway = SimpleNamespace(
        list_dataset_projects=AsyncMock(
            return_value=DatasetProjectsResult(
                dataset_id=dataset_id, projects=[snapshot]
            )
        )
    )
    monkeypatch.setattr(dataset_routes, "AssetsCatalogGateway", lambda _db: gateway)

    projects = await dataset_routes.get_linked_projects(
        dataset_id=dataset_id,
        db=AsyncMock(spec=AsyncSession),
        current_user=SimpleNamespace(id=actor_id),
    )

    request = gateway.list_dataset_projects.await_args.args[0]
    assert request.actor_id == actor_id
    assert request.dataset_id == dataset_id
    assert isinstance(projects[0], ProjectOut)
    assert projects[0].classes == ["car"]
    assert projects[0].owner_name is None
    assert projects[0].owner_avatar_ref is None
    assert projects[0].member_count == 0
    assert projects[0].my_project_role is None
    assert projects[0].ml_backend_id is None
    assert projects[0].ai_completed_tasks == 0
    assert projects[0].capability_warnings == []


@pytest.mark.asyncio
async def test_project_route_returns_only_project_dataset_summary_models(monkeypatch):
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    linked_at = datetime(2026, 10, 18, 8, 30, tzinfo=UTC)
    result = ProjectDatasetsResult(
        project_id=project_id,
        datasets=[
            ProjectDatasetSummary(
                id=uuid.uuid4(),
                display_id="D-20",
                name="Dataset",
                data_type="image",
                linked_at=linked_at,
                items_count=12,
                tasks_in_project=7,
            )
        ],
    )
    gateway = SimpleNamespace(list_project_datasets=AsyncMock(return_value=result))
    monkeypatch.setattr(project_routes, "AssetsCatalogGateway", lambda _db: gateway)

    datasets = await project_routes.list_project_datasets(
        project_id=project_id,
        db=AsyncMock(spec=AsyncSession),
        current_user=SimpleNamespace(id=actor_id),
    )

    request = gateway.list_project_datasets.await_args.args[0]
    assert request.actor_id == actor_id
    assert request.project_id == project_id
    assert len(datasets) == 1
    assert set(datasets[0].model_dump(mode="json")) == {
        "id",
        "display_id",
        "name",
        "data_type",
        "linked_at",
        "items_count",
        "tasks_in_project",
    }
    assert datasets[0].model_dump(mode="json")["linked_at"].endswith("+00:00")

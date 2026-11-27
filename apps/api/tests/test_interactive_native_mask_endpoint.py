from __future__ import annotations

import hashlib
import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
from PIL import Image
import pytest
from yoao_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    encode_low_res_mask,
    native_mask_candidate_id,
)

from app.config import settings
from app.core.security import create_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_prediction_input_session_factory
from app.services import managed_original_reader
from app.services.dataset_assets import register_item_asset
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from yoao_inference_contract.backend_control import BackendControlSetupResult
from yoao_inference_contract.execution import (
    ExecutionProvenance,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
    thaw_json,
)
from app.services.ai_mask_session import verify_ai_mask_session
from app.services.ai_mask_receipt import verify_ai_mask_receipt
from tests.conftest import create_registry_with_pool
from tests.test_managed_original_reader import Provider
from tests.test_prediction_inputs import input_storage_world


@pytest.fixture
async def db_session(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    tracked = {
        "tasks": [],
        "projects": [],
        "datasets": [],
        "items": [],
        "backends": [],
        "pools": [],
        "users": [],
    }
    async with maker() as db:
        db.info["cleanup"] = tracked
        try:
            yield db
        finally:
            await db.rollback()
            async with maker() as cleanup:
                if tracked["projects"]:
                    await cleanup.execute(
                        delete(Task).where(Task.project_id.in_(tracked["projects"]))
                    )
                    await cleanup.execute(
                        delete(ProjectMLBackendPool).where(
                            ProjectMLBackendPool.project_id.in_(tracked["projects"])
                        )
                    )
                if tracked["pools"]:
                    await cleanup.execute(
                        delete(MLBackendPoolMember).where(
                            MLBackendPoolMember.pool_id.in_(tracked["pools"])
                        )
                    )
                    await cleanup.execute(
                        delete(MLBackendServicePool).where(
                            MLBackendServicePool.id.in_(tracked["pools"])
                        )
                    )
                if tracked["backends"]:
                    await cleanup.execute(
                        delete(MLBackendRegistry).where(
                            MLBackendRegistry.id.in_(tracked["backends"])
                        )
                    )
                if tracked["items"]:
                    asset_ids = list(
                        await cleanup.scalars(
                            select(DatasetAsset.id).where(
                                DatasetAsset.dataset_item_id.in_(tracked["items"])
                            )
                        )
                    )
                    if asset_ids:
                        await cleanup.execute(
                            delete(DatasetAssetLocation).where(
                                DatasetAssetLocation.asset_id.in_(asset_ids)
                            )
                        )
                        await cleanup.execute(
                            delete(DatasetAsset).where(DatasetAsset.id.in_(asset_ids))
                        )
                if tracked["datasets"]:
                    await cleanup.execute(
                        delete(DatasetItem).where(
                            DatasetItem.dataset_id.in_(tracked["datasets"])
                        )
                    )
                    await cleanup.execute(
                        delete(Dataset).where(Dataset.id.in_(tracked["datasets"]))
                    )
                if tracked["projects"]:
                    await cleanup.execute(
                        delete(Project).where(Project.id.in_(tracked["projects"]))
                    )
                if tracked["users"]:
                    await cleanup.execute(
                        delete(ApiKey).where(ApiKey.user_id.in_(tracked["users"]))
                    )
                    await cleanup.execute(
                        delete(User).where(User.id.in_(tracked["users"]))
                    )
                await cleanup.commit()


@pytest.fixture
async def super_admin(db_session):
    user_id = uuid.uuid4()
    name = f"nm_admin_{user_id.hex[:8]}"
    user = User(
        id=user_id,
        name=name,
        email=f"{name}@test.local",
        password_hash=hash_password("Test1234!"),
        role="super_admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.info["cleanup"]["users"].append(user_id)
    await db_session.commit()
    return user, create_access_token(str(user_id), "super_admin")


@pytest.fixture(autouse=True)
async def _managed_original_io(test_engine, app_module, monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buf, format="PNG")
    raw = buf.getvalue()
    source = Provider()
    scheme = "https" if settings.minio_use_ssl else "http"
    source.meta.endpoint_url = f"{scheme}://{settings.minio_endpoint}"
    source.raw = source.actual = raw
    monkeypatch.setattr(managed_original_reader, "create_client", lambda **_: source)

    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async with input_storage_world(test_engine, monkeypatch):
        previous = app_module.dependency_overrides.get(
            get_prediction_input_session_factory
        )
        app_module.dependency_overrides[get_prediction_input_session_factory] = lambda: (
            maker
        )
        try:
            yield raw
        finally:
            if previous is None:
                app_module.dependency_overrides.pop(
                    get_prediction_input_session_factory, None
                )
            else:
                app_module.dependency_overrides[
                    get_prediction_input_session_factory
                ] = previous


async def _seed(db, owner_id):
    suffix = uuid.uuid4().hex[:8]
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buf, format="PNG")
    raw = buf.getvalue()
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-NM-{suffix}",
        name=f"native-mask-{suffix}",
        type_label="image-seg",
        type_key="image-seg",
        owner_id=owner_id,
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-NM-{suffix}",
        name=f"native-mask-{suffix}",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="image.png",
        file_path=f"native-mask/{suffix}/image.png",
        file_type="image",
        file_size=len(raw),
        width=3,
        height=2,
    )
    db.add(item)
    await db.flush()
    await register_item_asset(
        db, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )
    backend, pool = await create_registry_with_pool(
        db,
        name=f"native-mask-{suffix}",
        url=f"http://native-mask-{suffix}/",
        is_interactive=True,
        state="connected",
        enabled_pool=True,
    )
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-NM-{suffix}",
        file_name="image.png",
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    tracked = db.info.get("cleanup")
    if tracked is not None:
        tracked["projects"].append(project.id)
        tracked["datasets"].append(dataset.id)
        tracked["items"].append(item.id)
        tracked["backends"].append(backend.id)
        tracked["pools"].append(pool.id)
        tracked["tasks"].append(task.id)
    return project, backend, task, pool


def _setup(outputs: list[str]) -> dict:
    return {
        "name": "test-native-mask",
        "infra": "pytorch",
        "models": [
            {
                "id": "native-interactive",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": ["point", "interactive_box", "mask", "scribble"],
                "supported_inputs": [
                    "full_image",
                    "point_prompt",
                    "bbox_prompt",
                    "mask_prompt",
                    "scribble_prompt",
                ],
                "supported_geometric_outputs": outputs,
            },
            {
                "id": "mask-tracker",
                "task": "tracker",
                "is_interactive": True,
                "supported_prompts": ["bbox"],
                "supported_inputs": ["video"],
                "supported_geometric_outputs": ["bbox", "polygon", "mask"],
            },
        ],
    }


def _candidate(prompt_revision: str) -> dict:
    rle = CocoRlePayload(
        encoding="coco_rle",
        size=[2, 3],
        counts=[1, 2, 2, 1],
    )
    return NativeMaskCandidate(
        value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
        score=0.91,
        candidate_id=native_mask_candidate_id(
            rle,
            prompt_revision=prompt_revision,
            candidate_index=0,
        ),
    ).model_dump(mode="json")


def _url(project, backend) -> str:
    return (
        f"/api/v1/projects/{project.id}/ml-backends/{backend.id}/interactive-annotating"
    )


def _patch_setup(monkeypatch, setup):
    async def setup_backend(registry_id):
        return BackendControlSetupResult(registry_id=registry_id, setup=setup)

    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(setup=setup_backend),
    )


def _patch_execution(monkeypatch, captured, *, pool_id, respond):
    class FakeExecutionPort:
        async def execute(self, operation, *, before_dispatch=None):
            if before_dispatch is not None:
                await before_dispatch()
            captured["model_called"] = True
            task_data = thaw_json(operation.task_inputs[0])
            context = thaw_json(operation.context) or {}
            captured["operation"] = operation
            captured["task_data"] = task_data
            captured["context"] = context
            if respond is None:
                raise AssertionError("native output guard allowed model execution")
            result = respond(operation, task_data, context)
            registry_id = operation.requested_registry_id
            return ModelOperationResult(
                request_id=operation.request_id,
                operation=operation.operation,
                results=(result,),
                provenance=ExecutionProvenance(
                    requested_registry_id=operation.requested_registry_id,
                    requested_pool_id=operation.requested_pool_id,
                    registry_id=registry_id,
                    pool_id=pool_id,
                    instance_id=registry_id,
                    route_scope_id=uuid.uuid4(),
                    attempt_id=operation.request_id,
                    topology_generation=operation.topology_generation,
                    member_id=operation.selected_member_id,
                ),
                observation=ModelExecutionObservation(
                    backend_id=str(registry_id),
                    operation="predict_interactive",
                    outcome="success",
                    elapsed_seconds=0.001,
                ),
                wall_time_ms=1,
            )

    monkeypatch.setattr(
        "app.api.v1.ml_backends.model_execution_port",
        lambda *_args, **_kwargs: FakeExecutionPort(),
    )


async def test_native_endpoint_rejects_when_only_tracker_supports_mask(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    captured = {}
    _patch_setup(monkeypatch, _setup(["polygon"]))
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=None,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "labels": [1],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unsupported_output_geometry"
    assert not captured.get("model_called")


async def test_mask_prompt_source_requires_annotations_read_scope(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, _ = await _seed(db_session, user.id)
    await db_session.commit()
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        json={"name": "mask-scope-test", "scopes": ["datasets:read"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201, created.text
    api_key = created.json()["plaintext"]
    resolver = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.ml_backends.resolve_authorized_mask_prompt",
        resolver,
    )

    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "mask_prompt_source": {
                    "annotation_id": str(uuid.uuid4()),
                    "source_version": 1,
                },
            },
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 403
    assert "annotations:read" in response.json()["detail"]
    resolver.assert_not_awaited()


@pytest.mark.parametrize(
    ("backend_result", "diagnostic", "expected_status", "expected_reason"),
    [
        (
            [{"type": "mask", "value": {"rle": {}}, "candidate_id": "bad"}],
            None,
            502,
            "invalid_mask_payload",
        ),
        (
            [
                {
                    "type": "mask",
                    "value": {
                        "rle": {
                            "encoding": "coco_rle",
                            "size": [1, 4097],
                            "counts": [4097],
                        },
                        "masklabels": ["object"],
                    },
                    "score": 0.5,
                    "candidate_id": "sha256:" + "0" * 64,
                }
            ],
            None,
            413,
            "mask_payload_too_large",
        ),
        ([], {"reason": "empty_mask", "retryable": False}, 200, None),
    ],
)
async def test_native_endpoint_normalizes_error_and_empty_contracts(
    httpx_client,
    super_admin,
    db_session,
    monkeypatch,
    backend_result,
    diagnostic,
    expected_status,
    expected_reason,
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    _patch_setup(monkeypatch, _setup(["polygon", "mask"]))

    def respond(operation, task_data, _context):
        assert operation.max_response_bytes == 16 * 1024 * 1024
        return ModelTaskResult(
            task_id=task_data["id"],
            result=backend_result,
            diagnostic=diagnostic,
            score=0.73,
            model_version="native-mask-model",
            inference_time_ms=17,
            cache_hit=False,
            model_load_ms=29,
            meta={"result_origin": "test"},
        )

    captured = {}
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=respond,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "labels": [1],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == expected_status, response.text
    if expected_reason is not None:
        assert response.json()["detail"]["reason"] == expected_reason
    else:
        assert response.json()["result"] == []
        assert response.json()["diagnostic"]["reason"] == "empty_mask"
        assert response.json()["score"] == pytest.approx(0.73)
        assert response.json()["model_version"] == "native-mask-model"
        assert response.json()["inference_time_ms"] == 17
        assert response.json()["cache_hit"] is False
        assert response.json()["model_load_ms"] == 29


async def test_native_endpoint_returns_valid_candidate_with_route_lineage(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    backend_id = str(backend.id)
    pool_id = str(pool.id)
    task_id = str(task.id)
    await db_session.commit()

    raw_mask_input = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    _patch_setup(monkeypatch, _setup(["polygon", "mask"]))

    def respond(_operation, task_data, context):
        return ModelTaskResult(
            task_id=task_data["id"],
            result=(_candidate(context["prompt_revision"]),),
            score=0.86,
            model_version="test-model",
            inference_time_ms=19,
            cache_hit=True,
            model_load_ms=23,
            meta={"output": "native-mask"},
            mask_input_next=raw_mask_input,
        )

    captured = {}
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=respond,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": task_id,
            "context": {
                "type": "point",
                "points": [[0.5, 0.5]],
                "labels": [1],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"][0]["value"]["rle"]["size"] == [2, 3]
    assert payload["model_version"] == "test-model"
    assert payload["score"] == pytest.approx(0.86)
    assert payload["inference_time_ms"] == 19
    assert payload["cache_hit"] is True
    assert payload["model_load_ms"] == 23
    assert payload["routing"]["requested_backend_id"] == backend_id
    assert payload["routing"]["backend_instance_id"] == backend_id
    assert payload["routing"]["backend_pool_id"] == pool_id
    assert payload["routing"]["model_id"] == "native-interactive"
    claims = verify_ai_mask_session(payload["mask_input_next"])
    assert claims["task_id"] == task_id
    assert claims["frame_index"] is None
    assert claims["requested_backend_id"] == backend_id
    assert claims["model_id"] == "native-interactive"
    assert claims["model_variants"] == {}
    receipt = verify_ai_mask_receipt(
        payload["accept_receipts"][payload["result"][0]["candidate_id"]]
    )
    assert receipt["prompt_summary"] == payload["prompt_summary"]
    assert receipt["prompt_summary"]["positive_points"] == 1
    assert receipt["accept_target"] == {
        "mode": "create",
        "source_annotation_id": None,
        "source_version": None,
        "frame_index": None,
    }
    assert receipt["prompt_source"] is None


async def test_native_endpoint_receipt_counts_positive_and_negative_scribbles(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, backend, task, pool = await _seed(db_session, user.id)
    await db_session.commit()

    _patch_setup(monkeypatch, _setup(["polygon", "mask"]))

    def respond(_operation, task_data, context):
        return ModelTaskResult(
            task_id=task_data["id"],
            result=(_candidate(context["prompt_revision"]),),
            score=0.86,
            model_version="test-model",
            inference_time_ms=19,
            cache_hit=True,
            model_load_ms=23,
            meta={"output": "native-mask"},
        )

    captured = {}
    _patch_execution(
        monkeypatch,
        captured,
        pool_id=pool.id,
        respond=respond,
    )
    response = await httpx_client.post(
        _url(project, backend),
        json={
            "task_id": str(task.id),
            "context": {
                "type": "scribble",
                "scribbles": [
                    {
                        "polarity": 1,
                        "points": [[0.1, 0.1], [0.4, 0.4]],
                        "width": 0.01,
                    },
                    {
                        "polarity": 0,
                        "points": [[0.6, 0.6], [0.8, 0.8]],
                        "width": 0.01,
                    },
                ],
                "output_geometry": "mask",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    summary = payload["prompt_summary"]
    assert summary["family"] == "scribble"
    assert summary["positive_scribbles"] == 1
    assert summary["negative_scribbles"] == 1
    receipt = verify_ai_mask_receipt(
        payload["accept_receipts"][payload["result"][0]["candidate_id"]]
    )
    assert receipt["prompt_summary"] == summary

"""interactive-annotating 的 DINO 阈值透传契约。

平台按请求的 context 透传阈值，不注入项目级 box_threshold 或 text_threshold，
以免向不支持它们的 backend 发送参数。工作台按 backend 的 /setup.params 渲染
并保存用户偏好，再随请求传入。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from types import SimpleNamespace

from PIL import Image
import pytest

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
    name = f"it_admin_{user_id.hex[:8]}"
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
    Image.new("RGB", (12, 8), "blue").save(buf, format="PNG")
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


async def _seed(db, owner_id, *, box=0.4, text=0.3):
    suffix = uuid.uuid4().hex[:8]
    buf = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(buf, format="PNG")
    raw = buf.getvalue()
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-IT-{suffix}",
        name=f"it-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
        box_threshold=box,
        text_threshold=text,
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-IT-{suffix}",
        name=f"it-{suffix}",
        created_by=owner_id,
    )
    db.add_all([proj, dataset])
    await db.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="img.png",
        file_path=f"it/{suffix}/img.png",
        file_type="image",
        file_size=len(raw),
        width=12,
        height=8,
    )
    db.add(item)
    await db.flush()
    await register_item_asset(
        db, item, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
    )

    backend, pool = await create_registry_with_pool(
        db,
        name="dino-sam2",
        url=f"http://example-{suffix}/",
        is_interactive=True,
        state="connected",
        enabled_pool=True,
    )
    db.add(ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True))
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        dataset_item_id=item.id,
        display_id=f"T-IT-{suffix}",
        file_name="img.png",
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    tracked = db.info.get("cleanup")
    if tracked is not None:
        tracked["projects"].append(proj.id)
        tracked["datasets"].append(dataset.id)
        tracked["items"].append(item.id)
        tracked["backends"].append(backend.id)
        tracked["pools"].append(pool.id)
        tracked["tasks"].append(task.id)
    return proj, backend, task, pool


@pytest.fixture
def patched_client(monkeypatch):
    """Capture immutable route operations and emulate an interactive model reply."""
    captured: dict = {}
    setup_payload = {
        "models": [
            {
                "id": "interactive",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": ["point", "interactive_box", "text"],
                "supported_inputs": ["full_image", "point_prompt", "bbox_prompt"],
                "supported_geometric_outputs": ["polygon"],
            }
        ]
    }

    async def fake_setup(registry_id):
        return BackendControlSetupResult(registry_id=registry_id, setup=setup_payload)

    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: SimpleNamespace(setup=fake_setup),
    )

    class FakeExecutionPort:
        async def execute(self, operation, *, before_dispatch=None):
            if before_dispatch is not None:
                await before_dispatch()
            task_data = thaw_json(operation.task_inputs[0])
            captured["operation"] = operation
            captured["task_data"] = task_data
            captured["context"] = thaw_json(operation.context)
            registry_id = operation.requested_registry_id
            return ModelOperationResult(
                request_id=operation.request_id,
                operation=operation.operation,
                results=(
                    ModelTaskResult(
                        task_id=task_data["id"],
                        result=(),
                        score=None,
                        model_version="mock",
                        inference_time_ms=1,
                        cache_hit=False,
                        model_load_ms=123,
                        meta={"source": "interactive-test"},
                    ),
                ),
                provenance=ExecutionProvenance(
                    requested_registry_id=operation.requested_registry_id,
                    requested_pool_id=operation.requested_pool_id,
                    registry_id=registry_id,
                    pool_id=captured["pool_id"],
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
    return captured


async def test_text_prompt_does_not_inject_project_thresholds(
    httpx_client, super_admin, db_session, patched_client
):
    """自 59dfffa 起平台不再注入项目级阈值; 客户端未给阈值时 context 里就没有。"""
    user, token = super_admin
    proj, backend, task, pool = await _seed(db_session, user.id, box=0.42, text=0.18)
    patched_client["pool_id"] = pool.id
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/interactive-annotating",
        json={"task_id": str(task.id), "context": {"type": "text", "text": "person"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    ctx = patched_client["context"]
    assert ctx["type"] == "text"
    assert "box_threshold" not in ctx
    assert "text_threshold" not in ctx


async def test_text_prompt_respects_explicit_client_thresholds(
    httpx_client, super_admin, db_session, patched_client
):
    """客户端显式传值则尊重客户端，不被 project 默认覆盖。"""
    user, token = super_admin
    proj, backend, task, pool = await _seed(db_session, user.id, box=0.5, text=0.5)
    patched_client["pool_id"] = pool.id
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/interactive-annotating",
        json={
            "task_id": str(task.id),
            "context": {
                "type": "text",
                "text": "car",
                "box_threshold": 0.7,
                "text_threshold": 0.6,
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    ctx = patched_client["context"]
    assert ctx["box_threshold"] == pytest.approx(0.7)
    assert ctx["text_threshold"] == pytest.approx(0.6)


async def test_point_prompt_does_not_inject_thresholds(
    httpx_client, super_admin, db_session, patched_client
):
    """point/bbox 不走 DINO，不应注入阈值（避免污染缓存键 / 协议噪声）。"""
    user, token = super_admin
    proj, backend, task, pool = await _seed(db_session, user.id)
    patched_client["pool_id"] = pool.id
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}/interactive-annotating",
        json={
            "task_id": str(task.id),
            "context": {"type": "point", "points": [[0.5, 0.5]], "labels": [1]},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    ctx = patched_client["context"]
    assert "box_threshold" not in ctx
    assert "text_threshold" not in ctx
    data = resp.json()
    assert data["cache_hit"] is False
    assert data["score"] is None
    assert data["model_version"] == "mock"
    assert data["inference_time_ms"] == 1
    assert data["model_load_ms"] == 123

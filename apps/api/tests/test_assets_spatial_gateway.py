from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_assets_catalog.spatial_contract import (
    ApplyPreparationRequest,
    PreparePointcloudRequest,
    PreparationMutationResult,
    SPATIAL_SCHEMA,
    GetSceneRequest,
    ListScenesRequest,
    MutateSceneRequest,
    SceneCursor,
    SceneMutationResult,
    ScenePage,
    SceneResult,
    ScenePatch,
    PosePairsResult,
    SpatialLayoutPlan,
    SpatialScene,
)
from app.api.v1 import scenes as scene_routes
from app.schemas.scene import SceneUpdate
from app.services import assets_catalog
from app.services import assets_spatial
from app.services.assets_catalog import CatalogGatewayError
from app.services.assets_spatial import (
    AssetsSpatialGateway,
    SameSessionAssetsSpatialGateway,
)


def _scene(scene_id: uuid.UUID, dataset_id: uuid.UUID, name: str, created_at):
    return SpatialScene(
        id=scene_id,
        display_id=f"SCN-{scene_id.hex[:8]}",
        dataset_id=dataset_id,
        name=name,
        source_format=None,
        source_metadata={},
        created_by=None,
        created_at=created_at,
        updated_at=created_at,
    )


def _wire(model):
    return {
        "schema": SPATIAL_SCHEMA,
        "ok": True,
        "data": model.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_local_spatial_read_uses_exact_owner_function_on_caller_session():
    actor_id, scene_id, dataset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    created_at = datetime.now(UTC)
    expected = SceneResult(
        actor_id=actor_id,
        scene_id=scene_id,
        scene=_scene(scene_id, dataset_id, "scene-a", created_at),
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))

    result = await AssetsSpatialGateway(db).get_scene(
        GetSceneRequest(actor_id=actor_id, scene_id=scene_id)
    )

    assert result.scene == expected.scene
    statement, parameters = db.execute.await_args.args
    assert "yoao_assets_v1.get_spatial_scene" in str(statement)
    assert parameters["actor_id"] == actor_id
    assert parameters["scene_id"] == scene_id
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_spatial_list_collects_pages_in_caller_session():
    actor_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    base_time = datetime.now(UTC)
    first_scene = _scene(first_id, dataset_id, "one", base_time)
    second_scene = _scene(
        second_id, dataset_id, "two", base_time + timedelta(seconds=1)
    )
    cursor = SceneCursor(created_at=first_scene.created_at, scene_id=first_scene.id)
    pages = (
        ScenePage(
            actor_id=actor_id,
            dataset_id=dataset_id,
            after=None,
            next_cursor=cursor,
            snapshot_digest="a" * 64,
            total=2,
            complete=False,
            scenes=[first_scene],
        ),
        ScenePage(
            actor_id=actor_id,
            dataset_id=dataset_id,
            after=cursor,
            next_cursor=None,
            snapshot_digest="a" * 64,
            total=2,
            complete=True,
            scenes=[second_scene],
        ),
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [
        SimpleNamespace(scalar_one=lambda page=page: _wire(page)) for page in pages
    ]
    result = await AssetsSpatialGateway(db).list_scenes(
        ListScenesRequest(actor_id=actor_id, dataset_id=dataset_id, page_size=1)
    )

    assert [scene.id for scene in result] == [first_id, second_id]
    first, second = [call.args for call in db.execute.await_args_list]
    assert all(
        "yoao_assets_v1.list_spatial_scenes" in str(statement)
        and parameters["actor_id"] == actor_id
        and parameters["dataset_id"] == dataset_id
        for statement, parameters in (first, second)
    )
    assert first[1]["after_scene_id"] is None
    assert second[1]["after_scene_id"] == first_id
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_spatial_reply_scope_mismatch_fails_closed():
    actor_id, scene_id, dataset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    wrong_scene_id = uuid.uuid4()
    now = datetime.now(UTC)
    wrong = {
        "schema": SPATIAL_SCHEMA,
        "ok": True,
        "data": {
            "actor_id": str(actor_id),
            "scene_id": str(scene_id),
            "scene": _scene(wrong_scene_id, dataset_id, "escaped", now).model_dump(
                mode="json"
            ),
        },
    }
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: wrong)

    with pytest.raises(CatalogGatewayError) as failure:
        await AssetsSpatialGateway(db).get_scene(
            GetSceneRequest(actor_id=actor_id, scene_id=scene_id)
        )

    assert failure.value.code == "invalid_response"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_session_native_spatial_read_does_not_commit():
    actor_id, scene_id, dataset_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    expected = SceneResult(
        actor_id=actor_id,
        scene_id=scene_id,
        scene=_scene(scene_id, dataset_id, "local", now),
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))
    result = await SameSessionAssetsSpatialGateway(db).get_scene(
        GetSceneRequest(actor_id=actor_id, scene_id=scene_id)
    )

    assert result.scene.name == "local"
    assert "yoao_assets_v1.get_spatial_scene" in str(db.execute.await_args.args[0])
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_atomic_preparation_uses_native_binding_without_commit(
    monkeypatch,
):
    creation_operation_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    expected = PreparationMutationResult(
        creation_operation_id=creation_operation_id,
        dataset_id=dataset_id,
        calibrated_items=1,
        created_scenes=0,
        assigned_items=0,
    )
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr(assets_catalog, "_MAX_REQUEST_BYTES", 32)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))
    apply_request = ApplyPreparationRequest(
        creation_operation_id=creation_operation_id,
        snapshot={},
        calibration_updates=[],
        frame_plan=SpatialLayoutPlan(
            scenes=[],
            assignments=[],
            created_scenes=0,
            assigned_items=0,
            skipped_items=0,
            dry_run=False,
            notes=[],
        ),
    )

    class Workflow:
        async def prepare_pointcloud(self, request, *, native=None):
            assert request.creation_operation_id == creation_operation_id
            assert isinstance(native, SameSessionAssetsSpatialGateway)
            return await native.apply_pointcloud_preparation(apply_request)

    gateway = AssetsSpatialGateway(db)
    gateway._spatial_workflow = lambda: Workflow()
    result = await gateway.prepare_pointcloud(
        PreparePointcloudRequest(creation_operation_id=creation_operation_id),
        same_session_native=True,
        commit=False,
    )

    assert result == expected
    assert "yoao_assets_v1.apply_pointcloud_preparation" in str(
        db.execute.await_args.args[0]
    )
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_spatial_reply_budget_rejects_before_commit(monkeypatch):
    operation_id, actor_id, scene_id, dataset_id = (uuid.uuid4() for _ in range(4))
    scene = _scene(scene_id, dataset_id, "large", datetime.now(UTC)).model_copy(
        update={"source_metadata": {"large": "x" * 1024}}
    )
    result = SceneMutationResult(
        operation_id=operation_id, actor_id=actor_id, scene_id=scene_id, scene=scene
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(result))
    monkeypatch.setattr(assets_spatial, "_MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(CatalogGatewayError, match="response budget"):
        await AssetsSpatialGateway(db).mutate_scene(
            MutateSceneRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                scene_id=scene_id,
                patch=ScenePatch(name="large"),
            )
        )
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pose_pair_reads_bound_each_request_to_its_exact_chunk():
    scene_id = uuid.uuid4()
    captured = []

    def handler(statement, parameters):
        assert "yoao_assets_v1.read_spatial_pose_pairs" in str(statement)
        pairs = json.loads(parameters["pairs"])
        captured.append(
            [(uuid.UUID(pair["scene_id"]), pair["frame_index"]) for pair in pairs]
        )
        generation = len(captured)
        return SimpleNamespace(
            scalar_one=lambda: _wire(
                PosePairsResult(
                    pairs=pairs,
                    poses=[
                        {
                            **pair,
                            "timestamp_us": pair["frame_index"],
                            "ego_translation": [float(generation), 0.0, 0.0],
                            "ego_rotation": [1.0, 0.0, 0.0, 0.0],
                            "source_metadata": {},
                        }
                        for pair in pairs
                        if pair["frame_index"] % 17 != 0
                    ],
                    snapshot_digest=str(generation) * 64,
                )
            ),
        )

    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = handler
    gateway = AssetsSpatialGateway(db)
    assert await gateway.read_pose_pairs(set()) == {}
    assert captured == []
    wanted = {(scene_id, index) for index in range(1000)}
    poses = await gateway.read_pose_pairs(wanted)
    assert set(poses) == {pair for pair in wanted if pair[1] % 17 != 0}
    assert {tuple(pose.ego_translation) for pose in poses.values()} == {(1.0, 0.0, 0.0)}
    assert all(pose.timestamp_us == pair[1] for pair, pose in poses.items())
    assert {pair for chunk in captured for pair in chunk} == wanted


@pytest.mark.asyncio
async def test_scene_patch_preserves_public_response_and_reuses_idempotency_key(
    monkeypatch,
):
    actor_id, scene_id, dataset_id, operation_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    now = datetime.now(UTC)
    scene = _scene(scene_id, dataset_id, "renamed", now)
    captured = {}

    class Gateway:
        async def mutate_scene(self, request: MutateSceneRequest):
            captured["request"] = request
            return SceneMutationResult(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                scene_id=request.scene_id,
                scene=scene,
            )

    monkeypatch.setattr(scene_routes, "AssetsSpatialGateway", lambda _db: Gateway())
    response = Response()
    result = await scene_routes.update_scene(
        scene_id=scene_id,
        payload=SceneUpdate(name="renamed"),
        response=response,
        db=object(),
        current_user=SimpleNamespace(id=actor_id),
        idempotency_key=operation_id,
    )

    assert result.id == scene_id
    assert result.name == "renamed"
    assert captured["request"].operation_id == operation_id
    assert captured["request"].patch.name == "renamed"
    assert response.headers["Idempotency-Key"] == str(operation_id)


@pytest.mark.asyncio
async def test_read_spatial_item_facts_chunks_at_single_page_limit(monkeypatch):
    from app.services import labeling_scene
    from yoao_assets_catalog.spatial_contract import ItemFactsPage

    seen_requests = []

    class Gateway:
        async def read_items(self, request, *, same_session_native=False):
            seen_requests.append((request, same_session_native))
            return ItemFactsPage(
                dataset_id=None,
                item_ids=request.item_ids,
                after_item_id=None,
                next_item_id=None,
                snapshot_digest="0" * 64,
                total=len(request.item_ids),
                complete=True,
                items=[],
                scenes=[],
            )

    monkeypatch.setattr(labeling_scene, "AssetsSpatialGateway", lambda _db: Gateway())
    ids = [uuid.uuid4() for _ in range(501)]
    await labeling_scene.read_spatial_item_facts(object(), [*ids, ids[0]])
    assert [len(req.item_ids) for req, _ in seen_requests] == [500, 1]
    assert all(req.page_size == 500 for req, _ in seen_requests)
    assert all(len(req.item_ids) <= req.page_size for req, _ in seen_requests)

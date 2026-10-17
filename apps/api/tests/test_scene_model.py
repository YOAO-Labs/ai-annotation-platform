"""v0.14.0 · Scene model + service CRUD 烟雾测试。"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.dataset import Dataset, DatasetItem
from app.services import scene as scene_svc


async def _make_dataset(db, owner_id, name="ds-scene-test"):
    ds = Dataset(
        display_id=f"DS-{uuid.uuid4().hex[:6]}",
        name=name,
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    return ds


async def test_create_scene_assigns_display_id(db_session, super_admin):
    ds = await _make_dataset(db_session, super_admin[0].id)
    scene = await scene_svc.create_scene(
        db_session,
        dataset_id=ds.id,
        name="alpha",
        source_format="manual",
        created_by=super_admin[0].id,
    )
    assert scene.display_id.startswith("SCN-")
    assert scene.dataset_id == ds.id
    assert scene.name == "alpha"
    assert scene.source_metadata == {}


async def test_create_scene_name_conflict(db_session, super_admin):
    ds = await _make_dataset(db_session, super_admin[0].id)
    await scene_svc.create_scene(db_session, dataset_id=ds.id, name="dup")

    with pytest.raises(scene_svc.SceneNameConflict):
        await scene_svc.create_scene(db_session, dataset_id=ds.id, name="dup")


async def test_assign_items_to_scene_writes_frame_index(db_session, super_admin):
    ds = await _make_dataset(db_session, super_admin[0].id)
    scene = await scene_svc.create_scene(db_session, dataset_id=ds.id, name="s")

    items = []
    for i in range(3):
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=f"{i}.pcd",
            file_path=f"{ds.name}/lidar/{i}.pcd",
            file_type="point_cloud",
        )
        db_session.add(item)
        await db_session.flush()
        items.append(item)

    updated = await scene_svc.assign_items_to_scene(
        db_session, scene_id=scene.id, items_in_order=items
    )
    assert updated == 3
    assert [it.frame_index for it in items] == [0, 1, 2]
    assert all(it.scene_id == scene.id for it in items)


async def test_list_for_dataset_returns_ordered(db_session, super_admin):
    ds = await _make_dataset(db_session, super_admin[0].id)
    s1 = await scene_svc.create_scene(db_session, dataset_id=ds.id, name="s1")
    s2 = await scene_svc.create_scene(db_session, dataset_id=ds.id, name="s2")
    scenes = await scene_svc.list_for_dataset(db_session, ds.id)
    assert [s.id for s in scenes] == [s1.id, s2.id]


async def test_scenes_api_get_and_patch(app_module, test_engine):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.security import create_access_token
    from app.db.models.dataset import Scene
    from app.db.models.user import User
    from app.deps import get_db
    from tests.factory import make_user_dict

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        owner = User(
            **make_user_dict(
                "super_admin",
                f"scene-owner-{uuid.uuid4().hex}@test.local",
                "Scene owner",
            )
        )
        actor = User(
            **make_user_dict(
                "employee",
                f"scene-editor-{uuid.uuid4().hex}@test.local",
                "Scene editor",
            )
        )
        db.add_all([owner, actor])
        await db.flush()
        dataset = await _make_dataset(db, owner.id)
        scene = await scene_svc.create_scene(
            db,
            dataset_id=dataset.id,
            name="orig",
            source_format="manual",
            source_metadata={"before": True},
            created_by=owner.id,
        )
        duplicate = await scene_svc.create_scene(
            db,
            dataset_id=dataset.id,
            name="taken",
            source_format="manual",
            source_metadata={"untouched": True},
            created_by=owner.id,
        )
        dataset_id, scene_id, duplicate_id = dataset.id, scene.id, duplicate.id
        token = create_access_token(subject=str(actor.id), role="employee")
        await db.commit()

    async def session_per_request():
        async with sessions() as db:
            yield db

    previous_override = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = session_per_request
    headers = {"Authorization": f"Bearer {token}"}

    async def persisted_scene():
        async with sessions() as observer:
            return await observer.get(Scene, scene_id)

    try:
        transport = ASGITransport(app=app_module)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/scenes?dataset_id={dataset_id}", headers=headers
            )
            assert response.status_code == 200
            assert {row["id"] for row in response.json()} == {
                str(scene_id),
                str(duplicate_id),
            }

            response = await client.get(f"/api/v1/scenes/{scene_id}", headers=headers)
            assert response.status_code == 200
            assert response.json()["name"] == "orig"

            response = await client.patch(
                f"/api/v1/scenes/{scene_id}",
                json={
                    "name": "renamed",
                    "source_format": "nuscenes",
                    "source_metadata": {"patch": "accepted"},
                },
                headers=headers,
            )
            assert response.status_code == 200
            assert response.json()["name"] == "renamed"
            assert response.json()["source_format"] == "nuscenes"
            assert response.json()["source_metadata"] == {"patch": "accepted"}

            response = await client.get(f"/api/v1/scenes/{scene_id}", headers=headers)
            assert response.status_code == 200
            assert response.json()["name"] == "renamed"
            assert response.json()["source_format"] == "nuscenes"
            assert response.json()["source_metadata"] == {"patch": "accepted"}
            stored = await persisted_scene()
            assert stored.name == "renamed"
            assert stored.source_format == "nuscenes"
            assert stored.source_metadata == {"patch": "accepted"}

            response = await client.patch(
                f"/api/v1/scenes/{scene_id}",
                json={"source_metadata": {}},
                headers=headers,
            )
            assert response.status_code == 200
            assert response.json()["source_metadata"] == {}
            response = await client.get(f"/api/v1/scenes/{scene_id}", headers=headers)
            assert response.status_code == 200
            assert response.json()["source_metadata"] == {}
            stored = await persisted_scene()
            assert stored.source_metadata == {}

            for payload in (
                {},
                {"name": None, "source_format": None, "source_metadata": None},
            ):
                response = await client.patch(
                    f"/api/v1/scenes/{scene_id}",
                    json=payload,
                    headers=headers,
                )
                assert response.status_code == 200
                assert response.json()["name"] == "renamed"
                assert response.json()["source_format"] == "nuscenes"
                assert response.json()["source_metadata"] == {}

            response = await client.patch(
                f"/api/v1/scenes/{scene_id}",
                json={
                    "name": "taken",
                    "source_format": "must-not-persist",
                    "source_metadata": {"must_not": "persist"},
                },
                headers=headers,
            )
            assert response.status_code == 409
            response = await client.get(f"/api/v1/scenes/{scene_id}", headers=headers)
            assert response.status_code == 200
            assert response.json()["name"] == "renamed"
            assert response.json()["source_format"] == "nuscenes"
            assert response.json()["source_metadata"] == {}
            stored = await persisted_scene()
            assert stored.name == "renamed"
            assert stored.source_format == "nuscenes"
            assert stored.source_metadata == {}

            response = await client.patch(
                f"/api/v1/scenes/{uuid.uuid4()}",
                json={"name": "missing"},
                headers=headers,
            )
            assert response.status_code == 404
    finally:
        if previous_override is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_override
        async with sessions() as db:
            await db.execute(
                delete(Scene).where(Scene.id.in_([scene_id, duplicate_id]))
            )
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(delete(User).where(User.id.in_([owner.id, actor.id])))
            await db.commit()


async def test_scenes_api_patch_rolls_back_failed_commit(app_module, test_engine):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.security import create_access_token
    from app.db.models.dataset import Scene
    from app.db.models.user import User
    from app.deps import get_db
    from tests.factory import make_user_dict

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        user = User(
            **make_user_dict(
                "super_admin",
                f"scene-commit-{uuid.uuid4().hex}@test.local",
                "Scene commit failure",
            )
        )
        db.add(user)
        await db.flush()
        dataset = await _make_dataset(db, user.id)
        scene = await scene_svc.create_scene(
            db,
            dataset_id=dataset.id,
            name="before-commit-failure",
            source_format="manual",
            source_metadata={"before": True},
            created_by=user.id,
        )
        dataset_id, scene_id, user_id = dataset.id, scene.id, user.id
        token = create_access_token(subject=str(user.id), role="super_admin")
        await db.commit()

    suffix = uuid.uuid4().hex
    function_name = f"reject_scene_patch_{suffix}"
    trigger_name = f"reject_scene_patch_{suffix}"
    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                f"""
                CREATE FUNCTION {function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $body$
                BEGIN
                    RAISE EXCEPTION 'scene PATCH commit failure';
                END;
                $body$
                """
            )
        )
        await connection.execute(
            text(
                f"""
                CREATE CONSTRAINT TRIGGER {trigger_name}
                AFTER UPDATE ON scenes
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION {function_name}()
                """
            )
        )

    async def session_per_request():
        async with sessions() as db:
            yield db

    previous_override = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = session_per_request
    headers = {"Authorization": f"Bearer {token}"}
    try:
        transport = ASGITransport(app=app_module, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.patch(
                f"/api/v1/scenes/{scene_id}",
                json={
                    "name": "commit-must-fail",
                    "source_format": "nuscenes",
                    "source_metadata": {"must_not": "persist"},
                },
                headers=headers,
            )
        assert response.status_code == 500

        async with sessions() as observer:
            stored = await observer.get(Scene, scene_id)
            assert stored.name == "before-commit-failure"
            assert stored.source_format == "manual"
            assert stored.source_metadata == {"before": True}
    finally:
        if previous_override is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_override
        async with test_engine.begin() as connection:
            await connection.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger_name} ON scenes")
            )
            await connection.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))
        async with sessions() as db:
            await db.execute(delete(Scene).where(Scene.id == scene_id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()

"""Dataset Scene filtering stays bounded, identity-based and fail-closed."""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset import DatasetItem, Scene
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files
from tests.test_dataset_views import dataset


@pytest.fixture
async def scenes_world(db_session, super_admin):
    db = db_session
    ds = await dataset(db, super_admin[0])
    other = await dataset(db, super_admin[0])
    scenes = []
    for index in range(23):
        row = Scene(
            dataset_id=ds.id,
            display_id=f"S-{uuid.uuid4().hex[:12]}",
            name=f"Scene {index:02d}",
            source_metadata={"private_root": "/private/original"},
        )
        db.add(row)
        scenes.append(row)
    hidden = Scene(
        dataset_id=other.id,
        display_id=f"S-{uuid.uuid4().hex[:12]}",
        name="Private other Scene",
        source_metadata={"token": "private-token"},
    )
    db.add(hidden)
    await db.flush()
    specs = [
        (scenes[0], "lidar.pcd", "point_cloud", 0, None, None),
        (scenes[0], "camera.PNG", "image", 0, 1280, 720),
        (scenes[0], "calibration.json", "other", None, None, None),
        (scenes[1], "camera.PNG", "image", 0, 1280, 720),
        (None, "loose.PNG", "image", None, 1280, 720),
    ]
    for scene, name, kind, frame, width, height in specs:
        db.add(
            DatasetItem(
                dataset_id=ds.id,
                scene_id=scene.id if scene else None,
                file_name=name,
                file_path=f"scene-test/{uuid.uuid4()}",
                file_type=kind,
                frame_index=frame,
                width=width,
                height=height,
            )
        )
    await db.commit()
    return ds, scenes, hidden


async def test_scene_filters_all_business_members_and_combines_before_paging(
    db_session, scenes_world, super_admin, httpx_client
):
    ds, scenes, _ = scenes_world
    base = f"/api/v1/datasets/{ds.id}/items"
    auth = headers(super_admin)
    query = {"scene_id": str(scenes[0].id), "limit": 1}
    first = await httpx_client.get(base, params=query, headers=auth)
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 3 and len(first.json()["items"]) == 1
    seen = set()
    for offset in range(3):
        row = (
            await httpx_client.get(
                base, params={**query, "offset": offset}, headers=auth
            )
        ).json()
        seen.add(row["items"][0]["file_name"])
    assert seen == {"lidar.pcd", "camera.PNG", "calibration.json"}
    combined = await httpx_client.get(
        base,
        params={
            **query,
            "media_type": "image",
            "extension": "png",
            "min_width": 1000,
            "max_height": 800,
            "search": "camera",
            "availability": "unverified",
        },
        headers=auth,
    )
    assert combined.status_code == 200 and combined.json()["total"] == 1
    assert combined.json()["items"][0]["file_name"] == "camera.PNG"
    assert (
        await httpx_client.get(
            base, params={"scene_id": str(scenes[2].id)}, headers=auth
        )
    ).json()["total"] == 0


async def test_scene_options_are_paged_safe_and_keep_selected_outside_page(
    scenes_world, super_admin, httpx_client
):
    ds, scenes, _ = scenes_world
    url = f"/api/v1/datasets/{ds.id}/scene-options"
    auth = headers(super_admin)
    result = await httpx_client.get(
        url,
        params={"limit": 2, "offset": 2, "selected_id": str(scenes[0].id)},
        headers=auth,
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["total"] == 23 and len(body["items"]) == 2
    assert body["selected"]["id"] == str(scenes[0].id)
    assert {row["name"] for row in body["items"]} == {"Scene 02", "Scene 03"}
    assert set(body["items"][0]) == {"id", "display_id", "name"}
    assert "private" not in str(body) and "source_metadata" not in str(body)
    search = (
        await httpx_client.get(
            url,
            params={"search": "Scene 22", "selected_id": str(scenes[0].id)},
            headers=auth,
        )
    ).json()
    assert search["total"] == 1 and search["items"][0]["id"] == str(scenes[22].id)
    assert search["selected"]["id"] == str(scenes[0].id)
    empty = (await httpx_client.get(url, params={"offset": 1000}, headers=auth)).json()
    assert not empty["items"] and empty["selected"] is None and empty["total"] == 23
    for bad in ({"limit": 101}, {"limit": 0}, {"offset": -1}, {"search": "x" * 201}):
        assert (
            await httpx_client.get(url, params=bad, headers=auth)
        ).status_code == 422
    assert (await httpx_client.get(url)).status_code == 401


@pytest.mark.parametrize("kind", ["invalid", "missing", "other_dataset"])
async def test_invalid_scene_never_broadens_query_or_saves_invalid_view(
    scenes_world, super_admin, httpx_client, kind
):
    ds, _, hidden = scenes_world
    value = (
        "invalid"
        if kind == "invalid"
        else str(hidden.id)
        if kind == "other_dataset"
        else str(uuid.uuid4())
    )
    status = 422 if kind == "invalid" else 404
    base = f"/api/v1/datasets/{ds.id}"
    auth = headers(super_admin)
    for path, params in [
        ("/items", {"scene_id": value}),
        ("/scene-options", {"selected_id": value}),
    ]:
        result = await httpx_client.get(base + path, params=params, headers=auth)
        assert result.status_code == status, result.text
        assert (
            "Private other Scene" not in result.text
            and "private-token" not in result.text
        )
    saved = await httpx_client.post(
        base + "/saved-views",
        json={"name": "Invalid", "query": {"scene_id": value}},
        headers=auth,
    )
    assert saved.status_code == status


async def test_saved_scene_view_is_dynamic_and_deletion_stays_fail_closed(
    db_session, scenes_world, super_admin, httpx_client
):
    db = db_session
    ds, scenes, _ = scenes_world
    auth = headers(super_admin)
    base = f"/api/v1/datasets/{ds.id}"
    response = await httpx_client.post(
        base + "/saved-views",
        json={
            "name": "Scene camera",
            "query": {"scene_id": str(scenes[0].id), "media_type": "image"},
        },
        headers=auth,
    )
    assert response.status_code == 201, response.text
    view_id = response.json()["id"]
    query = {"scene_id": str(scenes[0].id), "media_type": "image"}
    assert (await httpx_client.get(base + "/items", params=query, headers=auth)).json()[
        "total"
    ] == 1
    db.add(
        DatasetItem(
            dataset_id=ds.id,
            scene_id=scenes[0].id,
            file_name="new.jpg",
            file_path="new",
            file_type="image",
        )
    )
    await db.commit()
    assert (await httpx_client.get(base + "/items", params=query, headers=auth)).json()[
        "total"
    ] == 2
    saved = (await httpx_client.get(base + "/saved-views", headers=auth)).json()[
        "items"
    ][0]
    assert saved["query"]["scene_id"] == str(scenes[0].id)
    await db.delete(scenes[0])
    await db.commit()
    assert (
        await httpx_client.get(base + "/items", params=query, headers=auth)
    ).status_code == 404
    # Restore retains the dead ID for the item query to reject, never drops it.
    assert (await httpx_client.get(base + "/saved-views", headers=auth)).json()[
        "items"
    ][0]["query"]["scene_id"] == query["scene_id"]
    assert (
        await httpx_client.put(
            base + f"/saved-views/{view_id}",
            json={"name": "Still invalid", "query": query},
            headers=auth,
        )
    ).status_code == 404
    assert (await httpx_client.get(base + "/items", headers=auth)).json()["total"] == 6


async def test_scene_combines_with_authorized_reference_source_and_stored_state(
    db_session, content_world, super_admin, httpx_client
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    scene = Scene(
        dataset_id=world[3].id,
        display_id=f"S-{uuid.uuid4().hex[:12]}",
        name="Reference scene",
    )
    db.add(scene)
    await db.flush()
    item = await db.scalar(
        select(DatasetItem).where(DatasetItem.dataset_id == world[3].id)
    )
    item.scene_id = scene.id
    await db.commit()
    response = await httpx_client.get(
        f"/api/v1/datasets/{world[3].id}/items",
        params={
            "scene_id": str(scene.id),
            "source_id": str(world[5].id),
            "availability": "verified",
        },
        headers=headers(super_admin),
    )
    assert response.status_code == 200 and response.json()["total"] == 1

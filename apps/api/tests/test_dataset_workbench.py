"""Workbench queries use stored evidence and current resource authorization."""

import uuid

import pytest

from sqlalchemy import select

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_source import DatasetSource, DatasetSourcePreview
from app.services.storage import storage_service
from tests.factory import create_project, create_task
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import (
    accept_all_files,
    picture,
    preflight,
    rescan,
)
from tests.test_training_datasets import (
    reviewed_project,
    source_headers as source_headers,
)
from tests.training_original_fixtures import register_original


async def test_item_pagination_single_item_parent_and_unknown_status(
    db_session, super_admin, httpx_client
):
    db = db_session
    ds = Dataset(
        display_id=f"WB-{uuid.uuid4().hex[:10]}",
        name="Workbench",
        created_by=super_admin[0].id,
    )
    other = Dataset(
        display_id=f"WB-{uuid.uuid4().hex[:10]}",
        name="Other",
        created_by=super_admin[0].id,
    )
    db.add_all([ds, other])
    await db.flush()
    for index in range(3):
        db.add(
            DatasetItem(
                dataset_id=ds.id,
                file_name=f"legacy-{index}.png",
                file_path=f"unverified/{index}.png",
            )
        )
    await db.commit()
    url = f"/api/v1/datasets/{ds.id}"
    auth = headers(super_admin)
    first = await httpx_client.get(url + "/items?limit=1", headers=auth)
    assert first.status_code == 200, first.text
    first = first.json()
    assert len(first["items"]) == 1 and first["total"] == 3
    assert first["items"][0]["availability"]["state"] == "unverified"
    second = (
        await httpx_client.get(url + "/items?limit=1&offset=1", headers=auth)
    ).json()
    assert first["items"][0]["id"] != second["items"][0]["id"]
    target = second["items"][0]["id"]
    item = await httpx_client.get(url + f"/items/{target}", headers=auth)
    assert item.status_code == 200 and item.json()["id"] == target
    assert (
        await httpx_client.get(
            f"/api/v1/datasets/{other.id}/items/{target}", headers=auth
        )
    ).status_code == 404
    assert (
        await httpx_client.get(url + "/items?availability=healthy", headers=auth)
    ).status_code == 422
    assert (
        await httpx_client.get(
            url + "/items?search=legacy-1&availability=unverified", headers=auth
        )
    ).json()["total"] == 1
    assert (
        await httpx_client.get(url + "/items?availability=verified", headers=auth)
    ).json()["total"] == 0


async def test_reference_states_follow_complete_preflight_and_filter_on_server(
    db_session, content_world, super_admin, httpx_client
):
    db, world = db_session, content_world
    for name in ["gone.png", "same.png"]:
        (world[0] / name).write_bytes(picture("blue"))
    world = await rescan(db, world)
    await accept_all_files(db, world)
    url = f"/api/v1/datasets/{world[3].id}"
    auth = headers(super_admin)
    original = (await httpx_client.get(url + "/items", headers=auth)).json()
    assert original["total"] == 3
    assert all(
        item["availability"]["state"] == "verified" for item in original["items"]
    )
    # A source mutation alone is not a recorded observation. Actual content
    # reads remain strict; this query must not silently read all source files.
    (world[0] / "图片 空格.png").write_bytes(picture("green"))
    (world[0] / "gone.png").unlink()
    assert (
        await httpx_client.get(url + "/items?availability=verified", headers=auth)
    ).json()["total"] == 3
    world = await rescan(db, world)
    await preflight(db, world)
    page = await httpx_client.get(
        url + "/items?limit=1&availability=changed", headers=auth
    )
    assert page.status_code == 200, page.text
    page = page.json()
    assert page["total"] == 1 and page["items"][0]["file_name"] == "图片 空格.png"
    assert "read_original" not in page["items"][0]["availability"]["allowed_actions"]
    assert (
        await httpx_client.get(url + "/items?availability=missing", headers=auth)
    ).json()["total"] == 1
    wb = (await httpx_client.get(url + "/workbench", headers=auth)).json()
    assert wb["availability_counts"] == dict(
        verified=1, missing=1, changed=1, preview_only=0, unavailable=0, unverified=0
    )
    assert wb["sources"][0]["location_id"] == str(world[4].id)
    assert str(world[0]) not in str(wb)
    world[5].enabled = False
    await db.commit()
    assert (
        await httpx_client.get(url + "/items?availability=unavailable", headers=auth)
    ).json()["total"] == 3


async def test_preview_is_independent_read_only_and_rechecks_source_authority(
    db_session, content_world, super_admin, project_admin, httpx_client
):
    db, world = db_session, content_world
    world[5].cache_policy = "preview"
    await db.commit()
    original = await accept_all_files(db, world)
    item_id = original["图片 空格.png"]["item_id"]
    preview = await db.scalar(
        select(DatasetSourcePreview).where(
            DatasetSourcePreview.source_id == world[5].id
        )
    )
    assert preview.state == "ready"
    bucket, key = preview.bucket, preview.object_key
    try:
        (world[0] / "图片 空格.png").write_bytes(b"not an image")
        world = await rescan(db, world)
        await preflight(db, world)
        base = f"/api/v1/datasets/{world[3].id}/items/{item_id}"
        item = (await httpx_client.get(base, headers=headers(super_admin))).json()
        assert item["availability"]["state"] == "preview_only"
        assert item["availability"]["allowed_actions"] == [
            "manage_source",
            "view_location",
            "read_preview",
        ]
        image = await httpx_client.get(base + "/preview", headers=headers(super_admin))
        assert image.status_code == 200 and image.headers["content-type"].startswith(
            "image/"
        )
        assert image.headers["cache-control"] == "no-store"
        assert (
            await httpx_client.get(base + "/preview", headers=headers(project_admin))
        ).status_code == 404
        assert (
            await httpx_client.get(
                base + "/preview",
                headers={**headers(super_admin), "range": "bytes=0-1"},
            )
        ).status_code == 416
        other = Dataset(
            display_id=f"WB-{uuid.uuid4().hex[:10]}",
            name="Other",
            created_by=world[2].id,
        )
        db.add(other)
        await db.commit()
        assert (
            await httpx_client.get(
                f"/api/v1/datasets/{other.id}/items/{item_id}/preview",
                headers=headers(super_admin),
            )
        ).status_code == 404
        # Sanitized observations do not provide source identity or management
        # actions to an unrelated manager, even with the exact item URL.
        hidden = (await httpx_client.get(base, headers=headers(project_admin))).json()[
            "availability"
        ]
        assert hidden["source_id"] is None and hidden["allowed_actions"] == []
        world[5].enabled = False
        await db.commit()
        assert (
            await httpx_client.get(base + "/preview", headers=headers(super_admin))
        ).status_code == 409
        disabled = (await httpx_client.get(base, headers=headers(super_admin))).json()[
            "availability"
        ]
        assert disabled["state"] == "unavailable" and not disabled["preview_available"]
    finally:
        storage_service.delete_object(key, bucket=bucket)


async def test_location_filter_counts_unique_visible_datasets_and_revokes(
    db_session, content_world, super_admin, project_admin, httpx_client
):
    db, world = db_session, content_world
    ds, loc = world[3:5]
    ds.created_by = project_admin[0].id
    loc.config = {**loc.config, "scope": "global"}
    db.add(
        DatasetSource(
            dataset_id=ds.id, location_id=loc.id, name="second", config=world[5].config
        )
    )
    hidden = Dataset(
        display_id=f"WB-{uuid.uuid4().hex[:10]}",
        name="Hidden",
        created_by=super_admin[0].id,
    )
    db.add(hidden)
    await db.flush()
    db.add(
        DatasetSource(
            dataset_id=hidden.id,
            location_id=loc.id,
            name="private",
            config=world[5].config,
        )
    )
    await db.commit()
    url = f"/api/v1/datasets?location_id={loc.id}&limit=1"
    result = (await httpx_client.get(url, headers=headers(project_admin))).json()
    assert result["total"] == 1 and result["items"][0]["id"] == str(ds.id)
    assert (await httpx_client.get(url, headers=headers(super_admin))).json()[
        "total"
    ] == 2
    summary = (
        await httpx_client.get(
            f"/api/v1/storage-locations/{loc.id}/source-summary?limit=1",
            headers=headers(project_admin),
        )
    ).json()
    assert summary["total"] == 2 and summary["dataset_count"] == 1
    loc.config = {**loc.config, "scope": "restricted", "use_user_ids": []}
    await db.commit()
    assert (
        await httpx_client.get(url, headers=headers(project_admin))
    ).status_code == 404


async def test_project_usage_counts_only_this_dataset_and_clips_project_visibility(
    db_session, super_admin, project_admin, httpx_client
):
    db = db_session
    dataset = Dataset(
        display_id=f"WB-{uuid.uuid4().hex[:10]}",
        name="Shared",
        created_by=super_admin[0].id,
    )
    db.add(dataset)
    await db.flush()
    items = [
        DatasetItem(
            dataset_id=dataset.id, file_name=f"{n}.png", file_path=f"source/{n}.png"
        )
        for n in range(2)
    ]
    db.add_all(items)
    await db.flush()
    own = await create_project(db, owner_id=project_admin[0].id, name="Owned")
    hidden = await create_project(db, owner_id=super_admin[0].id, name="Hidden")
    for project in [own, hidden]:
        db.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
        for index, item in enumerate(items):
            task = await create_task(db, project_id=project.id)
            task.dataset_item_id = item.id
            task.status = "completed" if project == own and index == 0 else "pending"
            task.is_labeled = project == own
        await create_task(db, project_id=project.id, status="completed")
    await db.commit()
    url = f"/api/v1/datasets/{dataset.id}"
    all_projects = (
        await httpx_client.get(url + "/workbench", headers=headers(super_admin))
    ).json()["projects"]
    assert len(all_projects) == 2 and all(p["total_tasks"] == 2 for p in all_projects)
    own_projects = (
        await httpx_client.get(url + "/workbench", headers=headers(project_admin))
    ).json()["projects"]
    assert (
        len(own_projects) == 1
        and own_projects[0]["labeled_tasks"] == 2
        and own_projects[0]["completed_tasks"] == 1
    )
    assert own_projects[0]["can_manage"]
    assert [
        p["id"]
        for p in (
            await httpx_client.get(url + "/projects", headers=headers(project_admin))
        ).json()
    ] == [str(own.id)]


async def test_training_dataset_scope_and_related_versions_are_real_queries(
    db_session, super_admin, project_admin, httpx_client, source_headers
):
    db = db_session
    project, tasks = await reviewed_project(db, super_admin[0])
    datasets = [
        Dataset(
            display_id=f"WB-{uuid.uuid4().hex[:10]}",
            name=f"Subset {n}",
            created_by=super_admin[0].id,
        )
        for n in range(2)
    ]
    db.add_all(datasets)
    await db.flush()
    for index, task in enumerate(tasks):
        ds = datasets[int(index == 2)]
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=task.file_name,
            file_path=task.file_path,
            width=64,
            height=48,
        )
        db.add(item)
        await db.flush()
        task.dataset_item_id = item.id
        await register_original(db, task, super_admin[0], provider=source_headers)
    for ds in datasets:
        db.add(ProjectDataset(dataset_id=ds.id, project_id=project.id))
    await db.commit()
    dataset_id = str(datasets[0].id)
    auth = headers(super_admin)
    task_result = await httpx_client.get(
        f"/api/v1/tasks?project_id={project.id}&dataset_id={dataset_id}", headers=auth
    )
    assert task_result.status_code == 200, task_result.text
    assert task_result.json()["total"] == 2
    base = f"/api/v1/projects/{project.id}/training-datasets"
    report = await httpx_client.post(
        base + ":preflight", headers=auth, json={"dataset_id": dataset_id}
    )
    assert report.status_code == 200 and report.json()["ready"], report.text
    assert report.json()["summary"]["eligible"] == 2
    create = await httpx_client.post(
        base,
        headers=auth,
        json=dict(
            dataset_id=dataset_id,
            name="Dataset subset",
            request_id=str(uuid.uuid4()),
            preflight_digest=report.json()["digest"],
        ),
    )
    assert create.status_code == 202, create.text
    versions = await httpx_client.get(
        f"/api/v1/datasets/{dataset_id}/training-versions?limit=1", headers=auth
    )
    assert versions.status_code == 200, versions.text
    assert (
        versions.json()["total"] == 1
        and versions.json()["items"][0]["id"] == create.json()["id"]
    )
    assert (
        await httpx_client.get(
            f"/api/v1/datasets/{datasets[1].id}/training-versions", headers=auth
        )
    ).json()["total"] == 0
    assert (
        await httpx_client.get(
            f"/api/v1/datasets/{dataset_id}/training-versions",
            headers=headers(project_admin),
        )
    ).json()["total"] == 0
    mismatch = await httpx_client.post(
        base + ":preflight",
        headers=auth,
        json={
            "dataset_id": dataset_id,
            "task_ids": [str(tasks[2].id)],
            "mode": "manager_confirmed",
        },
    )
    assert mismatch.status_code == 404
    assert (
        await httpx_client.post(
            base + ":preflight", headers=auth, json={"dataset_id": str(uuid.uuid4())}
        )
    ).status_code == 404


async def test_shared_managed_item_filters_each_source_and_chooses_visible_preview(
    db_session, content_world, super_admin, project_admin, httpx_client
):
    from datetime import datetime, timezone

    from app.schemas.dataset_source import DatasetSourceCreate
    from app.schemas.storage_location import FilesystemLocationCreate
    from app.services import (
        dataset_scans as scans,
        filesystem_storage as fs,
        storage_location as locations,
    )
    from app.services.dataset_storage_keys import managed_dataset_prefix

    db, world = db_session, content_world
    world[5].ownership_mode, world[5].cache_policy = "managed_copy", "preview"
    await db.commit()
    source_ids = [world[5].id]
    try:
        original = await accept_all_files(db, world)
        item_id = original["图片 空格.png"]["item_id"]
        other_location = await locations.create_filesystem(
            db,
            FilesystemLocationCreate(
                name=f"visible-{uuid.uuid4().hex}", root_handle="test", scope="global"
            ),
        )
        probe = fs.probe_directory(other_location.config, "source/图片 空格.png")
        for node in [locations.api_node_id(), "worker:content@test"]:
            await locations.store_probe(
                db, other_location, node, probe, datetime.now(timezone.utc)
            )
        other_source = await scans.create_source(
            db,
            DatasetSourceCreate(
                dataset_id=world[3].id,
                location_id=other_location.id,
                name="Second source",
                node_id="content@test",
                relative_path="source",
                ownership_mode="managed_copy",
                cache_policy="preview",
            ),
            world[2],
        )
        source_ids.append(other_source.id)
        other_world = (*world[:4], other_location, other_source, world[6], world[7])
        other_world = await rescan(db, other_world)
        reused = await accept_all_files(db, other_world)
        assert reused["图片 空格.png"]["item_id"] == item_id
        url = f"/api/v1/datasets/{world[3].id}/items"
        for source_id in source_ids:
            page = await httpx_client.get(
                url, params={"source_id": str(source_id)}, headers=headers(super_admin)
            )
            assert page.status_code == 200 and page.json()["total"] == 1, page.text
            assert page.json()["items"][0]["availability"]["source_id"] == str(
                source_id
            )
        world[3].created_by = project_admin[0].id
        await db.commit()
        item = await httpx_client.get(
            url + f"/{item_id}", headers=headers(project_admin)
        )
        assert item.status_code == 200, item.text
        assert item.json()["availability"]["source_id"] == str(other_source.id)
        assert item.json()["availability"]["preview_available"]
        preview = await httpx_client.get(
            url + f"/{item_id}/preview", headers=headers(project_admin)
        )
        assert preview.status_code == 200, preview.text
        assert (
            await httpx_client.get(
                url,
                params={"source_id": str(source_ids[0])},
                headers=headers(project_admin),
            )
        ).status_code == 404
    finally:
        for preview in await db.scalars(
            select(DatasetSourcePreview).where(
                DatasetSourcePreview.source_id.in_(source_ids)
            )
        ):
            if preview.object_key:
                storage_service.delete_object(preview.object_key, bucket=preview.bucket)
        storage_service.delete_prefix(
            managed_dataset_prefix(world[3].id), bucket=storage_service.datasets_bucket
        )


async def test_revalidation_is_not_reversed_when_same_run_finishes_added_registration(
    db_session, content_world, super_admin, httpx_client
):
    from app.services import dataset_source_revisions as revisions
    from tests.test_dataset_registration import begin_confirmed, drain
    from tests.test_dataset_source_revisions import candidate

    db, world = db_session, content_world
    original = await accept_all_files(db, world)
    item_id = original["图片 空格.png"]["item_id"]
    (world[0] / "new.png").write_bytes(picture("blue"))
    world, run, change, payload = await candidate(db, world, "red")
    url = f"/api/v1/datasets/{world[3].id}/items/{item_id}"
    auth = headers(super_admin)
    assert (await httpx_client.get(url, headers=auth)).json()["availability"][
        "state"
    ] == "changed"
    await revisions.accept(
        db, run.id, change["path_digest"], payload, world[2], revalidate=True
    )
    assert (await httpx_client.get(url, headers=auth)).json()["availability"][
        "state"
    ] == "verified"
    task_id, fence = await begin_confirmed(db, run, world)
    await drain(db, run, task_id, fence)
    assert run.status == "completed" and run.phase == "registering"
    assert (await httpx_client.get(url, headers=auth)).json()["availability"][
        "state"
    ] == "verified"


async def test_finishing_older_registration_does_not_override_newer_scan_evidence(
    db_session, content_world, super_admin, httpx_client
):
    from tests.test_dataset_registration import begin_confirmed, drain

    db, world = db_session, content_world
    original = await accept_all_files(db, world)
    item_id = original["图片 空格.png"]["item_id"]
    (world[0] / "图片 空格.png").unlink()
    (world[0] / "new.png").write_bytes(picture("blue"))
    world = await rescan(db, world)
    older = await preflight(db, world)
    url = f"/api/v1/datasets/{world[3].id}/items/{item_id}"
    auth = headers(super_admin)
    assert (await httpx_client.get(url, headers=auth)).json()["availability"][
        "state"
    ] == "missing"
    (world[0] / "图片 空格.png").write_bytes(b"broken image")
    world = await rescan(db, world)
    await preflight(db, world)
    assert (await httpx_client.get(url, headers=auth)).json()["availability"][
        "state"
    ] == "unavailable"
    task_id, fence = await begin_confirmed(db, older, world)
    await drain(db, older, task_id, fence)
    assert older.status == "completed"
    assert (await httpx_client.get(url, headers=auth)).json()["availability"][
        "state"
    ] == "unavailable"


@pytest.mark.parametrize("single", [False, True])
async def test_concurrent_item_delete_during_page_serialization_does_not_fail(
    db_session, super_admin, httpx_client, monkeypatch, single
):
    from app.services.dataset import DatasetService

    db = db_session
    dataset = Dataset(
        display_id=f"WB-{uuid.uuid4().hex[:10]}",
        name="Concurrent",
        created_by=super_admin[0].id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id, file_name="gone.png", file_path="gone.png"
    )
    db.add(item)
    await db.commit()
    dataset_id, item_id = dataset.id, item.id
    original = DatasetService.list_items

    async def deleted_between_reads(service, *args, **kwargs):
        await db.delete(item)
        await db.flush()
        return await original(service, *args, **kwargs)

    monkeypatch.setattr(DatasetService, "list_items", deleted_between_reads)
    url = f"/api/v1/datasets/{dataset_id}/items" + (f"/{item_id}" if single else "")
    response = await httpx_client.get(url, headers=headers(super_admin))
    assert response.status_code == (404 if single else 200), response.text
    if not single:
        assert response.json()["items"] == []


@pytest.mark.parametrize("scope", ["owner", "global"])
async def test_location_allowed_actions_follow_connector_admin_permissions(
    db_session, super_admin, project_admin, httpx_client, scope
):
    from app.db.models.storage_connection import StorageConnection
    from app.services.storage_location import register_connection

    db = db_session
    connection = StorageConnection(
        name=f"actions-{uuid.uuid4().hex}",
        kind="s3",
        scope=scope,
        created_by=project_admin[0].id,
        config={"endpoint": "example.test", "bucket": "test"},
    )
    db.add(connection)
    await db.flush()
    location = await register_connection(db, connection)
    await db.commit()
    url = f"/api/v1/storage-locations/{location.id}"
    actions = (await httpx_client.get(url, headers=headers(project_admin))).json()[
        "allowed_actions"
    ]
    assert ("manage_connection" in actions) is (scope == "owner")
    assert "manage_location" not in actions
    admin = (await httpx_client.get(url, headers=headers(super_admin))).json()[
        "allowed_actions"
    ]
    assert "manage_connection" in admin and "manage_location" in admin


async def test_combined_format_dimensions_count_before_pagination(
    db_session, super_admin, httpx_client
):
    db = db_session
    ds = Dataset(
        display_id=f"FLT-{uuid.uuid4().hex[:8]}",
        name="Filters",
        created_by=super_admin[0].id,
    )
    db.add(ds)
    await db.flush()
    values = [
        ("nested.dir/a.JPG", "image", 640, 480),
        ("b.jpeg", "image", 1280, 720),
        ("c.jpg", "image", 320, 240),
        ("d.png", "image", 640, 480),
        ("dir.jpg/no-extension", "image", 640, 480),
        ("unknown.jpg", "image", None, 480),
        ("invalid.jpg", "image", 0, 480),
        ("movie.JPG", "video", 640, 480),
        ("calib.json", "other", None, None),
    ]
    for name, kind, width, height in values:
        db.add(
            DatasetItem(
                dataset_id=ds.id,
                file_name=name,
                file_path=f"opaque/{uuid.uuid4()}",
                file_type=kind,
                width=width,
                height=height,
            )
        )
    await db.commit()
    url, auth = f"/api/v1/datasets/{ds.id}/items", headers(super_admin)
    query = dict(
        extension=".JPEG",
        media_type="image",
        min_width=640,
        max_height=720,
        availability="unverified",
        limit=1,
    )
    first = await httpx_client.get(url, params=query, headers=auth)
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2 and len(first.json()["items"]) == 1
    second = (
        await httpx_client.get(url, params={**query, "offset": 1}, headers=auth)
    ).json()
    assert (
        second["total"] == 2
        and second["items"][0]["id"] != first.json()["items"][0]["id"]
    )
    assert (
        await httpx_client.get(
            url, params={"extension": "jpg", "dimension_state": "unknown"}, headers=auth
        )
    ).json()["total"] == 2
    assert (
        await httpx_client.get(url, params={"dimension_state": "known"}, headers=auth)
    ).json()["total"] == 6
    assert (
        await httpx_client.get(
            url,
            params={
                "extension": "jpg",
                "search": "nested.dir",
                "min_width": 640,
                "max_width": 640,
            },
            headers=auth,
        )
    ).json()["total"] == 1
    other = await httpx_client.get(
        url, params={"media_type": "other", "extension": "json"}, headers=auth
    )
    assert other.status_code == 200 and other.json()["total"] == 1
    for invalid in (
        {"min_width": 0},
        {"max_height": -1},
        {"min_height": 100, "max_height": 20},
        {"dimension_state": "unknown", "max_width": 100},
        {"media_type": "jpg"},
        {"extension": "../jpg"},
    ):
        response = await httpx_client.get(url, params=invalid, headers=auth)
        assert response.status_code == 422, response.text

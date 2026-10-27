import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import DatasetScanEntry
from app.services import dataset_registration as registration
from app.services import dataset_source_access as access
from app.services import content_snapshot as snapshots
from app.services import storage_location as locations
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_registration import start


@pytest.fixture
async def managed_world(db_session, content_world, project_admin):
    actor = project_admin[0]
    content_world[3].created_by = actor.id
    location = content_world[4]
    location.config = {
        **location.config,
        "scope": "restricted",
        "use_user_ids": [str(actor.id)],
    }
    await db_session.commit()
    return content_world, actor


async def test_manager_scope_inherits_dataset_owner_and_location_grant(
    db_session, managed_world, httpx_client, project_admin
):
    world, actor = managed_world
    source, scan = world[5:7]
    auth = headers(project_admin)
    response = await httpx_client.get("/api/v1/dataset-sources", headers=auth)
    assert response.status_code == 200 and response.json()["total"] == 1
    assert (
        await httpx_client.get(f"/api/v1/dataset-scans/{scan.id}/entries", headers=auth)
    ).status_code == 200
    world[3].created_by = world[2].id
    await db_session.commit()
    assert (await httpx_client.get("/api/v1/dataset-sources", headers=auth)).json()[
        "total"
    ] == 0
    assert (
        await httpx_client.get(f"/api/v1/dataset-sources/{source.id}", headers=auth)
    ).status_code == 404
    response = await httpx_client.post(
        "/api/v1/dataset-sources",
        headers=auth,
        json={
            "dataset_id": str(world[3].id),
            "location_id": str(world[4].id),
            "name": "forbidden",
            "node_id": "content@test",
        },
    )
    assert response.status_code == 404


async def test_grant_withdrawal_hides_every_source_resource_and_jobs(
    db_session, managed_world, httpx_client, project_admin
):
    world, actor = managed_world
    run, _ = await registration.start_preflight(db_session, world[6].id, actor)
    await db_session.commit()
    source_id, scan_id, run_id, job_id = (
        world[5].id,
        world[6].id,
        run.id,
        run.async_job_id,
    )
    auth = headers(project_admin)
    paths = [
        f"/dataset-sources/{source_id}",
        f"/dataset-sources/{source_id}/scans",
        f"/dataset-scans/{scan_id}",
        f"/dataset-scans/{scan_id}/entries",
        f"/dataset-scans/{scan_id}/registration",
        f"/dataset-registrations/{run_id}/entries",
    ]
    for path in paths:
        response = await httpx_client.get("/api/v1" + path, headers=auth)
        assert response.status_code == 200, (path, response.text)
    assert (
        await httpx_client.get(f"/api/v1/async-jobs/{job_id}", headers=auth)
    ).status_code == 200
    world[4].config = {**world[4].config, "use_user_ids": []}
    await db_session.commit()
    for path in paths:
        response = await httpx_client.get("/api/v1" + path, headers=auth)
        assert response.status_code == 404, (path, response.text)
    assert (await httpx_client.get("/api/v1/dataset-sources", headers=auth)).json()[
        "total"
    ] == 0
    assert (
        await httpx_client.get(f"/api/v1/async-jobs/{job_id}", headers=auth)
    ).status_code == 403
    jobs = (await httpx_client.get("/api/v1/async-jobs", headers=auth)).json()
    assert not any(
        j["kind"] in {"dataset_scan", "dataset_registration"} for j in jobs["items"]
    )
    assert (
        await httpx_client.post(f"/api/v1/async-jobs/{job_id}/cancel", headers=auth)
    ).status_code == 403


@pytest.mark.parametrize("revocation", ["grant", "role"])
async def test_revocation_during_snapshot_prevents_receipt(
    db_session, managed_world, monkeypatch, revocation
):
    world, actor = managed_world
    run, task_id, fence = await start(db_session, (*world[:2], actor, *world[3:]))
    work = await registration.next_entry(db_session, run.id, task_id, fence)
    await db_session.commit()
    scan_id, digest = world[6].id, work["path_digest"]
    original = snapshots.capture

    async def revoked(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        if revocation == "grant":
            world[4].config = {**world[4].config, "use_user_ids": []}
        else:
            actor.role = "employee"
        await db_session.commit()
        return snapshot

    monkeypatch.setattr(snapshots, "capture", revoked)
    from app.workers.dataset_registration import process_one

    with pytest.raises(HTTPException) as exc:
        await process_one(db_session, run.id, task_id, fence, work)
    assert exc.value.status_code in {403, 404}
    await db_session.rollback()
    entry = await db_session.get(DatasetScanEntry, (scan_id, digest))
    assert entry.verified_content is None
    assert await db_session.scalar(select(func.count()).select_from(DatasetItem)) == 0


async def test_management_preview_revocation_keeps_assigned_task_reading(
    db_session, reference_world, project_admin, annotator, httpx_client
):
    registered, _, task, world = reference_world
    world[3].created_by = project_admin[0].id
    world[4].config = {
        **world[4].config,
        "scope": "restricted",
        "use_user_ids": [str(project_admin[0].id)],
    }
    await db_session.commit()
    preview = f"/api/v1/dataset-items/{registered['item_id']}/content?sha256={registered['sha256']}"
    assert (
        await httpx_client.get(preview, headers=headers(project_admin))
    ).status_code == 200
    world[4].config = {**world[4].config, "use_user_ids": []}
    await db_session.commit()
    assert (
        await httpx_client.get(preview, headers=headers(project_admin))
    ).status_code == 404
    response = await httpx_client.get(
        f"/api/v1/files/tasks/{task.id}/content?sha256={registered['sha256']}",
        headers=headers(annotator),
    )
    assert response.status_code == 200 and response.content == world[1]


async def test_legacy_dataset_writes_cannot_bypass_directory_owner(
    db_session, content_world, project_admin, httpx_client
):
    dataset_id = content_world[3].id
    auth = headers(project_admin)
    response = await httpx_client.put(
        f"/api/v1/datasets/{dataset_id}", headers=auth, json={"name": "forbidden"}
    )
    assert response.status_code == 404, response.text
    response = await httpx_client.delete(f"/api/v1/datasets/{dataset_id}", headers=auth)
    assert response.status_code == 404, response.text
    assert await access.require_source(
        db_session, content_world[5].id, content_world[2]
    )


async def test_retained_job_attempt_is_authorized_via_source_not_job_creator(
    db_session, managed_world, project_admin, httpx_client
):
    world, actor = managed_world
    run, _ = await registration.start_preflight(db_session, world[6].id, world[2])
    await db_session.commit()
    old_id = run.async_job_id
    await registration.cancel_job(db_session, old_id, actor)
    await registration.resume(db_session, run.id, actor)
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/async-jobs/{old_id}", headers=headers(project_admin)
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.get(
        "/api/v1/async-jobs",
        headers=headers(project_admin),
        params={"kind": "dataset_registration"},
    )
    assert {str(old_id), str(run.async_job_id)} <= {
        r["id"] for r in response.json()["items"]
    }


async def test_directory_browse_checks_grants_after_io(
    db_session, managed_world, httpx_client, project_admin, monkeypatch
):
    world, _ = managed_world
    original = locations.browse

    async def revoked(*args, **kwargs):
        result = await original(*args, **kwargs)
        world[4].config = {**world[4].config, "use_user_ids": []}
        await db_session.commit()
        return result

    monkeypatch.setattr(locations, "browse", revoked)
    response = await httpx_client.get(
        f"/api/v1/storage-locations/{world[4].id}/directories",
        headers=headers(project_admin),
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("phase", ["queued", "during_io"])
@pytest.mark.parametrize("change", ["rotate", "revoke"])
async def test_scan_key_authority_is_frozen_before_dispatch_and_io(
    db_session, managed_world, httpx_client, project_admin, phase, change
):
    import uuid
    from datetime import datetime, timezone

    from app.db.models.api_key import ApiKey
    from app.db.models.async_job import AsyncJob
    from app.services import dataset_scans as scans
    from app.services import filesystem_storage as fs

    world, actor = managed_world
    created = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "source-write", "scopes": ["datasets:write"]},
    )
    assert created.status_code == 201
    key = await db_session.get(ApiKey, uuid.UUID(created.json()["id"]))
    run, _ = await scans.start_scan(db_session, world[5].id, actor, api_key_id=key.id)
    job = await db_session.get(AsyncJob, run.async_job_id)
    if phase == "during_io":
        fence = await scans.claim(
            db_session, run.id, job.celery_task_id, "content@test"
        )
        while True:
            work = await scans.next_directory(
                db_session, run.id, job.celery_task_id, fence
            )
            if not work.get("transition"):
                break
        manifest = fs.scan_directory(
            work["config"],
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
    if change == "rotate":
        key.key_hash = "rotated-source-key"
    else:
        key.revoked_at = datetime.now(timezone.utc)
    await db_session.flush()
    if phase == "queued":
        assert (
            await scans.claim(db_session, run.id, job.celery_task_id, "content@test")
            is None
        )
        assert run.status == "failed" and run.error_code == "api_key_authority_revoked"
    else:
        with pytest.raises(HTTPException) as exc:
            await scans.apply_directory(
                db_session, run.id, job.celery_task_id, fence, work, manifest
            )
        assert exc.value.status_code == 403
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(DatasetScanEntry)
                .where(
                    DatasetScanEntry.run_id == run.id,
                    DatasetScanEntry.relative_path != "",
                )
            )
            == 0
        )


async def test_read_scope_applies_to_source_jobs_and_location_metadata(
    db_session, managed_world, httpx_client, project_admin
):
    world, actor = managed_world
    run, _ = await registration.start_preflight(db_session, world[6].id, actor)
    await db_session.commit()
    key = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "source-write", "scopes": ["datasets:write"]},
    )
    auth = {"Authorization": "Bearer " + key.json()["plaintext"]}
    response = await httpx_client.get("/api/v1/async-jobs", headers=auth)
    assert response.status_code == 200 and response.json()["total"] == 0
    for path in [
        f"/async-jobs/{run.async_job_id}",
        "/storage-locations",
        f"/storage-locations/{world[4].id}/probes",
        "/dataset-sources",
    ]:
        assert (
            await httpx_client.get("/api/v1" + path, headers=auth)
        ).status_code == 403


async def test_probe_does_not_publish_after_grant_is_withdrawn(
    db_session, managed_world, httpx_client, project_admin, monkeypatch
):
    world, _ = managed_world
    original = locations.perform_probe

    async def revoked(*args, **kwargs):
        result = await original(*args, **kwargs)
        world[4].config = {**world[4].config, "use_user_ids": []}
        await db_session.commit()
        return result

    monkeypatch.setattr(locations, "perform_probe", revoked)
    response = await httpx_client.post(
        f"/api/v1/storage-locations/{world[4].id}/probe",
        headers=headers(project_admin),
        json={"node_id": "api"},
    )
    assert response.status_code == 404


async def test_registration_claim_rechecks_authority_after_probe(
    db_session, managed_world, monkeypatch
):
    from app.db.models.async_job import AsyncJob

    world, actor = managed_world
    run, _ = await registration.start_preflight(db_session, world[6].id, actor)
    await db_session.commit()
    job = await db_session.get(AsyncJob, run.async_job_id)
    original = registration.probe_candidates

    async def revoked(*args, **kwargs):
        result = await original(*args, **kwargs)
        actor.role = "employee"
        await db_session.flush()
        return result

    monkeypatch.setattr(registration, "probe_candidates", revoked)
    with pytest.raises(HTTPException) as exc:
        await registration.claim(db_session, run.id, job.celery_task_id, "content@test")
    assert exc.value.status_code == 403
    assert run.status == "pending"


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_source_configuration_rechecks_key_after_lock_wait(
    db_session, managed_world, project_admin, httpx_client, monkeypatch, operation
):
    import uuid
    from app.db.models.api_key import ApiKey
    from app.services import dataset_scans as scans
    from app.schemas.dataset_source import DatasetSourceCreate, DatasetSourceUpdate

    world, actor = managed_world
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "configuration", "scopes": ["datasets:write"]},
    )
    key = await db_session.get(ApiKey, uuid.UUID(response.json()["id"]))
    original = locations.get_visible if operation == "create" else scans.source_context

    async def rotated(*args, **kwargs):
        result = await original(*args, **kwargs)
        if operation == "update" or kwargs.get("lock"):
            key.key_hash = "rotated-while-waiting"
            await db_session.flush()
        return result

    if operation == "create":
        monkeypatch.setattr(locations, "get_visible", rotated)
    else:
        monkeypatch.setattr(scans, "source_context", rotated)
    with pytest.raises(HTTPException) as exc:
        if operation == "create":
            await scans.create_source(
                db_session,
                DatasetSourceCreate(
                    dataset_id=world[3].id,
                    location_id=world[4].id,
                    name="new",
                    node_id="content@test",
                ),
                actor,
                api_key_id=key.id,
            )
        else:
            await scans.update_source(
                db_session,
                world[5].id,
                DatasetSourceUpdate(
                    expected_revision=world[5].config_revision, name="new"
                ),
                actor,
                api_key_id=key.id,
            )
    assert exc.value.status_code == 403


async def test_upload_completion_rechecks_initial_key_after_hashing(
    db_session, managed_world, project_admin, httpx_client, monkeypatch
):
    import uuid
    from app.db.models.api_key import ApiKey
    from app.db.models.dataset_asset import DatasetAsset
    from app.services.storage import storage_service
    from tests.test_dataset_assets import ObjectClient
    from app.services import managed_original_creation as creations

    world, _ = managed_world
    fake = ObjectClient()
    monkeypatch.setattr(storage_service, "client", fake)
    monkeypatch.setattr(creations, "create_client", lambda **_: fake)
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "upload", "scopes": ["datasets:write"]},
    )
    key = await db_session.get(ApiKey, uuid.UUID(response.json()["id"]))
    auth = {"Authorization": "Bearer " + response.json()["plaintext"]}
    init = await httpx_client.post(
        f"/api/v1/datasets/{world[3].id}/items/upload-init",
        headers=auth,
        json={"file_name": "a.bin", "content_type": "application/octet-stream"},
    )
    assert init.status_code == 200
    item_id = uuid.UUID(init.json()["item_id"])
    fake.objects[fake.presigned[-1][1]["Key"]] = b"original"
    original = creations.complete_direct

    async def rotated(*args, **kwargs):
        result = await original(*args, **kwargs)
        key.key_hash = "rotated-during-hashing"
        await db_session.flush()
        return result

    monkeypatch.setattr(creations, "complete_direct", rotated)
    response = await httpx_client.post(
        f"/api/v1/datasets/{world[3].id}/items/upload-complete/{item_id}",
        headers=auth,
    )
    assert response.status_code == 403
    asset = await db_session.scalar(
        select(DatasetAsset).where(DatasetAsset.dataset_item_id == item_id)
    )
    assert asset.verification_state == "unverified"


async def test_directory_link_chunks_recheck_actor_before_next_commit(
    db_session, reference_world, monkeypatch
):
    from app.db.models.task import Task
    from app.services.dataset import build_tasks_for_link

    _, project, _, world = reference_world
    actor, dataset = world[2:4]
    for i in range(2):
        db_session.add(
            DatasetItem(
                dataset_id=dataset.id,
                file_name=f"extra-{i}.png",
                file_path=f"legacy/extra-{i}.png",
                file_type="image",
            )
        )
    await db_session.commit()
    authority = await access.capture_dataset_write(db_session, dataset.id, actor, None)
    original = db_session.commit
    calls = []

    async def revoked():
        await original()
        calls.append(True)
        actor.role = "employee"
        await db_session.flush()

    monkeypatch.setattr(db_session, "commit", revoked)
    with pytest.raises(HTTPException) as exc:
        await build_tasks_for_link(
            db_session,
            dataset_id=dataset.id,
            project_id=project.id,
            chunk_size=1,
            directory_authority=authority,
        )
    assert exc.value.status_code == 403 and len(calls) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 2
    )


async def test_directory_copy_ingest_retains_creation_after_revocation(
    db_session, managed_world, monkeypatch
):
    import io
    from app.services import managed_original_creation as originals
    from app.services.dataset import DatasetService
    from app.services.storage import storage_service
    from tests.fakes.managed_creation_storage import CreationStorage

    world, actor = managed_world
    authority = await access.capture_dataset_write(db_session, world[3].id, actor, None)
    fake = CreationStorage()
    monkeypatch.setattr(storage_service, "client", fake)
    monkeypatch.setattr(originals, "create_client", lambda **_: fake)
    create_stream = originals.create_stream
    completed = []

    async def revoked(db, allocation, stream, **kwargs):
        receipt = await create_stream(db, allocation, stream, **kwargs)
        completed.append(allocation)
        actor.role = "employee"
        return receipt

    monkeypatch.setattr(originals, "create_stream", revoked)
    with pytest.raises(HTTPException):
        await DatasetService(db_session).ingest_one(
            world[3].id,
            "remote.bin",
            io.BytesIO(b"copy"),
            directory_authority=authority,
        )
    assert len(completed) == 1
    allocation = completed[0]
    assert fake.objects[allocation["object_key"]] == b"copy"
    assert not any(call[0] == "delete" for call in fake.calls)
    durable = await originals.for_key(
        db_session,
        dataset_id=world[3].id,
        object_key=allocation["object_key"],
    )
    assert durable["status"] == "verified"


async def test_async_job_does_not_expose_directory_authority(
    db_session, super_admin, content_world, httpx_client
):
    from app.services import async_job as jobs

    job = await jobs.create_job(
        db_session,
        kind="dataset_import",
        user_id=super_admin[0].id,
        payload={
            "dataset_id": str(content_world[3].id),
            "directory_authority": {"key_proof": "private"},
        },
    )
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}", headers=headers(super_admin)
    )
    assert response.status_code == 200
    assert "directory_authority" not in response.json()["payload"]


async def test_directory_link_rechecks_after_dataset_lock(
    db_session, content_world, monkeypatch
):
    import uuid
    from app.db.models.dataset import Dataset, ProjectDataset
    from app.db.models.project import Project
    from app.services.dataset import DatasetService

    actor, dataset = content_world[2:4]
    project = Project(
        id=uuid.uuid4(),
        display_id="P-lock",
        name="Link",
        type_key="image-det",
        type_label="Image",
        owner_id=actor.id,
    )
    db_session.add(project)
    await db_session.commit()
    authority = await access.capture_dataset_write(db_session, dataset.id, actor, None)
    original = db_session.scalar

    async def revoked(statement, *args, **kwargs):
        result = await original(statement, *args, **kwargs)
        descriptions = getattr(statement, "column_descriptions", [])
        if (
            descriptions
            and descriptions[0].get("entity") is Dataset
            and statement._for_update_arg is not None
        ):
            actor.role = "employee"
            await db_session.flush()
        return result

    monkeypatch.setattr(db_session, "scalar", revoked)
    with pytest.raises(HTTPException) as exc:
        await DatasetService(db_session).link_project(
            dataset.id, project.id, directory_authority=authority
        )
    assert exc.value.status_code == 403
    assert (
        await db_session.scalar(select(func.count()).select_from(ProjectDataset)) == 0
    )


async def test_zip_into_directory_dataset_keeps_non_temporal_registration(
    db_session, managed_world, project_admin, httpx_client, monkeypatch
):
    import io
    import zipfile
    from app.services import scene_inference
    from app.services.storage import storage_service
    from tests.test_dataset_assets import ObjectClient
    from app.services.dataset import DatasetService

    world, _ = managed_world
    from app.services import managed_original_creation as creations

    fake = ObjectClient()
    monkeypatch.setattr(storage_service, "client", fake)
    monkeypatch.setattr(creations, "create_client", lambda **_: fake)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Directory image ZIP must not create scenes")

    async def no_enqueue(*args, **kwargs):
        pass

    monkeypatch.setattr(scene_inference, "infer_and_apply", unexpected)
    monkeypatch.setattr(DatasetService, "enqueue_media_for_items", no_enqueue)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("note.txt", b"ordinary managed file")
    response = await httpx_client.post(
        f"/api/v1/datasets/{world[3].id}/items/upload-zip",
        headers=headers(project_admin),
        files={"file": ("mixed.zip", output.getvalue(), "application/zip")},
    )
    assert response.status_code == 200 and response.json()["added"] == 1, response.text
    assert any(
        "不自动创建 Scene" in s for s in response.json()["scene_inference_notes"]
    )


@pytest.mark.parametrize("cleanup", ["missing", "duplicate"])
async def test_upload_cleanup_is_not_a_revocation_bypass(
    db_session, managed_world, project_admin, httpx_client, monkeypatch, cleanup
):
    import uuid
    from app.services import dataset_assets
    from app.services.storage import storage_service
    from tests.test_dataset_assets import ObjectClient

    world, actor = managed_world
    fake = ObjectClient()
    monkeypatch.setattr(storage_service, "client", fake)
    from app.services.dataset import DatasetService

    # This exercises completion of upload URLs issued before staging isolation.
    item = await DatasetService(db_session).add_item(
        world[3].id, "a.bin", f"legacy-upload/{uuid.uuid4()}/a.bin", "other"
    )
    item_id = item.id
    key = item.file_path
    if cleanup == "duplicate":
        fake.objects[key] = b"original"

        async def revoke_after_lookup(*args, **kwargs):
            actor.role = "employee"
            await db_session.flush()
            return item

        monkeypatch.setattr(
            dataset_assets, "find_verified_content", revoke_after_lookup
        )
    else:

        def revoke_after_head(*args, **kwargs):
            actor.role = "employee"
            return None

        monkeypatch.setattr(storage_service, "verify_upload", revoke_after_head)
    response = await httpx_client.post(
        f"/api/v1/datasets/{world[3].id}/items/upload-complete/{item_id}",
        headers=headers(project_admin),
    )
    assert response.status_code == 403, response.text
    assert await db_session.get(DatasetItem, item_id) is not None
    if cleanup == "duplicate":
        assert fake.objects[key] == b"original"

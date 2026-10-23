"""Incremental preflight never rebinds accepted samples or infers deletion."""

import io
import os

from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import DatasetSourceItem
from app.services import dataset_source_changes as changes
from app.services import dataset_registration as registration
from app.services import dataset_scans as scans
from app.services import filesystem_storage as fs
from app.services.dataset_references import register_entry
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_registration import drain, start
from tests.test_dataset_references import reference_world as reference_world


def picture(color):
    body = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(body, format="PNG")
    return body.getvalue()


async def rescan(db, world):
    run, _ = await scans.start_scan(db, world[5].id, world[2])
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db, run.id, job.celery_task_id, "content@test")
    for _ in range(40):
        work = await scans.next_directory(db, run.id, job.celery_task_id, fence)
        if work is None:
            break
        if work.get("transition"):
            continue
        manifest = fs.scan_directory(
            work["config"],
            work["path"],
            expected_anchor=work["anchor"],
            expected_identity=work["identity"],
        )
        await scans.apply_directory(
            db, run.id, job.celery_task_id, fence, work, manifest
        )
    assert run.complete
    await db.commit()
    return (*world[:6], run, world[7])


async def accept_all_files(db, world):
    from app.db.models.dataset_source import DatasetScanEntry

    entries = list(
        await db.scalars(
            select(DatasetScanEntry).where(
                DatasetScanEntry.run_id == world[6].id,
                DatasetScanEntry.kind == "file",
                DatasetScanEntry.state == "observed",
            )
        )
    )
    result = {}
    for entry in entries:
        result[entry.relative_path] = await register_entry(
            db, world[6].id, entry.path_digest, world[2], node_id="worker:content@test"
        )
    return result


async def preflight(db, world):
    run, task, fence = await start(db, world)
    await drain(db, run, task, fence)
    return run


async def test_three_unchanged_scans_and_only_added_selection(
    db_session, content_world
):
    db, world = db_session, content_world
    original = await accept_all_files(db, world)
    old_id = original["图片 空格.png"]["item_id"]
    for _ in range(3):
        world = await rescan(db, world)
        run = await preflight(db, world)
        page = await changes.page(db, run, None, 20, 0)
        assert page["complete"] and page["counts"]["unchanged"] == 1
        assert page["counts"]["added"] == 0
        with pytest.raises(HTTPException, match="409"):
            await registration.confirm(db, run.id, world[2], scope="additions_only")
        assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 1
    (world[0] / "new.png").write_bytes(picture("blue"))
    world = await rescan(db, world)
    run = await preflight(db, world)
    assert (await changes.page(db, run, None, 20, 0))["counts"]["added"] == 1
    run, created = await registration.confirm(
        db, run.id, world[2], scope="additions_only"
    )
    assert created and run.registration_scope == "additions_only"
    assert run.counts["unchanged_count"] == 1 and run.counts["ready_count"] == 1
    await db.commit()
    same, created = await registration.confirm(
        db, run.id, world[2], scope="additions_only"
    )
    assert not created and same.id == run.id
    with pytest.raises(HTTPException) as exc:
        await registration.confirm(db, run.id, world[2])
    assert exc.value.detail["code"] == "registration_scope_changed"
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    assert run.counts["registered_count"] == 1 and run.counts["unchanged_count"] == 1
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 2
    baseline = await db.get(
        DatasetSourceItem, (world[5].id, scans.path_digest("图片 空格.png"))
    )
    assert baseline.dataset_item_id == old_id
    page = await changes.page(db, run, "added", 1, 0)
    assert page["total"] == 1 and page["items"][0]["before_item_id"] is None
    assert page["items"][0]["registration_state"] == "registered"


async def test_frozen_baseline_changed_unseen_excluded_and_errors(
    db_session, content_world
):
    db, world = db_session, content_world
    root = world[0]
    for path in ["gone.png", "blocked.png", "same.png"]:
        (root / path).write_bytes(world[1])
    world = await rescan(db, world)
    original = await accept_all_files(db, world)
    (root / "图片 空格.png").write_bytes(picture("green"))
    (root / "gone.png").unlink()
    (root / "blocked.png").unlink()
    (root / "blocked.png").symlink_to(root / "same.png")
    (root / "broken.png").write_bytes(b"broken")
    (root / "fresh.png").write_bytes(picture("blue"))
    world = await rescan(db, world)
    run, task, fence = await start(db, world)
    hidden = await changes.page(db, run, None, 20, 0)
    assert not hidden["complete"] and hidden["items"] == [] and hidden["counts"] == {}
    await drain(db, run, task, fence)
    page = await changes.page(db, run, None, 20, 0)
    assert page["counts"] == dict(
        added=1, unchanged=1, changed=1, not_seen=1, excluded=1, unreadable=1
    )
    entries = {e["relative_path"]: e for e in page["items"]}
    assert entries["图片 空格.png"]["reason"] == "content_changed"
    assert (
        entries["图片 空格.png"]["before_item_id"]
        == original["图片 空格.png"]["item_id"]
    )
    assert (
        entries["图片 空格.png"]["before_sha256"]
        != entries["图片 空格.png"]["candidate_sha256"]
    )
    assert (
        entries["gone.png"]["kind"] == "not_seen"
        and entries["blocked.png"]["kind"] == "excluded"
    )
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 4
    # Concurrent direct acceptance must not rewrite this preflight's baseline.
    fresh = await register_entry(
        db,
        world[6].id,
        scans.path_digest("fresh.png"),
        world[2],
        node_id="worker:content@test",
    )
    assert fresh["created"]
    assert (await changes.page(db, run, "added", 20, 0))["items"][0][
        "before_item_id"
    ] is None
    await registration.confirm(db, run.id, world[2], scope="additions_only")
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 5
    assert run.counts["registered_count"] == 0 and run.counts["unchanged_count"] == 2
    assert (await changes.page(db, run, "changed", 20, 0))["items"][0][
        "before_item_id"
    ] == original["图片 空格.png"]["item_id"]
    # Short-lived job history can disappear without losing the baseline.
    await db.delete(job)
    await db.commit()
    assert (await changes.page(db, run, None, 20, 0))["total"] == 6
    for name in ["图片 空格.png", "broken.png"]:
        with pytest.raises(HTTPException):
            await registration.resume(
                db, run.id, world[2], path_digest=scans.path_digest(name)
            )


async def test_same_bytes_changed_observation_does_not_rebind(
    db_session, content_world
):
    world = content_world
    original = await accept_all_files(db_session, world)
    file = world[0] / "图片 空格.png"
    info = file.stat()
    os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    world = await rescan(db_session, world)
    run = await preflight(db_session, world)
    page = await changes.page(db_session, run, "changed", 20, 0)
    entry = page["items"][0]
    assert entry["reason"] == "observation_changed"
    assert entry["before_sha256"] == entry["candidate_sha256"]
    assert entry["before_item_id"] == original["图片 空格.png"]["item_id"]
    assert run.counts["ready_count"] == 0


async def test_same_size_and_mtime_different_bytes_are_classified_by_full_hash(
    db_session, content_world
):
    db, world = db_session, content_world
    file = world[0] / "图片 空格.png"
    blue, white = picture("blue"), picture("white")
    assert len(blue) == len(white)
    file.write_bytes(blue)
    world = await rescan(db, world)
    await accept_all_files(db, world)
    before = file.stat()
    file.write_bytes(white)
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = file.stat()
    assert (before.st_size, before.st_mtime_ns, before.st_ino) == (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    )
    world = await rescan(db, world)
    run = await preflight(db, world)
    row = (await changes.page(db, run, "changed", 20, 0))["items"][0]
    assert row["reason"] == "content_changed"
    assert row["before_sha256"] != row["candidate_sha256"]


async def test_unaccepted_previous_scan_is_still_added_and_api_is_scoped(
    db_session, content_world, httpx_client, super_admin, annotator
):
    world = await rescan(db_session, content_world)
    run = await preflight(db_session, world)
    url = f"/api/v1/dataset-registrations/{run.id}/changes"
    response = await httpx_client.get(
        url, params={"kind": "added", "limit": 1}, headers=headers(super_admin)
    )
    assert response.status_code == 200
    assert response.json()["counts"]["added"] == response.json()["total"] == 1
    assert response.json()["items"][0]["before_item_id"] is None
    assert (await httpx_client.get(url, headers=headers(annotator))).status_code == 403
    assert (
        await httpx_client.get(
            url, params={"kind": "missing"}, headers=headers(super_admin)
        )
    ).status_code == 422


async def test_cancelled_preflight_hides_unseen_and_rejects_old_fence(
    db_session, content_world
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    (world[0] / "new.png").write_bytes(world[1])
    (world[0] / "图片 空格.png").unlink()
    world = await rescan(db, world)
    run, task, fence = await start(db, world)
    await registration.cancel_job(db, run.async_job_id, world[2])
    await db.commit()
    assert not (await changes.page(db, run, None, 20, 0))["complete"]
    await registration.resume(db, run.id, world[2])
    await db.commit()
    with pytest.raises(registration.LostRegistrationLease):
        await registration.record(
            db, run.id, task, fence, scans.path_digest("new.png"), "error"
        )
    new_job = await db.get(AsyncJob, run.async_job_id)
    new_fence = await registration.claim(
        db, run.id, new_job.celery_task_id, "content@test"
    )
    await db.commit()
    await drain(db, run, new_job.celery_task_id, new_fence)
    assert (await changes.page(db, run, None, 20, 0))["counts"]["not_seen"] == 1


async def test_repaired_error_republishes_only_after_preflight_completes(
    db_session, content_world
):
    db, world = db_session, content_world
    bad = world[0] / "broken.png"
    bad.write_bytes(b"broken")
    world = await rescan(db, world)
    run = await preflight(db, world)
    assert (await changes.page(db, run, None, 20, 0))["counts"]["unreadable"] == 1
    bad.write_bytes(world[1])
    await registration.resume(
        db, run.id, world[2], path_digest=scans.path_digest(bad.name)
    )
    await db.commit()
    assert not (await changes.page(db, run, None, 20, 0))["complete"]
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    page = await changes.page(db, run, None, 20, 0)
    assert page["complete"] and page["counts"]["added"] == 2
    assert page["counts"]["unreadable"] == 0


async def test_added_content_replaced_after_confirmation_is_not_registered(
    db_session, content_world
):
    db, world = db_session, content_world
    run = await preflight(db, world)
    await registration.confirm(db, run.id, world[2], scope="additions_only")
    await db.commit()
    (world[0] / "图片 空格.png").write_bytes(picture("blue"))
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    assert run.counts["error_count"] == 1 and run.counts["registered_count"] == 0
    assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0
    page = await changes.page(db, run, "added", 20, 0)
    assert page["items"][0]["registration_state"] == "error"
    assert page["items"][0]["candidate_sha256"] is not None


async def test_incremental_acceptance_keeps_tasks_and_existing_annotation(
    db_session, httpx_client, annotator, reference_world
):
    from app.db.models.task import Task

    db = db_session
    original, project, task, world = reference_world
    task_id, item_id, file_path = task.id, task.dataset_item_id, task.file_path
    url = f"/api/v1/tasks/{task_id}/annotations"
    saved = await httpx_client.post(
        url,
        json={
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
        headers=headers(annotator),
    )
    assert saved.status_code == 201
    annotation_id = saved.json()["id"]
    for _ in range(3):
        world = await rescan(db, world)
        run = await preflight(db, world)
        assert (await changes.page(db, run, None, 20, 0))["counts"]["unchanged"] == 1
        assert await db.scalar(select(func.count()).select_from(Task)) == 1
    (world[0] / "new.png").write_bytes(picture("blue"))
    (world[0] / "图片 空格.png").write_bytes(picture("green"))
    world = await rescan(db, world)
    run = await preflight(db, world)
    await registration.confirm(db, run.id, world[2], scope="additions_only")
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await registration.claim(db, run.id, job.celery_task_id, "content@test")
    await db.commit()
    await drain(db, run, job.celery_task_id, fence)
    assert await db.scalar(select(func.count()).select_from(Task)) == 2
    await db.refresh(task)
    assert (task.dataset_item_id, task.file_path, task.project_id) == (
        item_id,
        file_path,
        project.id,
    )
    assert (await httpx_client.get(url, headers=headers(annotator))).json()[0][
        "id"
    ] == annotation_id
    denied = await httpx_client.get(
        f"/api/v1/files/tasks/{task_id}/content",
        params={"sha256": original["sha256"]},
        headers=headers(annotator),
    )
    assert denied.status_code == 409

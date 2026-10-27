"""Location visibility never widens retained source/job authority."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetSource, DatasetScanRun
from app.services import dataset_source_access as access
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_access import managed_world as managed_world


async def test_summary_scopes_counts_and_jobs_across_source_pages(
    db_session, managed_world, project_admin, super_admin, httpx_client
):
    db = db_session
    world, actor = managed_world
    location, source, run = world[4:7]
    location.config = {**location.config, "scope": "global"}
    source.enabled = False
    source.unregistered_at = datetime.now(timezone.utc)
    # A newer source page must not limit the jobs for older visible sources.
    for index in range(21):
        db.add(
            DatasetSource(
                dataset_id=world[3].id,
                location_id=location.id,
                name=f"page-{index}",
                config=source.config,
                created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
            )
        )
    other_dataset = Dataset(
        name="Other owner",
        display_id=f"other-{uuid.uuid4().hex[:10]}",
        created_by=world[2].id,
    )
    db.add(other_dataset)
    await db.flush()
    hidden = DatasetSource(
        dataset_id=other_dataset.id,
        location_id=location.id,
        name="private",
        config=source.config,
    )
    db.add(hidden)
    await db.flush()
    hidden_job = AsyncJob(kind="dataset_scan", status="completed", payload={})
    db.add(hidden_job)
    await db.flush()
    db.add(
        DatasetScanRun(
            source_id=hidden.id,
            async_job_id=hidden_job.id,
            snapshot=run.snapshot,
            status="completed",
            complete=True,
        )
    )
    old_job = AsyncJob(
        kind="dataset_scan", status="failed", payload={"run_id": str(run.id)}
    )
    malformed_job = AsyncJob(
        kind="dataset_scan",
        status="failed",
        payload={"run_id": "not-a-uuid", "source_id": str(source.id)},
    )
    db.add_all([old_job, malformed_job])
    await db.commit()
    url = f"/api/v1/storage-locations/{location.id}/source-summary"
    response = await httpx_client.get(url + "?limit=1", headers=headers(project_admin))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 22 and result["items"][0]["source_id"] != str(source.id)
    assert {job["id"] for job in result["jobs"]["items"]} == {
        str(run.async_job_id),
        str(old_job.id),
    }
    assert not any(
        key in str(result)
        for key in ["config_revision", "root_handle", "key_proof", "payload"]
    )
    page = (
        await httpx_client.get(
            url + "?limit=1&offset=21&jobs_limit=1&jobs_offset=1",
            headers=headers(project_admin),
        )
    ).json()
    assert page["items"][0]["source_id"] == str(source.id)
    assert page["items"][0]["unregistered_at"] and not page["items"][0]["enabled"]
    assert len(page["jobs"]["items"]) == 1 and page["jobs"]["total"] == 2
    # All visible sources includes the other owner only for a super-admin.
    assert (await httpx_client.get(url, headers=headers(super_admin))).json()[
        "total"
    ] == 23
    other_location = StorageLocation(
        id=uuid.uuid4(),
        name=f"elsewhere-{uuid.uuid4()}",
        kind="filesystem",
        config={"scope": "global"},
        enabled=False,
    )
    db.add(other_location)
    await db.commit()
    elsewhere = (
        await httpx_client.get(
            f"/api/v1/storage-locations/{other_location.id}/source-summary",
            headers=headers(super_admin),
        )
    ).json()
    assert elsewhere["total"] == elsewhere["jobs"]["total"] == 0
    assert not list(
        await db.scalars(select(AsyncJob).where(access.job_scope_clause(actor, [])))
    )
    assert (
        len(
            list(
                await db.scalars(select(AsyncJob).where(access.job_scope_clause(actor)))
            )
        )
        == 2
    )


async def test_summary_retains_orphan_history_only_for_superadmin(
    db_session, managed_world, project_admin, super_admin, httpx_client
):
    world, _ = managed_world
    source, run = world[5:7]
    source.dataset_id = None
    world[4].enabled = False
    await db_session.commit()
    url = f"/api/v1/storage-locations/{world[4].id}/source-summary"
    admin = (await httpx_client.get(url, headers=headers(super_admin))).json()
    assert admin["total"] == 1 and admin["items"][0]["dataset_id"] is None
    assert admin["items"][0]["dataset_name"] is None
    assert admin["jobs"]["total"] == 1
    manager = (await httpx_client.get(url, headers=headers(project_admin))).json()
    assert manager["total"] == manager["jobs"]["total"] == 0


@pytest.mark.parametrize("revocation", ["grant", "role", "inactive"])
async def test_summary_rechecks_current_authority(
    db_session, managed_world, project_admin, httpx_client, revocation
):
    world, actor = managed_world
    url = f"/api/v1/storage-locations/{world[4].id}/source-summary"
    assert (
        await httpx_client.get(url, headers=headers(project_admin))
    ).status_code == 200
    if revocation == "grant":
        world[4].config = {**world[4].config, "use_user_ids": []}
    elif revocation == "role":
        actor.role = "employee"
    else:
        actor.is_active = False
    await db_session.commit()
    assert (
        await httpx_client.get(url, headers=headers(project_admin))
    ).status_code in {401, 403, 404}


async def test_scan_filters_find_complete_and_resumed_active_outside_latest_page(
    db_session, content_world, super_admin, httpx_client
):
    source, original = content_world[5:7]
    for index in range(24):
        db_session.add(
            DatasetScanRun(
                source_id=source.id,
                snapshot=original.snapshot,
                status="failed",
                complete=False,
                created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
            )
        )
    original.status = "running"
    original.complete = False
    complete = DatasetScanRun(
        source_id=source.id,
        snapshot=original.snapshot,
        status="completed",
        complete=True,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(
        DatasetScanRun(
            source_id=source.id,
            snapshot=original.snapshot,
            status="completed",
            complete=True,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            completed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    db_session.add(complete)
    await db_session.commit()
    url = f"/api/v1/dataset-sources/{source.id}/scans"
    first = (
        await httpx_client.get(url + "?limit=20", headers=headers(super_admin))
    ).json()
    assert first["total"] == 27 and all(
        not row["complete"] and row["status"] == "failed" for row in first["items"]
    )
    last = (
        await httpx_client.get(
            url + "?complete=true&limit=1", headers=headers(super_admin)
        )
    ).json()
    assert last["total"] == 2 and last["items"][0]["id"] == str(complete.id)
    active = (
        await httpx_client.get(url + "?active_only=true", headers=headers(super_admin))
    ).json()
    assert active["total"] == 1 and active["items"][0]["id"] == str(original.id)
    second = (
        await httpx_client.get(
            url + "?limit=20&offset=20", headers=headers(super_admin)
        )
    ).json()
    assert len(second["items"]) == 7


async def test_active_registration_is_source_scoped_even_after_a_newer_scan(
    db_session, managed_world, project_admin, httpx_client
):
    from app.services import dataset_registration

    world, actor = managed_world
    run, _ = await dataset_registration.start_preflight(db_session, world[6].id, actor)
    newer = DatasetScanRun(
        source_id=world[5].id, snapshot=world[6].snapshot, status="failed"
    )
    db_session.add(newer)
    await db_session.commit()
    url = f"/api/v1/dataset-sources/{world[5].id}/active-registration"
    response = await httpx_client.get(url, headers=headers(project_admin))
    assert response.status_code == 200 and response.json()["id"] == str(run.id)
    run.status = "completed"
    await db_session.commit()
    assert (await httpx_client.get(url, headers=headers(project_admin))).json() is None
    world[4].config = {**world[4].config, "use_user_ids": []}
    await db_session.commit()
    assert (
        await httpx_client.get(url, headers=headers(project_admin))
    ).status_code == 404


async def test_navigation_endpoints_require_read_scope(
    managed_world, project_admin, httpx_client
):
    world, _ = managed_world
    response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "write only", "scopes": ["datasets:write"]},
    )
    assert response.status_code == 201, response.text
    auth = {"Authorization": "Bearer " + response.json()["plaintext"]}
    for path in [
        f"/storage-locations/{world[4].id}/source-summary",
        f"/dataset-sources/{world[5].id}/active-registration",
        f"/dataset-sources/{world[5].id}/scans?complete=true",
    ]:
        assert (
            await httpx_client.get("/api/v1" + path, headers=auth)
        ).status_code == 403

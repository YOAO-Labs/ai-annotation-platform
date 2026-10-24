"""Reference truth, retirement phases and physical-handle responsibility."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import threading
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.db.models.storage_maintenance import StorageOperation
from app.services import guide_asset_references as references
from app.services import (
    guide_asset_storage as storage,
    guide_asset_uploads as uploads,
    storage_access,
)
from tests.test_comment_attachment_uploads import bearer
from tests.test_guide_assets import (
    content,
    objects as objects,
    project as project,
    unlink,
    upload,
)


@pytest.mark.parametrize("state", ["ready", "pending"])
async def test_orphan_ready_only_retires_in_first_cycle_and_pending_is_never_collected(
    httpx_client, project_admin, project, objects, db_session, state
):
    value = await upload(httpx_client, project_admin, project)
    identifier = uuid.UUID(value["key"].split("/")[-1])
    row = await db_session.get(Upload, identifier)
    if state == "pending":
        # A separate historical allocation may have bytes after an interrupted PUT.
        receipt = storage.snapshot(row)
        pending = Upload(
            id=uuid.uuid4(),
            owner_id=receipt.owner_id,
            project_id=receipt.project_id,
            sha256=receipt.sha256,
            size_bytes=receipt.size_bytes,
            mime_type=receipt.mime_type,
            file_name=receipt.file_name,
            created_at=datetime.now(timezone.utc) - timedelta(days=200),
            state="pending",
        )
        pending.storage_ref = {
            **receipt.storage_ref,
            "object_key": storage.key_for(pending.project_id, pending.id),
        }
        db_session.add(pending)
        await db_session.commit()
        identifier = pending.id
        objects.objects[
            pending.storage_ref["bucket"], pending.storage_ref["object_key"]
        ] = b"image\x00bytes"
    else:
        # Project CASCADE and test-only direct deletion can leave ready orphans.
        await db_session.execute(
            delete(Reference).where(Reference.upload_id == identifier)
        )
        await db_session.commit()
    first = await uploads.cleanup(db_session, identifiers=[identifier])
    assert first["objects"] == 0 and not objects.deleted
    current = await db_session.get(Upload, identifier, populate_existing=True)
    assert current.state == ("retired" if state == "ready" else "pending")
    second = await uploads.cleanup(db_session, identifiers=[identifier])
    assert second["objects"] == (1 if state == "ready" else 0)
    assert bool(objects.deleted) is (state == "ready")
    await db_session.refresh(project)
    assert project.guide_assets == [value]  # JSON is not a keep-alive reference.


async def test_last_reference_retirement_delete_failure_retains_saved_receipt_for_retry(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    row = await db_session.get(Upload, identifier)
    receipt = storage.snapshot(row)
    assert (
        await unlink(httpx_client, project_admin, project_id, value["key"])
    ).status_code == 200
    objects.fail_delete = True
    assert (await uploads.cleanup(db_session, identifiers=[identifier]))["failed"] == 1
    current = await db_session.get(Upload, identifier, populate_existing=True)
    first_retired = current.retired_at
    assert current.state == "retired" and storage.same(current, receipt)
    objects.fail_delete = False
    assert (await uploads.cleanup(db_session, identifiers=[identifier]))["objects"] == 1
    assert await db_session.get(Upload, identifier) is None
    assert first_retired is not None
    assert (await uploads.cleanup(db_session, identifiers=[identifier]))["objects"] == 0


@pytest.mark.parametrize("purpose", ["read", "write", "delete"])
async def test_registered_operation_blocks_retired_physical_cleanup_until_settled(
    httpx_client, project_admin, project, objects, db_session, purpose
):
    value = await upload(httpx_client, project_admin, project)
    identifier = uuid.UUID(value["key"].split("/")[-1])
    row = await db_session.get(Upload, identifier)
    location = uuid.UUID(row.storage_ref["location_id"])
    permit = await storage_access.acquire(
        db_session, location, uploads.purpose(purpose, identifier)
    )
    try:
        assert (
            await unlink(httpx_client, project_admin, project, value["key"])
        ).status_code == 200
        assert (await uploads.cleanup(db_session, identifiers=[identifier]))[
            "objects"
        ] == 0
        assert not objects.deleted
    finally:
        await permit.__aexit__(None, None, None)
    assert (await uploads.cleanup(db_session, identifiers=[identifier]))["objects"] == 1


async def test_held_real_read_allows_unlink_but_prevents_physical_delete_until_close(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Body(io.BytesIO):
        def read(self, size=-1):
            entered.set()
            assert release.wait(10)
            return super().read(size)

        def close(self):
            super().close()
            closed.set()

    objects.body_factory = Body
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + project_admin[1]).encode())],
        }
    )
    task = asyncio.create_task(
        uploads.read(db_session, project_admin[0], request, project_id, value["key"])
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not db_session.in_transaction()
        assert (
            await unlink(httpx_client, project_admin, project_id, value["key"])
        ).status_code == 200
        assert (await uploads.cleanup(db_session, identifiers=[identifier]))[
            "objects"
        ] == 0
        assert not objects.deleted and not closed.is_set()
        retained = await db_session.get(Upload, identifier, populate_existing=True)
        assert retained.state == "retired"
        await db_session.commit()
    finally:
        release.set()
        with pytest.raises(HTTPException) as rejected:
            await task
        assert rejected.value.status_code == 404 and closed.is_set()
    assert (await uploads.cleanup(db_session, identifiers=[identifier]))["objects"] == 1


async def test_unknown_physical_close_preserves_permit_and_independent_owner(
    httpx_client, project_admin, project, objects, db_session
):
    value = await upload(httpx_client, project_admin, project)
    project_id = project.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    row = await db_session.get(Upload, identifier)
    location = uuid.UUID(row.storage_ref["location_id"])

    class Body(io.BytesIO):
        def close(self):
            raise OSError("private physical close uncertainty")

    objects.body_factory = Body
    try:
        result = await content(httpx_client, project_admin, project_id, value["key"])
        assert result.status_code == 503 and "private physical" not in result.text
        assert (
            await unlink(httpx_client, project_admin, project_id, value["key"])
        ).status_code == 200
        assert (await uploads.cleanup(db_session, identifiers=[identifier]))[
            "objects"
        ] == 0
        assert not objects.deleted
    finally:
        # The fixture explicitly closes its own handles before exact ledger removal.
        for body in objects.bodies:
            io.BytesIO.close(body)
        async with storage_access.engine(db_session).begin() as connection:
            statement = select(StorageOperation.id).where(
                StorageOperation.location_id == location,
                StorageOperation.purpose == uploads.purpose("read", identifier),
            )
            identifiers = list(await connection.scalars(statement))
            assert identifiers
            await connection.execute(
                delete(StorageOperation).where(StorageOperation.id.in_(identifiers))
            )
    assert (await uploads.cleanup(db_session, identifiers=[identifier]))["objects"] == 1


@pytest.mark.parametrize("change", ["metadata", "add", "duplicate", "stale"])
async def test_reconcile_rejects_injection_metadata_rewrite_duplicate_and_stale_revision(
    httpx_client, project_admin, project, objects, db_session, change
):
    first = await upload(
        httpx_client, project_admin, project, name="first.png", data=b"x"
    )
    second = await upload(httpx_client, project_admin, project, name="second.png")
    project_id = project.id
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [project_id], write_ids=[project_id]
        )
    )[project_id]
    revision = current.guide_assets_revision
    assets = [deepcopy(first), deepcopy(second)]
    if change == "metadata":
        assets[0]["size"] = True  # strict JSON identity rejects bool/int coercion.
    elif change == "add":
        assets.append({**first, "key": f"projects/{uuid.uuid4()}/guide/{uuid.uuid4()}"})
    elif change == "duplicate":
        assets.append(deepcopy(first))
    else:
        revision -= 1
    with pytest.raises(HTTPException) as raised:
        await references.reconcile_assets(db_session, current, revision, assets)
    assert raised.value.status_code == 409
    await db_session.refresh(current)
    assert current.guide_assets == [first, second]
    assert not objects.deleted


async def test_reconcile_reorder_counts_once_and_only_removed_last_reference_retires(
    httpx_client, project_admin, project, objects, db_session
):
    first = await upload(httpx_client, project_admin, project, name="first.png")
    second = await upload(httpx_client, project_admin, project, name="second.png")
    project_id = project.id
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [project_id], write_ids=[project_id]
        )
    )[project_id]
    revision = current.guide_assets_revision
    await references.reconcile_assets(db_session, current, revision, [second, first])
    await db_session.commit()
    await db_session.refresh(current)
    assert (
        current.guide_assets == [second, first]
        and current.guide_assets_revision == revision + 1
    )
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [project_id], write_ids=[project_id]
        )
    )[project_id]
    await references.reconcile_assets(
        db_session, current, current.guide_assets_revision, [second]
    )
    await db_session.commit()
    rows = {
        row.id: row
        for row in await db_session.scalars(
            select(Upload).where(Upload.project_id == project_id)
        )
    }
    assert rows[uuid.UUID(first["key"].split("/")[-1])].state == "retired"
    assert rows[uuid.UUID(second["key"].split("/")[-1])].state == "ready"
    assert not objects.deleted


async def test_project_retirement_select_unlinks_only_its_refs_without_retiring_shared_owner(
    httpx_client, project_admin, super_admin, project, objects, db_session
):
    from app.db.models.project import Project
    from tests.factory import create_project

    value = await upload(httpx_client, project_admin, project)
    source_id = project.id
    original_user = project_admin[0]
    target = await create_project(db_session, owner_id=super_admin[0].id)
    target_id = target.id
    current = await uploads.lock_projects(
        db_session, super_admin[0], [source_id, target_id], write_ids=[target_id]
    )
    await references.copy_references(db_session, current[source_id], current[target_id])
    await db_session.commit()
    identifier = uuid.UUID(value["key"].split("/")[-1])
    await uploads.lock_projects(
        db_session, super_admin[0], [source_id], write_ids=[source_id]
    )
    retired = await references.retire_for_projects(
        db_session, select(Project.id).where(Project.id == source_id)
    )
    assert retired == []
    await db_session.delete(project)
    await db_session.flush()
    await db_session.delete(original_user)
    await db_session.commit()
    row = await db_session.get(Upload, identifier, populate_existing=True)
    assert row.state == "ready" and row.project_id == source_id
    assert (
        await content(httpx_client, super_admin, target_id, value["key"])
    ).status_code == 200
    assert not objects.deleted


async def test_unknown_write_close_keeps_pending_owner_and_registered_responsibility(
    httpx_client, project_admin, project, objects, db_session
):
    from tests.test_guide_assets import endpoint

    project_id = project.id

    class Body(io.BytesIO):
        def close(self):
            raise OSError("private uncertain write verification close")

    objects.body_factory = Body
    identifier = location = None
    try:
        result = await httpx_client.post(
            endpoint(project_id, "upload"),
            params={"file_name": "x.png"},
            content=b"x",
            headers={**bearer(project_admin[1]), "Content-Type": "image/png"},
        )
        assert result.status_code == 503
        row = await db_session.scalar(
            select(Upload).where(Upload.project_id == project_id)
        )
        identifier = row.id
        location = uuid.UUID(row.storage_ref["location_id"])
        assert row.state == "pending" and len(objects.puts) == 1
        assert await db_session.get(Reference, (project_id, identifier)) is None
        result = await uploads.cleanup(db_session, identifiers=[identifier])
        assert result["objects"] == 0 and not objects.deleted
        async with storage_access.engine(db_session).connect() as connection:
            assert (
                await connection.scalar(
                    select(StorageOperation.id).where(
                        StorageOperation.location_id == location,
                        StorageOperation.purpose
                        == uploads.purpose("write", identifier),
                    )
                )
                is not None
            )
    finally:
        for body in objects.bodies:
            io.BytesIO.close(body)
        if identifier is not None:
            async with storage_access.engine(db_session).begin() as connection:
                await connection.execute(
                    delete(StorageOperation).where(
                        StorageOperation.location_id == location,
                        StorageOperation.purpose
                        == uploads.purpose("write", identifier),
                    )
                )


def historical_entries(project_id):
    key = f"projects/{project_id}/guide/{uuid.uuid4()}-old.png"
    first = {
        "key": key,
        "original_name": "old.png",
        "content_type": "image/png",
        "size": 3,
        "uploaded_at": "2026-01-01T00:00:00+00:00",
    }
    variant = {
        **first,
        "original_name": "changed-at-complete.png",
        "content_type": "image/jpeg",
        "size": 4,
    }
    return key, [
        first,
        variant,
        deepcopy(first),
        {"historical": "dictionary without a key"},
    ]


async def test_delete_removes_all_historical_same_key_variants_without_owner_or_provider_io(
    httpx_client, project_admin, project, objects, db_session
):
    project_id = project.id
    key, assets = historical_entries(project_id)
    project.guide_assets = deepcopy(assets)
    await db_session.commit()
    response = await unlink(httpx_client, project_admin, project_id, key)
    assert response.status_code == 200, response.text
    await db_session.refresh(project)
    assert project.guide_assets == [assets[-1]]
    assert await db_session.scalar(select(Upload.id)) is None
    assert await db_session.scalar(select(Reference.upload_id)) is None
    assert not objects.puts and not objects.reads and not objects.deleted


async def test_copy_preserves_exact_legacy_duplicate_order_and_no_key_dict_without_authority(
    project_admin, project, objects, db_session
):
    from tests.factory import create_project

    source_id = project.id
    _, assets = historical_entries(source_id)
    project.guide_assets = deepcopy(assets)
    target = await create_project(db_session, owner_id=project_admin[0].id)
    target_id = target.id
    await db_session.commit()
    current = await uploads.lock_projects(
        db_session, project_admin[0], [source_id, target_id], write_ids=[target_id]
    )
    copied = await references.copy_references(
        db_session, current[source_id], current[target_id]
    )
    await db_session.commit()
    await db_session.refresh(target)
    assert copied == assets and target.guide_assets == assets
    assert await db_session.scalar(select(Upload.id)) is None
    assert await db_session.scalar(select(Reference.upload_id)) is None
    assert not objects.puts and not objects.reads and not objects.deleted


@pytest.mark.parametrize("variant", [0, 1])
async def test_reconcile_can_retain_any_exact_historical_key_variant_then_clear(
    project_admin, project, objects, db_session, variant
):
    project_id = project.id
    _, assets = historical_entries(project_id)
    project.guide_assets = deepcopy(assets)
    await db_session.commit()
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [project_id], write_ids=[project_id]
        )
    )[project_id]
    kept = deepcopy(assets[variant])
    await references.reconcile_assets(
        db_session, current, current.guide_assets_revision, [kept]
    )
    await db_session.commit()
    await db_session.refresh(current)
    assert current.guide_assets == [kept]
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [project_id], write_ids=[project_id]
        )
    )[project_id]
    with pytest.raises(HTTPException) as rejected:
        await references.reconcile_assets(
            db_session, current, current.guide_assets_revision, [{**kept, "size": 999}]
        )
    assert rejected.value.status_code == 409
    await references.reconcile_assets(
        db_session, current, current.guide_assets_revision, []
    )
    await db_session.commit()
    await db_session.refresh(current)
    assert current.guide_assets == []
    assert await db_session.scalar(select(Upload.id)) is None
    assert await db_session.scalar(select(Reference.upload_id)) is None
    assert not objects.puts and not objects.reads and not objects.deleted


@pytest.mark.parametrize(
    "malformed", [{"historical": "without key"}, {"key": None, "size": 1}]
)
async def test_explicit_clear_can_remove_historical_dictionary_without_usable_key(
    project_admin, project, objects, db_session, malformed
):
    project_id = project.id
    project.guide_assets = [malformed]
    await db_session.commit()
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [project_id], write_ids=[project_id]
        )
    )[project_id]
    await references.reconcile_assets(
        db_session, current, current.guide_assets_revision, []
    )
    await db_session.commit()
    await db_session.refresh(current)
    assert current.guide_assets == []
    assert await db_session.scalar(select(Upload.id)) is None
    assert await db_session.scalar(select(Reference.upload_id)) is None
    assert not objects.puts and not objects.reads and not objects.deleted


async def test_known_reference_copy_requires_unique_owner_entry_but_explicit_remove_repairs_json(
    httpx_client, project_admin, project, objects, db_session
):
    from tests.factory import create_project

    value = await upload(httpx_client, project_admin, project)
    source_id = project.id
    identifier = uuid.UUID(value["key"].split("/")[-1])
    malformed = {**value, "size": 999}
    project.guide_assets = [value, malformed, deepcopy(value)]
    target = await create_project(db_session, owner_id=project_admin[0].id)
    target_id = target.id
    await db_session.commit()
    current = await uploads.lock_projects(
        db_session, project_admin[0], [source_id, target_id], write_ids=[target_id]
    )
    with pytest.raises(HTTPException) as rejected:
        await references.copy_references(
            db_session, current[source_id], current[target_id]
        )
    assert rejected.value.status_code == 409
    assert await db_session.get(Reference, (target_id, identifier)) is None
    await db_session.commit()
    # A unique exact retained owner entry repairs the old list; forged metadata
    # cannot be retained even though it is a historical JSON source fact.
    current = (
        await uploads.lock_projects(
            db_session, project_admin[0], [source_id], write_ids=[source_id]
        )
    )[source_id]
    with pytest.raises(HTTPException):
        await references.reconcile_assets(
            db_session, current, current.guide_assets_revision, [malformed]
        )
    await references.reconcile_assets(
        db_session, current, current.guide_assets_revision, [value]
    )
    await db_session.commit()
    await db_session.refresh(project)
    assert project.guide_assets == [value]
    project.guide_assets = [malformed, deepcopy(malformed)]
    await db_session.commit()
    assert (
        await unlink(httpx_client, project_admin, source_id, value["key"])
    ).status_code == 200
    row = await db_session.get(Upload, identifier, populate_existing=True)
    assert (
        row.state == "retired"
        and await db_session.get(Reference, (source_id, identifier)) is None
    )
    assert not objects.deleted

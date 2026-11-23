from datetime import UTC, datetime
from hashlib import sha256
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.db.models.project import Project
from app.db.models.project_deletion_operation import ProjectDeletionOperation
from app.db.models.project_guide_asset import ProjectGuideAsset
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.user import User
from app.middleware import audit as audit_middleware
from app.services import (
    annotations_storage,
    guide_asset_references,
    guide_asset_storage,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.workspace_configuration import execute_configuration
from tests.factory import create_project, create_user
from tests.project_guide_asset_owner_helpers import GUIDE_FILE
from tests.test_labeling_counter_deltas_workflows_native import _configure_actual_api
from tests.test_project_orphan_cleanup_transactions import _asgi_client_for_maker
from tests.test_workspace_project_authority_locking import _api_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable verifier and the deployed Workspace API LOGIN",
    ),
]
_PROJECTS = "/api/v1/projects"


def _headers(actor_id):
    return {
        "Authorization": f"Bearer {create_access_token(str(actor_id), 'project_admin')}"
    }


def _guide_facts(project):
    return {
        field: project[field]
        for field in (
            "owner_id",
            "annotation_guide",
            "guide_assets",
            "guide_assets_revision",
        )
    }


async def _references(db, upload_id):
    return set(
        await db.scalars(
            select(ProjectGuideAsset.project_id).where(
                ProjectGuideAsset.upload_id == upload_id
            )
        )
    )


async def test_api_login_clones_published_guide_and_retires_last_reference(
    test_engine, app_module, monkeypatch
):
    api_url = _api_url(test_engine)
    assert api_url.host == "127.0.0.1" and not api_url.query
    _configure_actual_api(test_engine, monkeypatch)

    owner_db = async_sessionmaker(test_engine, expire_on_commit=False)
    upload_id = uuid4()
    filename = "共享 引用%示例.png"
    async with owner_db.begin() as db:
        actor = await create_user(
            db, "project_admin", f"guide-{upload_id}@example.test", "Guide owner"
        )
        outsider = await create_user(
            db, "project_admin", f"foreign-{upload_id}@example.test", "Foreign manager"
        )
        source = await create_project(
            db, owner_id=actor.id, name=f"Guide source {upload_id}"
        )
        actor_id, outsider_id, source_id = actor.id, outsider.id, source.id
        key = guide_asset_storage.key_for(source_id, upload_id)
        markdown = f"# 图片归属验收\n\n原始字节与独立引用。\n\n![{filename}](guide-asset:{key})"
        source.annotation_guide = markdown
        initial_revision = source.guide_assets_revision
        created_gate = await db.scalar(
            insert(StorageMaintenanceGate)
            .values(location_id=LEGACY_LOCATION_ID)
            .on_conflict_do_nothing()
            .returning(StorageMaintenanceGate.location_id)
        )
        gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
        assert gate is not None and gate.state == "active"
        pending = GuideAssetUpload(
            id=upload_id,
            owner_id=actor_id,
            project_id=source_id,
            storage_ref=annotations_storage.legacy_ref(settings.minio_bucket, key),
            sha256=sha256(GUIDE_FILE).hexdigest(),
            size_bytes=len(GUIDE_FILE),
            mime_type="image/png",
            file_name=filename,
            created_at=datetime.now(UTC),
            state="pending",
        )
        db.add(pending)
        await db.flush()
        entry = guide_asset_storage.entry(pending)
        await guide_asset_references.publish(
            db,
            actor_id=actor_id,
            project_id=source_id,
            receipt=guide_asset_storage.snapshot(pending),
            entry=entry,
            maintenance_revision=gate.revision,
        )

    source_facts = {
        "owner_id": str(actor_id),
        "annotation_guide": markdown,
        "guide_assets": [entry],
        "guide_assets_revision": initial_revision + 1,
    }
    headers = _headers(actor_id)
    source_url = f"{_PROJECTS}/{source_id}"
    clone_body = {
        "type_label": "图像-检测",
        "data_type": "image",
        "source_project_id": str(source_id),
        "copy_annotation_guide": True,
    }
    api_engine = create_async_engine(api_url, hide_parameters=True)
    api_db = async_sessionmaker(api_engine, expire_on_commit=False)
    monkeypatch.setattr(audit_middleware, "async_session", api_db)
    try:
        async with api_db() as db:
            login = (
                await db.execute(
                    text(
                        "SELECT current_user, session_user, oid::bigint "
                        "FROM pg_catalog.pg_database WHERE datname=current_database()"
                    )
                )
            ).one()
            assert login == (
                "identity_api_consumer",
                "identity_api_consumer",
                int(os.environ["AAP_MODULE_DATA_PG_DATABASE_OID"]),
            )
            for statement in (
                "SELECT guide_assets FROM public.projects LIMIT 1",
                "SELECT upload_id FROM public.project_guide_assets LIMIT 1",
                "SELECT storage_ref FROM public.guide_asset_uploads LIMIT 1",
                "UPDATE public.guide_asset_uploads SET state = state WHERE false",
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(text(statement))
                assert denied.value.orig.sqlstate == "42501"
            snapshot = await guide_asset_references.clone_snapshot(db, source_id)
            assert snapshot == {**source_facts, "upload_ids": [str(upload_id)]}
            staged = await execute_configuration(
                db,
                operation="create_project",
                actor_id=actor_id,
                payload={
                    **clone_body,
                    "name": f"Rolled back guide {upload_id}",
                    "source_guide_snapshot": snapshot,
                },
            )
            for project_id in (source_id, staged.resource_id):
                mounted = await guide_asset_references.clone_snapshot(db, project_id)
                assert mounted["upload_ids"] == [str(upload_id)]
            await db.rollback()
        async with owner_db() as observer:
            assert await observer.get(Project, staged.resource_id) is None
            assert await _references(observer, upload_id) == {source_id}

        async with _asgi_client_for_maker(app_module, api_db) as client:
            rejected = await client.post(
                _PROJECTS,
                headers=_headers(outsider_id),
                json={**clone_body, "name": f"Foreign clone {upload_id}"},
            )
            assert rejected.status_code == 404, rejected.text
            assert rejected.json() == {"detail": "项目不存在"}
            async with api_db() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT id FROM yoao_workspace_v1.project_facts WHERE owner_id=:actor"
                        ),
                        {"actor": outsider_id},
                    )
                    is None
                )
            async with owner_db() as db:
                assert await _references(db, upload_id) == {source_id}

            target_ids = []
            for suffix in ("target", "sibling"):
                created = await client.post(
                    _PROJECTS,
                    headers=headers,
                    json={**clone_body, "name": f"Guide {suffix} {upload_id}"},
                )
                assert created.status_code == 200, created.text
                project = created.json()
                assert (project["type_key"], project["data_type"]) == (
                    "image-det",
                    "image",
                )
                assert _guide_facts(project) == source_facts
                target_ids.append(UUID(project["id"]))
            async with owner_db() as db:
                assert await _references(db, upload_id) == {source_id, *target_ids}

            removed = await client.delete(source_url, headers=headers)
            assert removed.status_code == 204, removed.text
            missing = await client.get(source_url, headers=headers)
            assert missing.status_code == 404, missing.text
            async with owner_db() as db:
                assert await _references(db, upload_id) == set(target_ids)
                upload = await db.get(GuideAssetUpload, upload_id)
                assert (
                    upload.owner_id,
                    upload.project_id,
                    upload.state,
                    upload.retired_at,
                ) == (
                    actor_id,
                    source_id,
                    "ready",
                    None,
                )

            for index, target_id in enumerate(target_ids):
                target_url = f"{_PROJECTS}/{target_id}"
                retained = await client.get(target_url, headers=headers)
                assert retained.status_code == 200, retained.text
                assert _guide_facts(retained.json()) == source_facts
                unlinked = await client.delete(
                    f"{target_url}/guide-assets", headers=headers, params={"key": key}
                )
                assert unlinked.status_code == 200, unlinked.text
                assert unlinked.json() == {"deleted": key}
                emptied = await client.get(target_url, headers=headers)
                assert emptied.status_code == 200, emptied.text
                assert _guide_facts(emptied.json()) == {
                    **source_facts,
                    "guide_assets": [],
                    "guide_assets_revision": source_facts["guide_assets_revision"] + 1,
                }
                remaining = set(target_ids[index + 1 :])
                async with owner_db() as db:
                    assert await _references(db, upload_id) == remaining
                    upload = await db.get(GuideAssetUpload, upload_id)
                    assert upload.state == ("ready" if remaining else "retired")
                    assert (upload.retired_at is None) == bool(remaining)
    finally:
        await api_engine.dispose()
        async with owner_db.begin() as db:
            actors = (actor_id, outsider_id)
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(delete(AuditLog).where(AuditLog.actor_id.in_(actors)))
            await db.execute(
                delete(ProjectDeletionOperation).where(
                    ProjectDeletionOperation.actor_id == actor_id,
                    ProjectDeletionOperation.project_id == source_id,
                )
            )
            await db.execute(delete(Project).where(Project.owner_id.in_(actors)))
            await db.execute(
                delete(GuideAssetUpload).where(GuideAssetUpload.id == upload_id)
            )
            await db.execute(delete(User).where(User.id.in_((actor_id, outsider_id))))
            if created_gate is not None:
                await db.execute(
                    delete(StorageMaintenanceGate).where(
                        StorageMaintenanceGate.location_id == created_gate
                    )
                )

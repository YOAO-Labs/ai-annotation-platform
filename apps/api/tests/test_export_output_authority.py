"""Export requests retain their original, currently accepted authority."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import delete, update
from starlette.requests import Request

from app.core.security import create_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.task import Task
from app.services import async_job as jobs, identity_request_authority
from app.services.exporting import output_authority as authority
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import get_identity_gateway
from tests.factory import create_batch
from tests.mask_repair_publication import bind_publication_login
from tests.test_reference_original_cache import referenced_cache as referenced_cache


@pytest.fixture
async def world(referenced_cache, test_engine, monkeypatch):
    """Committed Identity and reference owners visible to independent sessions."""
    owner = referenced_cache
    token = "ak_" + uuid.uuid4().hex
    key = ApiKey(
        user_id=owner.manager.id,
        name="export-authority",
        key_prefix=token[:12],
        key_hash=hash_password(token),
        scopes=["tasks:read"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    async with owner.factory() as db:
        db.add(key)
        await db.commit()
        key_id = key.id
        principal = await get_identity_gateway().authenticate(token, db=db)
        await db.commit()
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + token).encode())],
        }
    )
    request.state.api_key_id = str(key_id)
    request.state.api_key_scopes = list(principal.scopes)
    request.state.identity_principal = principal
    async with bind_publication_login(test_engine, monkeypatch):
        yield SimpleNamespace(
            owner=owner,
            actor=owner.manager,
            project=owner.project,
            key_id=key_id,
            token=token,
            request=request,
        )


async def job_for(world):
    async with world.owner.factory() as db:
        proof = await authority.capture(db, world.project, world.actor, world.request)
        job = await jobs.create_job(
            db,
            kind="export",
            user_id=world.actor.id,
            project_id=world.project.id,
            payload={"output_authority": proof},
        )
        await db.commit()
        return job.id


@pytest.mark.parametrize(
    "change", ["rotate", "scopes", "expiry", "revoke", "owner", "delete"]
)
async def test_key_changes_after_queueing_cannot_publish_output(world, change):
    job_id = await job_for(world)
    async with world.owner.factory() as owner:
        if change == "delete":
            await owner.execute(delete(ApiKey).where(ApiKey.id == world.key_id))
        else:
            values = {
                "rotate": {"key_hash": hash_password("ak_" + uuid.uuid4().hex)},
                "scopes": {"scopes": ["*"]},
                "expiry": {
                    "expires_at": datetime.now(timezone.utc) + timedelta(days=2)
                },
                "revoke": {"revoked_at": datetime.now(timezone.utc)},
                "owner": {"user_id": world.owner.stranger.id},
            }[change]
            await owner.execute(
                update(ApiKey).where(ApiKey.id == world.key_id).values(**values)
            )
        await owner.commit()
    async with world.owner.factory() as observer:
        job = await jobs.get_job(observer, job_id)
        with pytest.raises(authority.ExportOutputAuthorityError):
            await authority.validate(observer, job, lock=True)


@pytest.mark.parametrize(
    "change", ["rotate", "scopes", "expired", "expiry_extended", "missing_id"]
)
async def test_capture_rejects_credential_changes_since_authentication(world, change):
    async with world.owner.factory() as owner:
        if change == "missing_id":
            world.request.state.api_key_id = None
        else:
            values = {
                "rotate": {"key_hash": hash_password("ak_" + uuid.uuid4().hex)},
                "scopes": {"scopes": ["*"]},
                "expired": {
                    "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)
                },
                "expiry_extended": {
                    "expires_at": datetime.now(timezone.utc) + timedelta(days=2)
                },
            }[change]
            await owner.execute(
                update(ApiKey).where(ApiKey.id == world.key_id).values(**values)
            )
            await owner.commit()
    async with world.owner.factory() as caller:
        with pytest.raises(HTTPException) as caught:
            await authority.capture(caller, world.project, world.actor, world.request)
        assert caught.value.status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 1.0),
        ("project_id", str(uuid.UUID(int=1))),
        ("actor_id", str(uuid.UUID(int=2))),
        ("extra", "unrecognized"),
        ("api_key_id", True),
    ],
)
async def test_malformed_or_foreign_proof_is_not_a_new_authority(world, field, value):
    job_id = await job_for(world)
    async with world.owner.factory() as observer:
        job = await jobs.get_job(observer, job_id)
        job.payload = deepcopy(job.payload)
        job.payload["output_authority"][field] = value
        with pytest.raises(authority.ExportOutputAuthorityError):
            await authority.validate(observer, job)


async def test_legacy_job_does_not_acquire_a_new_proof_on_execution(world):
    async with world.owner.factory() as caller:
        job = await jobs.create_job(
            caller,
            kind="export",
            user_id=world.actor.id,
            project_id=world.project.id,
            payload={},
        )
        with pytest.raises(authority.ExportOutputAuthorityError) as caught:
            await authority.validate(caller, job)
        assert caught.value.code == "export_output_authority_missing"
        assert job.payload == {}


async def test_identity_unavailable_is_not_reported_as_revoked(world, monkeypatch):
    class UnavailableGateway:
        async def authenticate(self, *_args, **_kwargs):
            raise IdentityGatewayUnavailable()

    monkeypatch.setattr(
        identity_request_authority, "get_identity_gateway", lambda: UnavailableGateway()
    )
    async with world.owner.factory() as caller:
        with pytest.raises(HTTPException) as caught:
            await authority.capture(caller, world.project, world.actor, world.request)
        assert caught.value.status_code == 503


@pytest.mark.parametrize("scope", ["project", "batch", "selected"])
@pytest.mark.parametrize("credential", ["jwt", "key"])
async def test_all_new_http_export_entries_preserve_and_hide_original_authority(
    world, app_module, monkeypatch, scope, credential
):
    from app.deps import get_db
    from app.workers.celery_app import celery_app

    async with world.owner.factory() as setup:
        batch = await create_batch(setup, project_id=world.project.id, status="active")
        task = await setup.get(Task, world.owner.task.id)
        task.batch_id = batch.id
        await setup.commit()
        batch_id = batch.id
    if credential == "key":
        async with world.owner.factory() as setup:
            await setup.execute(
                update(ApiKey).where(ApiKey.id == world.key_id).values(scopes=["*"])
            )
            await setup.commit()
        token = world.token
    else:
        token = create_access_token(subject=str(world.actor.id), role="super_admin")

    async def database():
        async with world.owner.factory() as db:
            yield db

    monkeypatch.setitem(app_module.dependency_overrides, get_db, database)
    monkeypatch.setattr(celery_app, "send_task", Mock())
    endpoint = f"/api/v1/projects/{world.project.id}"
    body = None
    if scope == "project":
        endpoint += "/export?targets=native_json"
    elif scope == "batch":
        endpoint += f"/batches/{batch_id}/export?targets=native_json"
    else:
        endpoint += "/data-manager/tasks/export"
        body = {"task_ids": [str(world.owner.task.id)], "targets": ["native_json"]}
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": uuid.uuid4().hex}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module), base_url="http://test"
    ) as client:
        response = await client.post(endpoint, headers=headers, json=body)
        assert response.status_code == 202, response.text
        job_id = uuid.UUID(response.json()["job_id"])
        async with world.owner.factory() as observer:
            job = await jobs.get_job(observer, job_id)
            original_proof = deepcopy(job.payload["output_authority"])
            assert original_proof["actor_id"] == str(world.actor.id)
            assert original_proof["project_id"] == str(world.project.id)
            assert original_proof["api_key_id"] == (
                str(world.key_id) if credential == "key" else None
            )
            await authority.validate(observer, job, lock=True)
        public = await client.get(f"/api/v1/async-jobs/{job_id}", headers=headers)
        assert public.status_code == 200, public.text
        payload = public.json()["payload"]
        assert not (
            {"output_authority", "image_media", "export_authority"} & payload.keys()
        )
        assert "key_proof" not in public.text
        if scope == "selected" and credential == "key":
            replacement = token[:12] + uuid.uuid4().hex
            async with world.owner.factory() as owner:
                await owner.execute(
                    update(ApiKey)
                    .where(ApiKey.id == world.key_id)
                    .values(key_hash=hash_password(replacement))
                )
                await owner.commit()
            headers["Authorization"] = "Bearer " + replacement
            retried = await client.post(endpoint, headers=headers, json=body)
            assert retried.status_code == 202, retried.text
            assert retried.json()["job_id"] == str(job_id)
            async with world.owner.factory() as observer:
                job = await jobs.get_job(observer, job_id)
                assert job.payload["output_authority"] == original_proof
                with pytest.raises(authority.ExportOutputAuthorityError):
                    await authority.validate(observer, job)

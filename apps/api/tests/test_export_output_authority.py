"""Output authorization stays bound to the original export request credential."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from app.core.security import hash_password
from app.services.exporting import output_authority as authority


@pytest.fixture
def world():
    actor, project = SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())
    plaintext = "ak_output-authorization-fixture"
    key = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=actor.id,
        key_hash=hash_password(plaintext),
        scopes=["annotations:read"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        revoked_at=None,
    )
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + plaintext).encode())],
        }
    )
    request.state.api_key_id = str(key.id)
    request.state.api_key_scopes = list(key.scopes)
    db = SimpleNamespace(scalar=AsyncMock(return_value=key))
    return db, actor, project, key, request


async def job_for(world):
    db, actor, project, _, request = world
    return SimpleNamespace(
        user_id=actor.id,
        project_id=project.id,
        payload={
            "output_authority": await authority.capture(db, project, actor, request)
        },
    )


async def test_original_key_can_keep_existing_scopes_and_hold_final_share_lock(world):
    db, *_ = world
    job = await job_for(world)
    await authority.validate(db, job, lock=True)
    statement = db.scalar.await_args.args[0]
    assert statement._for_update_arg.read


@pytest.mark.parametrize(
    "change", ["rotate", "scopes", "expiry", "revoke", "owner", "delete"]
)
async def test_key_changes_after_queueing_cannot_publish_output(world, change):
    db, _, _, key, _ = world
    job = await job_for(world)
    if change == "rotate":
        key.key_hash = hash_password("different-key")
    elif change == "scopes":
        key.scopes = ["*"]
    elif change == "expiry":
        key.expires_at += timedelta(days=1)
    elif change == "revoke":
        key.revoked_at = datetime.now(timezone.utc)
    elif change == "owner":
        key.user_id = uuid.uuid4()
    else:
        db.scalar.return_value = None
    with pytest.raises(authority.ExportOutputAuthorityError):
        await authority.validate(db, job)


@pytest.mark.parametrize("change", ["rotate", "scopes", "expired", "missing_id"])
async def test_capture_rejects_credential_changes_since_authentication(world, change):
    db, actor, project, key, request = world
    if change == "rotate":
        key.key_hash = hash_password("replacement")
    elif change == "scopes":
        key.scopes = ["*"]
    elif change == "expired":
        key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        request.state.api_key_id = None
    with pytest.raises(HTTPException) as caught:
        await authority.capture(db, project, actor, request)
    assert caught.value.status_code == 403


async def test_jwt_proof_is_explicit_and_needs_no_key_lookup(world):
    db, actor, project, _, request = world
    request.state.api_key_id = None
    request.state.api_key_scopes = None
    proof = await authority.capture(db, project, actor, request)
    assert proof == {
        "schema": 1,
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "api_key_id": None,
        "key_proof": None,
    }
    await authority.validate(
        db,
        SimpleNamespace(
            user_id=actor.id, project_id=project.id, payload={"output_authority": proof}
        ),
    )
    db.scalar.assert_not_awaited()


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
    db, *_ = world
    job = await job_for(world)
    job.payload["output_authority"][field] = value
    with pytest.raises(authority.ExportOutputAuthorityError):
        await authority.validate(db, job)


async def test_legacy_job_does_not_acquire_a_new_proof_on_execution(world):
    db, actor, project, *_ = world
    job = SimpleNamespace(user_id=actor.id, project_id=project.id, payload={})
    with pytest.raises(authority.ExportOutputAuthorityError) as caught:
        await authority.validate(db, job)
    assert caught.value.code == "export_output_authority_missing"
    assert job.payload == {}


def test_public_job_payload_hides_output_key_proof_without_mutating_it():
    from app.schemas.async_job import AsyncJobOut

    private = {
        "output_authority": {"key_proof": {"hash": "private-hash"}},
        "targets": ["coco"],
    }
    before = deepcopy(private)
    assert AsyncJobOut.public_payload(private) == {"targets": ["coco"]}
    assert private == before


@pytest.mark.parametrize("scope", ["project", "batch", "selected"])
@pytest.mark.parametrize("credential", ["jwt", "key"])
async def test_all_new_http_export_entries_preserve_and_hide_original_authority(
    httpx_client, db_session, super_admin, monkeypatch, scope, credential
):
    from app.db.models.api_key import ApiKey
    from app.db.models.async_job import AsyncJob
    from app.workers.export import run_export
    from tests.factory import create_batch, create_project, create_task
    from unittest.mock import Mock

    actor, token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id)
    task.batch_id = batch.id
    key = None
    if credential == "key":
        token = "ak_" + uuid.uuid4().hex
        key = ApiKey(
            user_id=actor.id,
            name="output-authority-fixture",
            key_prefix=token[:12],
            key_hash=hash_password(token),
            scopes=["*"],
        )
        db_session.add(key)
    await db_session.commit()
    monkeypatch.setattr(
        run_export, "delay", Mock(return_value=SimpleNamespace(id="fixture"))
    )
    endpoint = f"/api/v1/projects/{project.id}"
    body = None
    if scope == "project":
        endpoint += "/export?targets=aap_json"
    elif scope == "batch":
        endpoint += f"/batches/{batch.id}/export?targets=aap_json"
    else:
        endpoint += "/data-manager/tasks/export"
        body = {"task_ids": [str(task.id)], "targets": ["aap_json"]}
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": uuid.uuid4().hex}
    response = await httpx_client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 202, response.text
    job = await db_session.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    proof = deepcopy(job.payload["output_authority"])
    assert proof["actor_id"] == str(actor.id) and proof["project_id"] == str(project.id)
    assert proof["api_key_id"] == (str(key.id) if key else None)
    assert proof["key_proof"] == (
        {"hash": key.key_hash, "scopes": ["*"], "expires_at": None} if key else None
    )
    await authority.validate(db_session, job, lock=True)
    public = await httpx_client.get(f"/api/v1/async-jobs/{job.id}", headers=headers)
    assert public.status_code == 200, public.text
    assert "output_authority" not in public.json()["payload"]
    assert "key_proof" not in public.text
    if scope == "selected" and key:
        # A request authenticated with a replacement token can republish the
        # original pending job, but cannot replace its original credential proof.
        replacement = token[:12] + uuid.uuid4().hex
        key.key_hash = hash_password(replacement)
        await db_session.commit()
        headers["Authorization"] = "Bearer " + replacement
        retried = await httpx_client.post(endpoint, headers=headers, json=body)
        assert retried.status_code == 202, retried.text
        assert retried.json()["job_id"] == str(job.id)
        await db_session.refresh(job)
        assert job.payload["output_authority"] == proof
        with pytest.raises(authority.ExportOutputAuthorityError):
            await authority.validate(db_session, job)

"""Physical failures retain safe evidence without taking over caller transactions."""

import hashlib
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.audit_log import AuditLog
from app.db.models.storage_fault import StorageFaultState
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_source import DatasetSourceItem
from app.db.models.user import User
from app.services import dataset_content as content, dataset_references as references
from app.services import dataset_scans
from app.services import directory_previews, filesystem_storage as fs
from app.services import storage_fault_events as events
from app.services.content_snapshot import ContentError
from app.services.source_readers import s3 as s3_reference
from app.services.source_readers import sftp as sftp_reference
from tests.test_dataset_content import content_world as content_world, headers


async def rows(engine, location_id):
    async with engine.connect() as connection:
        return (
            (
                await connection.execute(
                    select(AuditLog.__table__).where(
                        AuditLog.action == events.ACTION,
                        AuditLog.target_id == str(location_id),
                    )
                )
            )
            .mappings()
            .all()
        )


async def clean(engine, location_id):
    async with engine.begin() as connection:
        await connection.execute(
            delete(StorageFaultState).where(
                StorageFaultState.location_id == location_id
            )
        )
        await connection.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
        await connection.execute(
            delete(AuditLog).where(
                AuditLog.action == events.ACTION, AuditLog.target_id == str(location_id)
            )
        )


@pytest.fixture
async def world(content_world, test_engine):
    location_id = content_world[4].id
    yield content_world
    await clean(test_engine, location_id)


@pytest.mark.parametrize("fault", ["root", "changed"])
async def test_actual_read_fault_survives_error_response_and_caller_rollback(
    world,
    db_session,
    httpx_client,
    super_admin,
    test_engine,
    monkeypatch,
    tmp_path,
    fault,
):
    root, data, _, dataset, location, source, run, entry = world
    location_id, source_id, run_id, job_id = (
        location.id,
        source.id,
        run.id,
        run.async_job_id,
    )
    relative_path = entry.relative_path
    snapshot = await content.scan_snapshot(
        db_session, run.id, entry.path_digest, super_admin[0], verify=True
    )
    snapshot.close()
    if fault == "root":
        monkeypatch.setattr(
            settings,
            "storage_filesystem_roots",
            {"test": str(tmp_path / "missing-root")},
        )
    else:
        (root / entry.relative_path).write_bytes(data + b"changed")
    request_id = uuid.uuid4().hex
    response = await httpx_client.get(
        f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}/content",
        params={"sha256": hashlib.sha256(data).hexdigest()},
        headers={**headers(super_admin), "X-Request-ID": request_id},
    )
    assert response.status_code == 409
    history = await httpx_client.get(
        "/api/v1/audit-logs",
        params={
            "action": events.ACTION,
            "target_type": "storage_location",
            "detail_key": "source_id",
            "detail_value": str(source_id),
        },
        headers=headers(super_admin),
    )
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["request_id"] == request_id
    await db_session.rollback()
    saved = await rows(test_engine, location_id)
    assert len(saved) == 1
    event = saved[0]
    assert event["request_id"] == request_id
    detail = event["detail_json"]
    assert detail["category"] == (
        "root_unavailable" if fault == "root" else "source_changed"
    )
    assert detail["location_id"] == str(location_id) and detail["source_id"] == str(
        source_id
    )
    assert detail["run_id"] == str(run_id) and detail["job_id"] == str(job_id)
    assert str(root) not in str(detail) and relative_path not in str(detail)


async def test_client_sha_and_unauthorized_request_do_not_create_storage_fault(
    world,
    httpx_client,
    super_admin,
    annotator,
    test_engine,
):
    _, _, _, _, location, _, run, entry = world
    path = f"/api/v1/dataset-scans/{run.id}/entries/{entry.path_digest}/content"
    for actor in (super_admin, annotator):
        result = await httpx_client.get(
            path, params={"sha256": "0" * 64}, headers=headers(actor)
        )
        assert result.status_code in {403, 409}
    assert not await rows(test_engine, location.id)


async def test_observation_failure_keeps_original_exception_and_redacts_logs(
    monkeypatch,
):
    monkeypatch.setattr(
        events,
        "_persist",
        AsyncMock(side_effect=RuntimeError("private-secret /private/root")),
    )
    warning = Mock()
    monkeypatch.setattr(events.log, "warning", warning)
    failure = ContentError("source_changed")
    with pytest.raises(ContentError) as caught:
        async with events.observe(
            None, operation="reference_read", location_id=uuid.uuid4()
        ):
            raise failure
    assert caught.value is failure
    warning.assert_called_once_with("Storage fault observation could not be persisted")
    assert "private-secret" not in str(warning.call_args)


async def test_preview_failure_does_not_discard_verified_original(
    world, db_session, test_engine, monkeypatch
):
    _, data, actor, _, location, source, run, entry = world
    registered = await references.register_entry(
        db_session, run.id, entry.path_digest, actor
    )
    mapping = await db_session.get(DatasetSourceItem, (source.id, entry.path_digest))
    source.cache_policy = "preview"
    snapshot = await content.scan_snapshot(
        db_session, run.id, entry.path_digest, actor, verify=True
    )
    monkeypatch.setattr(
        directory_previews,
        "encode",
        AsyncMock(side_effect=RuntimeError("private encoder path")),
    )
    try:
        preview = await directory_previews.generate(
            db_session,
            location,
            source,
            mapping,
            snapshot,
            [],
            [],
            actor_id=actor.id,
            run_id=run.id,
            job_id=run.async_job_id,
        )
        assert (
            preview.state == "failed"
            and preview.error_code == "preview_generation_failed"
        )
        assert registered["sha256"] == hashlib.sha256(data).hexdigest()
    finally:
        snapshot.close()
    saved = await rows(test_engine, location.id)
    assert len(saved) == 1 and saved[0]["detail_json"]["category"] == "preview_failed"
    assert saved[0]["detail_json"]["item_id"] == str(mapping.dataset_item_id)
    assert saved[0]["detail_json"]["asset_id"] == str(mapping.asset_id)
    assert "private encoder" not in str(saved)


@pytest.mark.parametrize(
    "code", ["AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId"]
)
async def test_actual_provider_rejection_is_classified_without_disclosing_credentials(
    world,
    db_session,
    test_engine,
    monkeypatch,
    code,
):
    location, source, run = world[4:7]
    client = Mock()
    monkeypatch.setattr(s3_reference.boto3, "client", lambda *args, **kwargs: client)
    monkeypatch.setattr(s3_reference, "allowed_endpoint", lambda *args: None)
    monkeypatch.setattr(
        s3_reference,
        "decrypt_secret",
        lambda _: {"access_key": "private", "secret_key": "private"},
    )
    access = s3_reference.S3Access(
        {"endpoint": "http://127.0.0.1:9000", "bucket": "external"}, b"sealed", ()
    )
    with pytest.raises(fs.FilesystemStorageError):
        async with events.observe(
            db_session,
            operation="reference_read",
            location_id=location.id,
            source_id=source.id,
            run_id=run.id,
        ):
            with access.client():
                raise ClientError(
                    {"Error": {"Code": code, "Message": "private-secret"}}, "GetObject"
                )
    saved = await rows(test_engine, location.id)
    assert saved[0]["detail_json"]["category"] == "access_denied"
    assert "private" not in str(saved)
    client.close.assert_called_once()


async def test_missing_source_file_is_not_a_missing_root(world):
    root, _, _, _, location, source, _, entry = world
    (root / entry.relative_path).unlink()
    with pytest.raises(fs.FilesystemStorageError) as caught:
        with fs.content_file(
            location.config,
            source.config["relative_path"],
            entry.relative_path,
            expected_anchor=None,
        ):
            pytest.fail("missing source file must not open")
    assert caught.value.code == "path_unavailable"
    assert events.classify(caught.value) is None


async def test_independent_event_does_not_commit_caller_business(test_engine):
    location_id, actor_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with test_engine.connect() as connection:
            async with AsyncSession(connection, expire_on_commit=False) as db:
                user = User(
                    id=actor_id,
                    email=f"fault-{actor_id}@example.test",
                    name="Pending",
                    password_hash="unused",
                    role="employee",
                )
                db.add(user)
                await db.flush()
                await events.report(
                    db,
                    ContentError("source_changed"),
                    operation="reference_read",
                    location_id=location_id,
                    actor_id=actor_id,
                )
                async with test_engine.connect() as other:
                    assert (
                        await other.scalar(select(User.id).where(User.id == actor_id))
                        is None
                    )
                await db.rollback()
        saved = await rows(test_engine, location_id)
        assert len(saved) == 1
        assert saved[0]["actor_id"] is None
        assert saved[0]["detail_json"]["actor_id"] == str(actor_id)
    finally:
        await clean(test_engine, location_id)


async def test_scan_failure_records_current_job_once_and_rejects_old_delivery(
    world,
    db_session,
    test_engine,
    monkeypatch,
):
    _, _, actor, _, location, source, _, _ = world
    run, _ = await dataset_scans.start_scan(db_session, source.id, actor)
    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await dataset_scans.claim(
        db_session, run.id, job.celery_task_id, "content@test"
    )
    monkeypatch.setattr(settings, "storage_filesystem_roots", {})
    with pytest.raises(fs.FilesystemStorageError) as caught:
        with fs.location_directory(location.config):
            pytest.fail("missing configured root must not open")
    failure = caught.value
    await dataset_scans.fail_attempt(
        db_session, run.id, "old-delivery", fence, failure.code, failure=failure
    )
    assert not await rows(test_engine, location.id)
    await dataset_scans.fail_attempt(
        db_session, run.id, job.celery_task_id, fence, failure.code, failure=failure
    )
    assert run.status == job.status == "failed"
    saved = await rows(test_engine, location.id)
    assert len(saved) == 1
    assert saved[0]["detail_json"]["job_id"] == str(job.id)
    assert saved[0]["detail_json"]["run_id"] == str(run.id)
    await dataset_scans.fail_attempt(
        db_session, run.id, job.celery_task_id, fence, failure.code, failure=failure
    )
    assert len(await rows(test_engine, location.id)) == 1


@pytest.mark.parametrize("failure", ["provider", "decryption"])
def test_sftp_authentication_is_distinct_from_local_secret_failure(
    monkeypatch, failure
):
    import paramiko
    from app.core.crypto import ConnectorCryptoError

    key = paramiko.RSAKey.generate(1024)
    client, connection = Mock(), Mock()
    client.connect.side_effect = paramiko.AuthenticationException(
        "private provider response"
    )
    monkeypatch.setattr(sftp_reference.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(sftp_reference.socket, "socket", lambda *args: connection)
    monkeypatch.setattr(
        sftp_reference.connector_guard,
        "resolve_allowed_ips",
        lambda *args: ["10.1.1.1"],
    )
    decrypt = Mock(return_value={"password": "private"})
    if failure == "decryption":
        decrypt.side_effect = ConnectorCryptoError("private key material")
    monkeypatch.setattr(sftp_reference, "decrypt_secret", decrypt)
    access = sftp_reference.SftpAccess(
        {},
        b"sealed",
        (),
        "test",
        {
            "host": "10.1.1.1",
            "port": 22,
            "username": "reader",
            "host_key": f"{key.get_name()} {key.get_base64()}",
        },
    )
    with pytest.raises(fs.FilesystemStorageError) as caught:
        with access.client():
            pytest.fail("rejected authentication must not open SFTP")
    assert caught.value.code == "connection_unavailable"
    assert events.classify(caught.value) == (
        ("access_denied", "access_denied") if failure == "provider" else None
    )
    assert "private" not in str(caught.value)
    client.close.assert_called_once()

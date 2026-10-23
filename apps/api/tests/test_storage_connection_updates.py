"""Candidate validation fences publication, with independent-transaction races."""

import asyncio
from contextlib import asynccontextmanager
import time
import uuid

import anyio
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
from fastapi import HTTPException
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.crypto import decrypt_secret
from app.db.models.dataset_asset import StorageLocation, StorageBinding
from app.db.models.storage_connection import StorageConnection
from app.db.models.system_setting import SystemSetting
from app.db.models.user import User
from app.services import storage_connection as svc
from app.services.storage_location import connector_location_id
from tests.factory import create_user

OLD = {"access_key": "old-access", "secret_key": "old-secret"}
NEW = {"access_key": "new-access", "secret_key": "new-secret"}
CONFIG = {"endpoint": "http://8.8.8.8:9000", "bucket": "data"}


@pytest.fixture
async def connection(httpx_client, super_admin, monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "connector_host_allowlist", ["8.8.8.8/32"])
    response = await httpx_client.post(
        "/api/v1/storage-connections",
        headers={"Authorization": f"Bearer {super_admin[1]}"},
        json={
            "name": "original",
            "kind": "s3",
            "config": CONFIG,
            "secret": OLD,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["revision"] == 1
    return response.json()["id"], {"Authorization": f"Bearer {super_admin[1]}"}


async def test_revision_required_and_stale_edits_do_not_overwrite(
    httpx_client, connection, db_session, monkeypatch
):
    conn_id, headers = connection

    async def forbidden(*args, **kwargs):
        pytest.fail("name-only/stale edits must not probe")

    monkeypatch.setattr(svc, "_probe", forbidden)
    path = f"/api/v1/storage-connections/{conn_id}"
    assert (
        await httpx_client.patch(path, headers=headers, json={"name": "missing"})
    ).status_code == 422
    first = await httpx_client.patch(
        path, headers=headers, json={"name": "winner", "expected_revision": 1}
    )
    assert first.status_code == 200 and first.json()["revision"] == 2
    # Sending the same config must not invalidate node/scan evidence.
    second = await httpx_client.patch(
        path, headers=headers, json={"config": CONFIG, "expected_revision": 2}
    )
    assert second.status_code == 200 and second.json()["revision"] == 3
    stale = await httpx_client.patch(
        path,
        headers=headers,
        json={"name": "loser", "secret": NEW, "expected_revision": 1},
    )
    assert (
        stale.status_code == 409
        and stale.json()["detail"]["code"] == "connection_revision_conflict"
    )
    current = await httpx_client.get(path, headers=headers)
    assert current.json()["name"] == "winner" and current.json()["revision"] == 3
    location = await db_session.get(
        StorageLocation,
        connector_location_id(uuid.UUID(conn_id)),
        populate_existing=True,
    )
    assert location.config_revision == 1


@pytest.mark.parametrize("failure", ["authentication", "unexpected", "timeout"])
async def test_failed_candidate_keeps_name_config_ciphertext_and_revisions(
    httpx_client, connection, db_session, monkeypatch, failure
):
    conn_id, headers = connection
    before = await db_session.get(StorageConnection, uuid.UUID(conn_id))
    ciphertext = before.secret_enc

    def probe(*args):
        if failure == "authentication":
            raise ClientError(
                {"Error": {"Code": "SignatureDoesNotMatch", "Message": "new-secret"}},
                "ListObjectsV2",
            )
        if failure == "timeout":
            time.sleep(0.08)
            return 0
        raise RuntimeError("new-secret https://signed.invalid/?secret=old-secret")

    monkeypatch.setattr(svc, "_test_blocking", probe)
    monkeypatch.setattr(
        svc, "_PROBE_TIMEOUT_SECONDS", 0.01 if failure == "timeout" else 1
    )
    result = await httpx_client.patch(
        f"/api/v1/storage-connections/{conn_id}",
        headers=headers,
        json={
            "expected_revision": 1,
            "name": "bad",
            "config": {**CONFIG, "bucket": "other"},
            "secret": NEW,
        },
    )
    assert result.status_code == 400, result.text
    assert "原配置未修改" in result.text
    for value in ["new-secret", "old-secret", "signed.invalid"]:
        assert value not in result.text
    await db_session.refresh(before)
    assert (before.name, before.config, before.secret_enc, before.revision) == (
        "original",
        CONFIG,
        ciphertext,
        1,
    )
    assert (
        await db_session.get(StorageLocation, connector_location_id(before.id))
    ).config_revision == 1
    await anyio.sleep(
        0.1
    )  # Wait for this test's abandoned bounded thread before teardown.


async def test_successful_rotation_validates_candidate_without_replacing_old_first(
    httpx_client, connection, db_session, monkeypatch
):
    conn_id, headers = connection
    conn = await db_session.get(StorageConnection, uuid.UUID(conn_id))
    old_cipher = conn.secret_enc

    async def probe(kind, config, secret, **kwargs):
        assert (kind, config, secret) == ("s3", CONFIG, NEW)
        if conn.revision == 1:
            assert conn.secret_enc == old_cipher
        return True, "ok", 0

    monkeypatch.setattr(svc, "_probe", probe)
    response = await httpx_client.patch(
        f"/api/v1/storage-connections/{conn_id}",
        headers=headers,
        json={"expected_revision": 1, "secret": NEW},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    await db_session.refresh(conn)
    assert decrypt_secret(conn.secret_enc) == NEW and conn.secret_enc != old_cipher
    assert (
        await db_session.get(StorageLocation, connector_location_id(conn.id))
    ).config_revision == 2
    tested = await httpx_client.post(
        f"/api/v1/storage-connections/{conn_id}/test", headers=headers
    )
    assert tested.status_code == 200 and tested.json()["ok"]


async def test_existing_test_errors_are_redacted(httpx_client, connection, monkeypatch):
    conn_id, headers = connection

    def fail(*args):
        raise RuntimeError("old-secret https://signed.invalid/?token=secret")

    monkeypatch.setattr(svc, "_test_blocking", fail)
    result = await httpx_client.post(
        f"/api/v1/storage-connections/{conn_id}/test", headers=headers
    )
    assert result.status_code == 200 and not result.json()["ok"]
    assert "old-secret" not in result.text and "signed.invalid" not in result.text


@asynccontextmanager
async def committed_world(test_engine, monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "connector_host_allowlist", ["8.8.8.8/32"])
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        actor = await create_user(
            db, "project_admin", f"cas-{uuid.uuid4()}@test.local", "CAS test"
        )
        conn = await svc.StorageConnectionService.create(
            db,
            name="original",
            kind="s3",
            config=CONFIG,
            secret=OLD,
            scope="owner",
            created_by=actor.id,
        )
        actor_id, conn_id = actor.id, conn.id
        await db.commit()
    try:
        yield maker, actor_id, conn_id
    finally:
        async with maker() as db:
            await db.execute(
                delete(StorageBinding).where(
                    StorageBinding.purpose == f"cas-{conn_id.hex[:20]}"
                )
            )
            await db.execute(
                delete(StorageConnection).where(StorageConnection.id == conn_id)
            )
            await db.execute(delete(User).where(User.id == actor_id))
            await db.execute(
                delete(SystemSetting).where(
                    SystemSetting.key == "connector_host_allowlist"
                )
            )
            await db.commit()


@pytest.mark.parametrize(
    "change, expected_status",
    [
        ("rename", 409),
        ("delete", 404),
        ("disable", 403),
        ("demote", 403),
        ("allowlist", 400),
        ("reference", 409),
    ],
)
async def test_validation_holds_no_row_locks_and_rechecks_publication(
    test_engine, monkeypatch, change, expected_status
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await release.wait()
        return True, "ok", 0

    monkeypatch.setattr(svc, "_probe", probe)
    async with committed_world(test_engine, monkeypatch) as (maker, actor_id, conn_id):

        async def candidate():
            async with maker() as db:
                conn = await db.get(StorageConnection, conn_id)
                try:
                    await svc.StorageConnectionService.update(
                        db,
                        conn,
                        expected_revision=1,
                        actor_id=actor_id,
                        name="candidate",
                        config={**CONFIG, "bucket": "candidate"},
                        secret=NEW,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        task = asyncio.create_task(candidate())
        try:
            await asyncio.wait_for(entered.wait(), 3)

            async def concurrent():
                async with maker() as db:
                    if change in {"rename", "delete"}:
                        conn = await db.get(StorageConnection, conn_id)
                        if change == "rename":
                            await svc.StorageConnectionService.update(
                                db,
                                conn,
                                expected_revision=1,
                                actor_id=actor_id,
                                name="winner",
                                config=None,
                                secret=None,
                            )
                        else:
                            await svc.StorageConnectionService.delete(db, conn)
                    elif change in {"disable", "demote"}:
                        actor = await db.get(User, actor_id)
                        if change == "disable":
                            actor.is_active = False
                        else:
                            actor.role = "employee"
                    elif change == "allowlist":
                        db.add(
                            SystemSetting(
                                key="connector_host_allowlist",
                                value_type="json",
                                value_json=[],
                            )
                        )
                    else:
                        db.add(
                            StorageBinding(
                                purpose=f"cas-{conn_id.hex[:20]}",
                                location_id=connector_location_id(conn_id),
                                bucket="data",
                            )
                        )
                    await db.commit()

            # If validation held a connection/location lock, this would time out.
            await asyncio.wait_for(concurrent(), 3)
            release.set()
            with pytest.raises(
                (HTTPException, svc.connector_guard.ConnectorHostDenied)
            ) as exc:
                await asyncio.wait_for(task, 3)
            status = getattr(exc.value, "status_code", 400)
            assert status == expected_status
            async with maker() as db:
                conn = await db.get(StorageConnection, conn_id)
                if change != "delete":
                    assert conn.name == ("winner" if change == "rename" else "original")
                    assert (
                        conn.config == CONFIG and decrypt_secret(conn.secret_enc) == OLD
                    )
                    assert conn.revision == (2 if change == "rename" else 1)
                    location = await db.get(
                        StorageLocation, connector_location_id(conn_id)
                    )
                    assert location.config_revision == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def test_s3_probe_bounds_and_closes_client(monkeypatch):
    class Client:
        closed = False

        def list_objects_v2(self, **kwargs):
            assert kwargs == {"Bucket": "data", "Prefix": "", "MaxKeys": 20}
            raise RuntimeError("secret")

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(svc.boto3, "client", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError):
        svc._test_s3(CONFIG, OLD)
    assert client.closed


def test_sftp_probe_does_not_enumerate_full_directory(monkeypatch):
    import paramiko

    class Entries:
        count = 0
        closed = False

        def __iter__(self):
            return self

        def __next__(self):
            self.count += 1
            assert self.count <= 20
            return object()

        def close(self):
            self.closed = True

    entries = Entries()

    class Client:
        closed = False

        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            assert kwargs["auth_timeout"] == kwargs["channel_timeout"] == 10

        def open_sftp(self):
            return self

        def get_channel(self):
            return self

        def settimeout(self, value):
            assert value == 10

        def listdir_iter(self, path, read_aheads):
            assert path == "/readonly" and read_aheads == 1
            return entries

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    assert (
        svc._test_sftp(
            {"host": "example", "username": "user", "base_path": "/readonly"},
            {"password": "secret"},
        )
        == 20
    )
    assert entries.closed and client.closed


async def test_candidate_rechecks_original_api_key(
    connection, db_session, super_admin, monkeypatch
):
    from app.db.models.api_key import ApiKey

    conn_id, _ = connection
    actor = super_admin[0]
    key = ApiKey(
        user_id=actor.id,
        name="candidate",
        key_prefix="ak_candidate",
        key_hash="initial-hash",
        scopes=["datasets:write"],
    )
    db_session.add(key)
    await db_session.flush()

    async def probe(*args, **kwargs):
        key.key_hash = "rotated-hash"
        await db_session.flush()
        return True, "ok", 0

    monkeypatch.setattr(svc, "_probe", probe)
    conn = await db_session.get(StorageConnection, uuid.UUID(conn_id))
    old_cipher = conn.secret_enc
    with pytest.raises(HTTPException) as exc:
        await svc.StorageConnectionService.update(
            db_session,
            conn,
            expected_revision=1,
            actor_id=actor.id,
            api_key_id=key.id,
            name="candidate",
            config=None,
            secret=NEW,
        )
    assert exc.value.status_code == 403
    await db_session.refresh(conn)
    assert conn.revision == 1 and conn.secret_enc == old_cipher


def test_qualified_sftp_probe_uses_existing_pinned_root(monkeypatch):
    from contextlib import contextmanager
    from app.services.sources import sftp_reference

    config = {"host": "qualified.test", "username": "reader", "base_path": "/"}
    root = {"host": "qualified.test", "host_key": "fixed-host-key"}
    monkeypatch.setattr(
        sftp_reference, "qualified_root", lambda value: ("qualified", root)
    )
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )

    class Sftp:
        def get_channel(self):
            return self

        def get_transport(self):
            return self

        def close(self):
            pass

        def listdir_iter(self, path, read_aheads):
            assert path == "/" and read_aheads == 1
            yield object()

    @contextmanager
    def client(access):
        assert access.root is root and access.handle == "qualified"
        assert decrypt_secret(access.secret_enc) == NEW
        assert access.allowlist == ("8.8.8.8/32",)
        yield Sftp()

    monkeypatch.setattr(sftp_reference.SftpAccess, "client", client)
    assert svc._test_sftp(config, NEW, ["8.8.8.8/32"]) == 1


def test_sftp_deadline_closes_a_non_yielding_remote_listing(monkeypatch):
    import paramiko
    from threading import Event

    closed = Event()

    class Client:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def open_sftp(self):
            return self

        def get_channel(self):
            return self

        def settimeout(self, value):
            pass

        def listdir_iter(self, *args, **kwargs):
            assert closed.wait(1), "the transport deadline did not close the client"
            raise ConnectionError("closed")
            yield

        def close(self):
            closed.set()

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    monkeypatch.setattr(svc, "_PROBE_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(ConnectionError):
        svc._test_sftp(
            {"host": "sftp.test", "username": "reader"}, {"password": "secret"}
        )
    assert closed.is_set()

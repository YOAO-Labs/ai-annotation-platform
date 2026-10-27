"""Candidate validation fences publication in the Assets Source owner.

The retired API service is gone. These tests exercise the canonical
``ConnectionsWorkflow``/native owner path through HTTP, patching only the
portable owner seam (``aap_assets_source.connections_workflow.probe_connection``
and the bounded probe internals).
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from threading import Event
import uuid

import anyio
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_source import connections_workflow
from aap_assets_source.connectors import probe as probe_module
from app.config import settings
from app.core.crypto import decrypt_secret
from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageLocation, StorageBinding
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate,
    StorageOperation,
)
from app.db.models.system_setting import SystemSetting
from app.db.models.user import User
from app.services.storage_location import connector_location_id
from tests.factory import create_user

OLD = {"access_key": "old-access", "secret_key": "old-secret"}
NEW = {"access_key": "new-access", "secret_key": "new-secret"}
CONFIG = {"endpoint": "http://8.8.8.8:9000", "bucket": "data"}


async def _success(*args, **kwargs):
    return True, "连接成功", 0


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

    monkeypatch.setattr(connections_workflow, "probe_connection", forbidden)
    path = f"/api/v1/storage-connections/{conn_id}"
    assert (
        await httpx_client.patch(path, headers=headers, json={"name": "missing"})
    ).status_code == 422
    first = await httpx_client.patch(
        path, headers=headers, json={"name": "winner", "expected_revision": 1}
    )
    assert first.status_code == 200 and first.json()["revision"] == 2
    # Sending the identical config is not a candidate change: no probe and no
    # storage-location revision movement.
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

    async def probe(*args, **kwargs):
        if failure == "authentication":
            return False, "连接失败: ClientError", None
        if failure == "timeout":
            await anyio.sleep(0.08)
            return False, "连接失败: ConnectionError", None
        return False, "连接失败: RuntimeError", None

    monkeypatch.setattr(connections_workflow, "probe_connection", probe)
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
    for value in ["new-secret", "old-secret", "signed.invalid", "8.8.8.8"]:
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
    await anyio.sleep(0.1)


async def test_successful_rotation_validates_candidate_without_replacing_old_first(
    httpx_client, connection, db_session, monkeypatch
):
    conn_id, headers = connection
    conn = await db_session.get(StorageConnection, uuid.UUID(conn_id))
    old_cipher = conn.secret_enc

    async def probe(kind, config, secret, **kwargs):
        assert (kind, config, secret) == ("s3", CONFIG, NEW)
        assert conn.secret_enc == old_cipher
        return True, "连接成功", 0

    monkeypatch.setattr(connections_workflow, "probe_connection", probe)
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


async def test_connection_test_receipts_are_sanitized_and_replayable(
    test_engine, monkeypatch
):
    """The owner probe lease is independently durable, so this needs committed state."""
    ok_operation, failed_operation = uuid.uuid4(), uuid.uuid4()
    calls: list[str] = []

    async def success(*args, **kwargs):
        calls.append("success")
        return True, "连接成功", 0

    async def failure(*args, **kwargs):
        calls.append("failure")
        return False, "连接失败: RuntimeError", None

    async with committed_world(test_engine, monkeypatch) as (
        maker,
        actor_id,
        connection_id,
    ):
        from aap_assets_source.connections_contract import TestConnectionRequest
        from app.services.assets_connections import AssetsConnectionsGateway

        async def tested(operation_id):
            async with maker() as db:
                return await AssetsConnectionsGateway(db).test_connection(
                    TestConnectionRequest(
                        operation_id=operation_id,
                        actor_id=actor_id,
                        connection_id=connection_id,
                    )
                )

        monkeypatch.setattr(connections_workflow, "probe_connection", success)
        receipt = await tested(ok_operation)
        assert receipt.ok and receipt.sample_count == 0
        monkeypatch.setattr(connections_workflow, "probe_connection", failure)
        failed = await tested(failed_operation)
        assert not failed.ok and failed.message == "连接失败: RuntimeError"
        assert "old-secret" not in failed.message
        assert "signed.invalid" not in failed.message
        assert await tested(failed_operation) == failed
        assert calls == ["success", "failure"]


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
        actor_id = actor.id
        await db.commit()
    connection_id = uuid.uuid4()
    async with maker() as db:
        from aap_audit_contract import ProjectDeletionAuditContext
        from aap_assets_source.connections_contract import CreateConnectionRequest
        from app.services.assets_connections import AssetsConnectionsGateway

        created = await AssetsConnectionsGateway(db).create_connection(
            CreateConnectionRequest(
                operation_id=connection_id,
                actor_id=actor_id,
                name="committed",
                kind="s3",
                config=CONFIG,
                secret=OLD,
                audit_context=ProjectDeletionAuditContext(),
            )
        )
        await db.commit()
        assert created.connection is not None
        assert created.connection.id == connection_id
    try:
        yield maker, actor_id, connection_id
    finally:
        location_id = connector_location_id(connection_id)
        # Restore the connector allowlist override first: a later teardown
        # failure must not strand an empty committed allowlist for the next test.
        async with maker.begin() as db:
            await db.execute(
                delete(SystemSetting).where(
                    SystemSetting.key == "connector_host_allowlist"
                )
            )
        async with maker.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.storage_connection_operations "
                    "WHERE connection_id = :connection_id"
                ),
                {"connection_id": connection_id},
            )
            await db.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == location_id
                )
            )
            await db.execute(
                delete(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id == location_id
                )
            )
            await db.execute(
                delete(StorageBinding).where(
                    StorageBinding.purpose == f"cas-{connection_id.hex[:20]}"
                )
            )
            await db.execute(
                delete(StorageConnection).where(StorageConnection.id == connection_id)
            )
        async with maker.begin() as db:
            # Use the audit owner's sanctioned fixture exemption for this one
            # transaction; the immutability trigger still guards every other path.
            await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await db.execute(delete(AuditLog).where(AuditLog.actor_id == actor_id))
            await db.execute(delete(User).where(User.id == actor_id))


@pytest.mark.parametrize(
    "change, expected_status",
    [
        ("rename", 409),
        ("delete", 404),
        ("disable", 403),
        ("demote", 403),
        ("allowlist", 409),
        ("reference", 409),
    ],
)
async def test_validation_holds_no_row_locks_and_rechecks_publication(
    test_engine, httpx_client, monkeypatch, change, expected_status
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await release.wait()
        return True, "连接成功", 0

    monkeypatch.setattr(connections_workflow, "probe_connection", probe)
    async with committed_world(test_engine, monkeypatch) as (
        maker,
        actor_id,
        connection_id,
    ):

        async def candidate():
            async with maker() as db:
                from aap_assets_source.connections_contract import (
                    UpdateConnectionRequest,
                )
                from app.services.assets_connections import AssetsConnectionsGateway

                try:
                    await AssetsConnectionsGateway(db).update_connection(
                        UpdateConnectionRequest(
                            operation_id=uuid.uuid4(),
                            actor_id=actor_id,
                            connection_id=connection_id,
                            expected_revision=1,
                            name="candidate",
                            config={**CONFIG, "bucket": "candidate"},
                            secret=NEW,
                        )
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
                        conn = await db.get(StorageConnection, connection_id)
                        if change == "rename":
                            conn.name = "winner"
                        else:
                            await db.delete(conn)
                    elif change in {"disable", "demote"}:
                        actor_row = await db.get(User, actor_id)
                        if change == "disable":
                            actor_row.is_active = False
                        else:
                            actor_row.role = "employee"
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
                                purpose=f"cas-{connection_id.hex[:20]}",
                                location_id=connector_location_id(connection_id),
                                bucket="data",
                            )
                        )
                    await db.commit()

            await asyncio.wait_for(concurrent(), 3)
            # The concurrent change itself advances the connection revision
            # (Main's update trigger), so record what it published instead of
            # pinning an incidental revision count. The rejected candidate must
            # not advance it any further.
            async with maker() as db:
                published = await db.get(StorageConnection, connection_id)
                published_revision = None if published is None else published.revision
            release.set()
            with pytest.raises(Exception) as exc:
                await asyncio.wait_for(task, 3)
            assert _error_status(exc.value) == expected_status
            async with maker() as db:
                conn = await db.get(StorageConnection, connection_id)
                if change == "delete":
                    assert conn is None
                else:
                    assert conn.name == (
                        "winner" if change == "rename" else "committed"
                    )
                    assert conn.config == CONFIG
                    assert decrypt_secret(conn.secret_enc) == OLD
                    assert conn.revision == published_revision
                    location = await db.get(
                        StorageLocation, connector_location_id(connection_id)
                    )
                    assert location.config_revision == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("code"))
    return type(exc).__name__


def _error_status(exc: BaseException) -> int:
    return int(getattr(exc, "status_code", 400))


def test_s3_probe_bounds_and_closes_client(monkeypatch):
    import boto3

    class Client:
        closed = False

        def list_objects_v2(self, **kwargs):
            assert kwargs == {"Bucket": "data", "Prefix": "", "MaxKeys": 20}
            raise RuntimeError("secret")

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError):
        probe_module._probe_s3(CONFIG, OLD)
    assert client.closed


def test_sftp_probe_samples_without_enumerating_full_directory(monkeypatch):
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
        probe_module._probe_sftp(
            {"host": "example", "username": "user", "base_path": "/readonly"},
            {"password": "secret"},
            None,
        )
        == 20
    )
    assert entries.closed and client.closed


def test_qualified_sftp_probe_pins_the_known_hosts_file(monkeypatch):
    import paramiko

    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    loaded = []

    class Entries:
        def __iter__(self):
            yield object()

        def close(self):
            pass

    class Sftp:
        def get_channel(self):
            return self

        def settimeout(self, value):
            assert value == 10

        def listdir_iter(self, path, read_aheads):
            assert path == "/" and read_aheads == 1
            return Entries()

        def close(self):
            pass

    class Client:
        def load_host_keys(self, path):
            loaded.append(path)

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

        def open_sftp(self):
            return Sftp()

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    assert (
        probe_module._probe_sftp(
            {"host": "qualified.test", "username": "reader", "base_path": "/"},
            NEW,
            "/pinned/known_hosts",
        )
        == 1
    )
    assert loaded == ["/pinned/known_hosts"]


def test_sftp_deadline_closes_a_non_yielding_remote_listing(monkeypatch):
    import paramiko

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
    monkeypatch.setattr(probe_module, "PROBE_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(ConnectionError):
        probe_module._probe_sftp(
            {"host": "sftp.test", "username": "reader"},
            {"password": "secret"},
            None,
        )
    assert closed.is_set()


async def test_candidate_actor_role_loss_preserves_secret(
    httpx_client, connection, db_session, monkeypatch
):
    conn_id, headers = connection
    conn = await db_session.get(StorageConnection, uuid.UUID(conn_id))
    old_cipher = conn.secret_enc
    # Capture the owner identity before the request: a rejected candidate
    # rolls the caller's unit of work back, which expires this instance.
    actor_id = conn.created_by

    async def probe(*args, **kwargs):
        actor = await db_session.get(User, actor_id)
        actor.role = "employee"
        await db_session.commit()
        return True, "连接成功", 0

    monkeypatch.setattr(connections_workflow, "probe_connection", probe)
    try:
        response = await httpx_client.patch(
            f"/api/v1/storage-connections/{conn_id}",
            headers=headers,
            json={"expected_revision": 1, "secret": NEW},
        )
    finally:
        actor = await db_session.get(User, actor_id)
        actor.role = "super_admin"
        await db_session.commit()
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "access_revoked"
    await db_session.refresh(conn)
    assert conn.revision == 1 and conn.secret_enc == old_cipher


async def test_candidate_rechecks_accepted_api_key_after_probe(
    connection, db_session, super_admin, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from aap_assets_source.connections_contract import (
        ConnectionAuthorityProof,
        UpdateConnectionRequest,
    )
    from app.db.models.api_key import ApiKey
    from app.services.assets_connections import AssetsConnectionsGateway
    from app.services.identity_account_lock_data import IdentityAccountLockData

    conn_id, _ = connection
    conn = await db_session.get(StorageConnection, uuid.UUID(conn_id))
    old_cipher = conn.secret_enc
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
    facts = await IdentityAccountLockData(db_session).read_accepted_api_key_facts(
        actor.id, key.id
    )
    assert facts is not None
    proof = ConnectionAuthorityProof(
        scopes=list(facts.scopes),
        expires_at=facts.expires_at,
        revision=facts.revision,
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await release.wait()
        return True, "连接成功", 0

    monkeypatch.setattr(connections_workflow, "probe_connection", probe)
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def candidate():
        return await AssetsConnectionsGateway(db_session).update_connection(
            UpdateConnectionRequest(
                operation_id=uuid.uuid4(),
                actor_id=actor.id,
                connection_id=uuid.UUID(conn_id),
                expected_revision=1,
                secret=NEW,
                api_key_id=key.id,
                accepted_api_key_proof=proof,
            )
        )

    task = asyncio.create_task(candidate())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with maker() as other:
            rotated = await other.get(ApiKey, key.id)
            rotated.key_hash = "rotated-hash"
            await other.commit()
        release.set()
        with pytest.raises(Exception) as exc:
            await asyncio.wait_for(task, 3)
        assert _error_code(exc.value) == "api_key_authority_revoked"
        assert _error_status(exc.value) == 403
        await db_session.refresh(conn)
        assert conn.revision == 1 and conn.secret_enc == old_cipher
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_connection_probe_lease_survives_cancellation_until_thread_closes(
    test_engine, monkeypatch
):
    """The owner probe keeps its storage_operations receipt until the thread ends."""
    from sqlalchemy import delete, func, select

    from aap_assets_source.connectors import probe as owner_probe
    from aap_assets_source.connections_contract import (
        TestConnectionRequest,
    )
    from app.db.models.storage_maintenance import (
        StorageMaintenanceGate as Gate,
        StorageOperation,
    )
    from app.services.assets_connections import AssetsConnectionsGateway
    from app.services.storage_location import connector_location_id

    entered, release, closed = (
        Event(),
        Event(),
        Event(),
    )

    def blocking(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            return 0
        finally:
            closed.set()

    monkeypatch.setattr(owner_probe, "_probe_blocking", blocking)
    async with committed_world(test_engine, monkeypatch) as (
        maker,
        actor_id,
        connection_id,
    ):
        location_id = connector_location_id(connection_id)
        task = None
        try:
            async with maker() as db:
                task = asyncio.create_task(
                    AssetsConnectionsGateway(db).test_connection(
                        TestConnectionRequest(
                            operation_id=uuid.uuid4(),
                            actor_id=actor_id,
                            connection_id=connection_id,
                        )
                    )
                )
                assert await asyncio.to_thread(entered.wait, 2)
                async with maker() as observer:
                    statement = (
                        select(func.count())
                        .select_from(StorageOperation)
                        .where(StorageOperation.location_id == location_id)
                    )
                    assert await observer.scalar(statement) == 1
                    task.cancel()
                    await asyncio.sleep(0.02)
                    assert not task.done() and not closed.is_set()
                    assert await observer.scalar(statement) == 1
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert closed.is_set()
                    assert await observer.scalar(statement) == 0
        finally:
            release.set()
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            async with test_engine.begin() as connection:
                await connection.execute(
                    delete(Gate).where(Gate.location_id == location_id)
                )

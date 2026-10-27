from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from aap_assets_source import connections_workflow as module
from aap_assets_source.connections_contract import (
    ConnectionCapture,
    ConnectionFailure,
    ConnectionPolicy,
    ConnectionProbeReceipt,
    ConnectionRead,
    ConnectionReceiptLookup,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    EmptyConnectionIntent,
    ListConnectionsRequest,
    NativeConnectionFacts,
    NativeConnectionMutation,
    NativeConnectionRead,
    NativeConnectionsPage,
    ReadConnectionRequest,
    TestConnectionRequest,
    UpdateConnectionRequest,
)
from aap_assets_source.connections_workflow import (
    ConnectionsOptions,
    ConnectionsWorkflow,
)
from aap_assets_source.connectors.crypto import decrypt_secret, encrypt_secret
from aap_assets_source.connectors.host_guard import ConnectorHostDenied


class Native:
    def __init__(self, *, kind="s3", config=None, secret=None):
        self.actor_id, self.connection_id = uuid4(), uuid4()
        self.key = Fernet.generate_key().decode()
        self.role = "project_admin"
        self.calls = []
        self.saved = {}
        self.final_requests = []
        self.policy_error = None
        self.final_error = None
        self.secret = secret or {"access_key": "AK", "secret_key": "SK"}
        self.cipher = base64.b64encode(encrypt_secret(self.secret, self.key)).decode()
        now = datetime.now(timezone.utc)
        self.record = NativeConnectionFacts(
            id=self.connection_id,
            revision=1,
            name="connection",
            kind=kind,
            config=config or {"endpoint": "http://8.8.8.8", "bucket": "data"},
            scope="owner",
            project_id=None,
            secret_set=True,
            created_by=self.actor_id,
            created_at=now,
            updated_at=now,
        )

    async def read_storage_connection_receipt(self, request):
        self.calls.append("receipt")
        saved = self.saved.get(request.operation_id)
        if saved is not None and saved[0] != request.intent:
            raise ConnectionFailure("operation_conflict")
        return ConnectionReceiptLookup(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            action=request.action,
            connection_id=request.connection_id,
            result=saved[1] if saved else None,
        )

    async def read_storage_connection_policy(self, request):
        self.calls.append("policy")
        if self.policy_error:
            raise self.policy_error
        return ConnectionPolicy(
            actor_id=request.actor_id,
            platform_role=self.role,
            allowlist=["8.8.8.0/24"],
            policy_source="environment",
            policy_digest="b" * 64,
        )

    async def begin_connection_probe(self, request):
        from aap_assets_source.connections_contract import ConnectionProbeLease

        return ConnectionProbeLease(
            operation_id=uuid4(),
            connection_id=request.connection_id,
            location_id=uuid4(),
            revision=1,
            proof=None,
        )

    async def finish_connection_probe(self, request):
        from aap_assets_source.connections_contract import ConnectionProbeFinish

        return ConnectionProbeFinish(
            settled=request.settled,
            retained=not request.settled,
            proof=None,
        )

    async def update_storage_connection_authorized(self, request):
        return await self.update_storage_connection(request)

    async def capture_storage_connection(self, request):
        self.calls.append("capture")
        return ConnectionCapture(
            actor_id=request.actor_id,
            platform_role=self.role,
            connection_id=request.connection_id,
            access=request.access,
            connection=self.record,
            secret_enc=self.cipher,
            source_snapshot="c" * 64,
        )

    async def read_storage_connection(self, request):
        return NativeConnectionRead(
            actor_id=request.actor_id,
            connection_id=request.connection_id,
            connection=self.record,
        )

    async def list_storage_connections(self, request):
        return NativeConnectionsPage(
            actor_id=request.actor_id,
            after=request.after,
            next_cursor=None,
            snapshot_digest="d" * 64,
            total=1,
            complete=True,
            connections=[self.record],
        )

    async def create_storage_connection(self, request):
        self.calls.append("create")
        self.final_requests.append(request)
        record = self.record.model_copy(
            update={
                "id": request.operation_id,
                "name": request.intent.name,
                "kind": request.intent.kind,
                "scope": request.intent.scope,
                "config": request.config,
            }
        )
        result = NativeConnectionMutation(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            action="create",
            connection_id=request.operation_id,
            connection=record,
        )
        self.saved[request.operation_id] = (request.intent, result)
        return result

    async def update_storage_connection(self, request):
        self.calls.append("update")
        self.final_requests.append(request)
        record = self.record.model_copy(
            update={
                "name": request.intent.name or self.record.name,
                "config": request.config,
            }
        )
        return NativeConnectionMutation(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            action="update",
            connection_id=request.connection_id,
            connection=record,
        )

    async def record_storage_connection_test(self, request):
        self.calls.append("record_test")
        self.final_requests.append(request)
        if self.final_error:
            raise self.final_error
        return ConnectionProbeReceipt(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            connection_id=request.connection_id,
            **request.result.model_dump(),
        )

    async def delete_storage_connection(self, request):
        self.calls.append("delete")
        return NativeConnectionMutation(
            operation_id=request.operation_id,
            actor_id=request.actor_id,
            connection_id=request.connection_id,
            action="delete",
            connection=None,
        )


async def _probe_ok(*args, **kwargs):
    return True, "连接成功", 0


def workflow(native, *, key=None):
    return ConnectionsWorkflow(
        native=native,
        options=ConnectionsOptions(
            encryption_key=native.key if key is None else key,
            fallback_allowlist=("8.8.8.0/24",),
        ),
    )


@pytest.mark.asyncio
async def test_create_commits_encrypted_credentials_and_replay_skips_io(monkeypatch):
    native = Native()
    owner = workflow(native)
    request = CreateConnectionRequest(
        operation_id=uuid4(),
        actor_id=native.actor_id,
        name="created",
        kind="s3",
        config=native.record.config,
        secret=native.secret,
    )
    calls = []
    monkeypatch.setattr(
        module,
        "assert_host_allowed",
        lambda host, entries: calls.append((host, entries)),
    )
    first = await owner.invoke("create", request)
    assert first.connection.id == request.operation_id
    assert first.connection.created_by == native.actor_id
    final = native.final_requests[0]
    assert (
        decrypt_secret(base64.b64decode(final.secret_enc), native.key) == native.secret
    )
    assert "secret" not in final.intent.model_dump()
    assert len(final.intent.secret_digest) == 64
    assert native.calls == ["receipt", "policy", "create"]
    assert len(calls) == 1
    monkeypatch.setattr(
        module, "assert_host_allowed", lambda *_: pytest.fail("replay performed DNS")
    )
    monkeypatch.setattr(
        module, "encrypt_secret", lambda *_: pytest.fail("replay encrypted again")
    )
    assert await owner.invoke("create", request) == first
    assert len(native.final_requests) == 1
    changed = request.model_copy(
        update={"secret": {**native.secret, "secret_key": "rotated"}}
    )
    with pytest.raises(ConnectionFailure) as failure:
        await owner.invoke("create", changed)
    assert failure.value.code == "operation_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch", [{}, {"name": "renamed"}, {"name": None, "config": None, "secret": None}]
)
async def test_no_network_patch_keeps_credentials_and_ignores_policy_without_key(
    monkeypatch, patch
):
    native = Native()
    native.policy_error = AssertionError("name-only patch resolved policy")
    monkeypatch.setattr(
        module,
        "assert_host_allowed",
        lambda *_: pytest.fail("name-only patch performed DNS"),
    )
    monkeypatch.setattr(
        module, "decrypt_secret", lambda *_: pytest.fail("name-only patch decrypted")
    )
    result = await workflow(native, key="").invoke(
        "update",
        UpdateConnectionRequest(
            operation_id=uuid4(),
            actor_id=native.actor_id,
            connection_id=native.connection_id,
            expected_revision=1,
            **patch,
        ),
    )
    assert (
        result.connection.name == patch.get("name")
        or result.connection.name == "connection"
    )
    final = native.final_requests[0]
    assert final.secret_enc is None
    assert final.policy_digest is None
    assert final.source_snapshot == "c" * 64
    assert final.config == native.record.config


@pytest.mark.asyncio
async def test_config_only_sftp_update_uses_retained_credentials_and_infers_auth(
    monkeypatch,
):
    native = Native(
        kind="sftp",
        config={"host": "8.8.8.8", "username": "u"},
        secret={"password": "retained"},
    )
    monkeypatch.setattr(module, "assert_host_allowed", lambda *_: None)
    monkeypatch.setattr(
        module,
        "probe_connection",
        lambda *args, **kwargs: _probe_ok(),
    )
    await workflow(native).invoke(
        "update",
        UpdateConnectionRequest(
            operation_id=uuid4(),
            actor_id=native.actor_id,
            connection_id=native.connection_id,
            expected_revision=1,
            config={"host": "8.8.8.8", "username": "changed"},
        ),
    )
    final = native.final_requests[0]
    assert final.config == {
        "host": "8.8.8.8",
        "username": "changed",
        "auth_type": "password",
    }
    assert final.secret_enc is None
    assert final.intent.config == {"host": "8.8.8.8", "username": "changed"}
    assert final.policy_digest == "b" * 64
    assert native.calls == [
        "receipt",
        "capture",
        "policy",
        "policy",
        "capture",
        "update",
    ]


@pytest.mark.asyncio
async def test_secret_only_sftp_update_infers_auth_without_host_policy(monkeypatch):
    native = Native(
        kind="sftp",
        config={"host": "8.8.8.8", "username": "u", "legacy": "retained"},
        secret={"password": "old"},
    )
    monkeypatch.setattr(module, "assert_host_allowed", lambda *_: None)
    monkeypatch.setattr(
        module,
        "probe_connection",
        lambda *args, **kwargs: _probe_ok(),
    )
    await workflow(native).invoke(
        "update",
        UpdateConnectionRequest(
            operation_id=uuid4(),
            actor_id=native.actor_id,
            connection_id=native.connection_id,
            expected_revision=1,
            secret={"password": "new"},
        ),
    )
    final = native.final_requests[0]
    assert (
        final.config["auth_type"] == "password" and final.config["legacy"] == "retained"
    )
    assert decrypt_secret(base64.b64decode(final.secret_enc), native.key) == {
        "password": "new"
    }
    assert final.policy_digest is None


@pytest.mark.asyncio
async def test_probe_checks_host_before_decryption_and_never_records_denied_probe(
    monkeypatch,
):
    native = Native()

    def denied(*_):
        raise ConnectorHostDenied("connector target is not allowlisted")

    monkeypatch.setattr(module, "assert_host_allowed", denied)
    monkeypatch.setattr(
        module, "decrypt_secret", lambda *_: pytest.fail("denied probe decrypted")
    )
    with pytest.raises(ConnectionFailure) as failure:
        await workflow(native, key="").invoke(
            "test",
            TestConnectionRequest(
                operation_id=uuid4(),
                actor_id=native.actor_id,
                connection_id=native.connection_id,
            ),
        )
    assert failure.value.code == "host_denied"
    assert native.final_requests == []


@pytest.mark.asyncio
async def test_probe_submits_captured_source_and_policy_after_io(monkeypatch):
    native = Native()
    native.final_error = ConnectionFailure("policy_changed")
    monkeypatch.setattr(module, "assert_host_allowed", lambda *_: None)

    async def probe(kind, config, secret, **kwargs):
        assert native.final_requests == []
        assert secret == native.secret and kwargs["allowlist"] == ["8.8.8.0/24"]
        return True, "连接成功", 4

    monkeypatch.setattr(module, "probe_connection", probe)
    with pytest.raises(ConnectionFailure) as failure:
        await workflow(native).invoke(
            "test",
            TestConnectionRequest(
                operation_id=uuid4(),
                actor_id=native.actor_id,
                connection_id=native.connection_id,
            ),
        )
    assert failure.value.code == "policy_changed"
    final = native.final_requests[0]
    assert final.source_snapshot == "c" * 64 and final.policy_digest == "b" * 64
    assert final.result.sample_count == 4
    assert native.calls == ["receipt", "capture", "policy", "record_test"]


@pytest.mark.asyncio
async def test_legacy_read_and_list_mask_embedded_credentials_and_project_scope():
    native = Native()
    native.record = native.record.model_copy(
        update={
            "scope": "project",
            "project_id": uuid4(),
            "config": {
                **native.record.config,
                "endpoint": "http://user:password@8.8.8.8",
                "password": "hidden",
            },
        }
    )
    owner = workflow(native, key="")
    result = await owner.invoke(
        "read",
        ReadConnectionRequest(
            actor_id=native.actor_id, connection_id=native.connection_id
        ),
    )
    assert isinstance(result, ConnectionRead)
    assert (
        result.connection.scope == "owner"
        and result.connection.project_id == native.record.project_id
    )
    assert result.connection.config == {"endpoint": "http://8.8.8.8", "bucket": "data"}
    assert "password" not in result.model_dump_json()
    page = await owner.invoke("list", ListConnectionsRequest(actor_id=native.actor_id))
    assert page.connections == [result.connection]


@pytest.mark.asyncio
async def test_native_scope_corruption_fails_before_public_response():
    native = Native()

    async def escaped(request):
        return NativeConnectionRead(
            actor_id=uuid4(),
            connection_id=request.connection_id,
            connection=native.record,
        )

    native.read_storage_connection = escaped
    with pytest.raises(ConnectionFailure) as failure:
        await workflow(native).invoke(
            "read",
            ReadConnectionRequest(
                actor_id=native.actor_id, connection_id=native.connection_id
            ),
        )
    assert failure.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_probe_replay_uses_saved_outcome_without_credentials_or_network(
    monkeypatch,
):
    native = Native()
    request = TestConnectionRequest(
        operation_id=uuid4(),
        actor_id=native.actor_id,
        connection_id=native.connection_id,
    )
    saved = ConnectionProbeReceipt(
        operation_id=request.operation_id,
        actor_id=request.actor_id,
        connection_id=request.connection_id,
        ok=True,
        message="连接成功",
        sample_count=4,
    )
    native.saved[request.operation_id] = (EmptyConnectionIntent(), saved)
    monkeypatch.setattr(
        module, "probe_connection", lambda *_: pytest.fail("cached probe used network")
    )
    assert await workflow(native, key="").invoke("test", request) == saved
    assert native.calls == ["receipt"]


@pytest.mark.asyncio
async def test_delete_uses_atomic_owner_command_without_credentials_or_policy():
    native = Native()
    result = await workflow(native, key="").invoke(
        "delete",
        DeleteConnectionRequest(
            operation_id=uuid4(),
            actor_id=native.actor_id,
            connection_id=native.connection_id,
        ),
    )
    assert result.connection is None and result.action == "delete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["kind", "scope", "project_id", "created_by", "created_at", "name"]
)
async def test_update_rejects_changed_captured_immutable_fields(field):
    native = Native()
    original = native.update_storage_connection
    changed = {
        "kind": "sftp",
        "scope": "global",
        "project_id": uuid4(),
        "created_by": uuid4(),
        "created_at": native.record.created_at + timedelta(seconds=1),
        "name": "unrequested name",
    }

    async def corrupt(request):
        result = await original(request)
        return result.model_copy(
            update={
                "connection": result.connection.model_copy(
                    update={field: changed[field]}
                )
            }
        )

    native.update_storage_connection = corrupt
    with pytest.raises(ConnectionFailure) as failure:
        await workflow(native, key="").invoke(
            "update",
            UpdateConnectionRequest(
                operation_id=uuid4(),
                actor_id=native.actor_id,
                connection_id=native.connection_id,
                expected_revision=1,
            ),
        )
    assert failure.value.code == "invalid_response"

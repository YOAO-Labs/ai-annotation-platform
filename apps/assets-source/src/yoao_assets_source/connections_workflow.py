"""Storage connection operations shared by local and independent providers."""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from .connections_contract import (
    BeginConnectionProbeRequest,
    ConnectionProbeFinish,
    ConnectionProbeLease,
    FinishConnectionProbeRequest,
    CONNECTION_NATIVE_OPERATIONS,
    RUNTIME_NATIVE_OPERATIONS,
    CONNECTION_OPERATIONS,
    CaptureConnectionRequest,
    ConnectionCapture,
    ConnectionFailure,
    ConnectionMutation,
    ConnectionPolicy,
    ConnectionPolicyRequest,
    ConnectionProbeReceipt,
    ConnectionProbeResult,
    ConnectionRead,
    ConnectionReceiptLookup,
    ConnectionReceiptRequest,
    ConnectionRecord,
    ConnectionsPage,
    CreateConnectionIntent,
    CreateConnectionNativeRequest,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    EmptyConnectionIntent,
    ListConnectionsRequest,
    NativeConnectionFacts,
    NativeConnectionMutation,
    NativeConnectionRead,
    NativeConnectionsPage,
    ReadConnectionRequest,
    RecordConnectionTestRequest,
    TestConnectionRequest,
    UpdateConnectionIntent,
    UpdateConnectionAuthorizedRequest,
    UpdateConnectionRequest,
    require_connection_scope,
)
from .connectors.crypto import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    credential_fingerprint,
    decrypt_secret,
    encrypt_secret,
)
from .connectors.host_guard import (
    ConnectorAllowlistInvalid,
    ConnectorHostDenied,
    assert_host_allowed,
    normalize_allowlist,
)
from .connectors.management import (
    ConnectorValidationError,
    public_config,
    validate_config,
    validate_secret,
)
from .connectors.probe import probe_connection
from .connectors.registry import target_host


@dataclass(frozen=True, slots=True)
class ConnectionsOptions:
    encryption_key: str = field(default="", repr=False)
    fallback_allowlist: tuple[str, ...] = ()
    known_hosts_file: str | None = None


class ConnectionsNativePort(Protocol):
    async def list_storage_connections(
        self, request: ListConnectionsRequest
    ) -> NativeConnectionsPage: ...
    async def read_storage_connection(
        self, request: ReadConnectionRequest
    ) -> NativeConnectionRead: ...
    async def read_storage_connection_policy(
        self, request: ConnectionPolicyRequest
    ) -> ConnectionPolicy: ...
    async def capture_storage_connection(
        self, request: CaptureConnectionRequest
    ) -> ConnectionCapture: ...
    async def read_storage_connection_receipt(
        self, request: ConnectionReceiptRequest
    ) -> ConnectionReceiptLookup: ...
    async def create_storage_connection(
        self, request: CreateConnectionNativeRequest
    ) -> NativeConnectionMutation: ...
    async def update_storage_connection_authorized(
        self, request: UpdateConnectionAuthorizedRequest
    ) -> NativeConnectionMutation: ...
    async def delete_storage_connection(
        self, request: DeleteConnectionRequest
    ) -> NativeConnectionMutation: ...
    async def record_storage_connection_test(
        self, request: RecordConnectionTestRequest
    ) -> ConnectionProbeReceipt: ...
    async def begin_connection_probe(
        self, request: BeginConnectionProbeRequest
    ) -> ConnectionProbeLease: ...
    async def finish_connection_probe(
        self, request: FinishConnectionProbeRequest
    ) -> ConnectionProbeFinish: ...


def _public_record(record: NativeConnectionFacts) -> ConnectionRecord:
    if record.scope not in {"global", "owner", "project"}:
        raise ConnectionFailure("invalid_response")
    return ConnectionRecord.model_validate(
        {
            **record.model_dump(),
            "scope": "global" if record.scope == "global" else "owner",
            "config": public_config(record.kind, record.config),
        }
    )


def _public_mutation(result: NativeConnectionMutation) -> ConnectionMutation:
    return ConnectionMutation.model_validate(
        {
            **result.model_dump(),
            "connection": _public_record(result.connection)
            if result.connection is not None
            else None,
        }
    )


class ConnectionsWorkflow:
    def __init__(self, *, native: ConnectionsNativePort, options: ConnectionsOptions):
        self._native = native
        self._options = options

    async def invoke(self, operation: str, request: BaseModel) -> BaseModel:
        pair = CONNECTION_OPERATIONS.get(operation)
        if pair is None or type(request) is not pair[0]:
            raise ConnectionFailure("invalid_request")
        handlers = {
            "list": self.list_connections,
            "read": self.read_connection,
            "create": self.create_connection,
            "update": self.update_connection,
            "delete": self.delete_connection,
            "test": self.test_connection,
        }
        try:
            result = await handlers[operation](request)
            require_connection_scope(request, result)
            return result
        except ConnectorValidationError as exc:
            raise ConnectionFailure("validation_failed", str(exc)) from None
        except ConnectorHostDenied as exc:
            raise ConnectionFailure("host_denied", str(exc)) from None
        except ConnectorAllowlistInvalid:
            raise ConnectionFailure("policy_unavailable") from None
        except ConnectorCryptoNotConfigured as exc:
            raise ConnectionFailure("crypto_not_configured", str(exc)) from None
        except ConnectorCryptoError:
            raise ConnectionFailure(
                "crypto_error", "连接器凭据解密失败（密文损坏或密钥不匹配）"
            ) from None

    async def _call(self, operation: str, request: BaseModel):
        request_type, result_type = (
            CONNECTION_NATIVE_OPERATIONS.get(operation)
            or RUNTIME_NATIVE_OPERATIONS[operation]
        )
        if type(request) is not request_type:
            raise TypeError("invalid connection native request model")
        result = await getattr(self._native, operation)(request)
        if not isinstance(result, result_type):
            raise ConnectionFailure("invalid_response")
        if operation in CONNECTION_NATIVE_OPERATIONS:
            try:
                require_connection_scope(request, result)
            except (ValueError, TypeError, AttributeError):
                raise ConnectionFailure("invalid_response") from None
        return result

    async def _receipt(self, request: BaseModel, action: str, intent: BaseModel):
        result = await self._call(
            "read_storage_connection_receipt",
            ConnectionReceiptRequest(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                connection_id=request.operation_id
                if action == "create"
                else request.connection_id,
                action=action,
                intent=intent,
            ),
        )
        return result.result

    def _fingerprint(self, request: BaseModel) -> str | None:
        if request.secret is None:
            return None
        return credential_fingerprint(
            request.secret, self._options.encryption_key, request.operation_id
        )

    async def _capture(self, request: BaseModel, access: str) -> ConnectionCapture:
        return await self._call(
            "capture_storage_connection",
            CaptureConnectionRequest(
                actor_id=request.actor_id,
                connection_id=request.connection_id,
                access=access,
            ),
        )

    @staticmethod
    async def _check_host(
        kind: str, config: dict, policy: ConnectionPolicy
    ) -> list[str]:
        entries = normalize_allowlist(policy.allowlist)
        await asyncio.to_thread(assert_host_allowed, target_host(kind, config), entries)
        return entries

    def _decrypt(self, capture: ConnectionCapture) -> dict:
        if capture.secret_enc is None:
            return {}
        try:
            token = base64.b64decode(capture.secret_enc, validate=True)
        except (binascii.Error, ValueError):
            raise ConnectionFailure("invalid_response") from None
        return decrypt_secret(token, self._options.encryption_key)

    async def list_connections(
        self, request: ListConnectionsRequest
    ) -> ConnectionsPage:
        result = await self._call("list_storage_connections", request)
        return ConnectionsPage.model_validate(
            {
                **result.model_dump(),
                "connections": [
                    _public_record(record) for record in result.connections
                ],
            }
        )

    async def read_connection(self, request: ReadConnectionRequest) -> ConnectionRead:
        result = await self._call("read_storage_connection", request)
        return ConnectionRead(
            actor_id=result.actor_id,
            connection_id=result.connection_id,
            connection=_public_record(result.connection),
        )

    async def create_connection(
        self, request: CreateConnectionRequest
    ) -> ConnectionMutation:
        intent = CreateConnectionIntent(
            name=request.name,
            kind=request.kind,
            config=request.config,
            scope=request.scope,
            secret_digest=self._fingerprint(request),
        )
        saved = await self._receipt(request, "create", intent)
        if saved is not None:
            return _public_mutation(saved)
        config = validate_config(request.kind, request.config)
        inferred = validate_secret(request.kind, request.secret, config)
        if inferred is not None and config.get("auth_type") is None:
            config["auth_type"] = inferred
        policy = await self._call(
            "read_storage_connection_policy",
            ConnectionPolicyRequest(
                actor_id=request.actor_id,
                fallback_allowlist=list(self._options.fallback_allowlist),
            ),
        )
        if policy.platform_role not in {"super_admin", "project_admin"}:
            raise ConnectionFailure("manager_required")
        if request.scope == "global" and policy.platform_role != "super_admin":
            raise ConnectionFailure("global_creation_forbidden")
        await self._check_host(request.kind, config, policy)
        encrypted = encrypt_secret(request.secret, self._options.encryption_key)
        result = await self._call(
            "create_storage_connection",
            CreateConnectionNativeRequest(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                intent=intent,
                config=config,
                secret_enc=base64.b64encode(encrypted).decode("ascii"),
                policy_digest=policy.policy_digest,
                fallback_allowlist=list(self._options.fallback_allowlist),
                audit_context=request.audit_context,
            ),
        )
        return _public_mutation(result)

    async def update_connection(
        self, request: UpdateConnectionRequest
    ) -> ConnectionMutation:
        intent = UpdateConnectionIntent(
            name=request.name,
            config=request.config,
            secret_digest=self._fingerprint(request),
        )
        saved = await self._receipt(request, "update", intent)
        if saved is not None:
            return _public_mutation(saved)
        capture = await self._capture(request, "admin")
        if capture.platform_role not in {"super_admin", "project_admin"}:
            raise ConnectionFailure("manager_required")
        if capture.connection.revision != request.expected_revision:
            raise ConnectionFailure("connection_revision_conflict")
        current_config = dict(capture.connection.config)
        config = (
            validate_config(capture.connection.kind, request.config)
            if request.config is not None
            else dict(current_config)
        )
        config_changed = request.config is not None and config != current_config
        candidate_secret = request.secret
        if (
            candidate_secret is None
            and config_changed
            and capture.secret_enc is not None
        ):
            candidate_secret = self._decrypt(capture)
        if candidate_secret is not None:
            inferred = validate_secret(
                capture.connection.kind, candidate_secret, config
            )
            if inferred is not None and config.get("auth_type") is None:
                config["auth_type"] = inferred
        policy_digest = None
        policy = None
        entries: list[str] | None = None
        if request.config is not None:
            policy = await self._call(
                "read_storage_connection_policy",
                ConnectionPolicyRequest(
                    actor_id=request.actor_id,
                    fallback_allowlist=list(self._options.fallback_allowlist),
                ),
            )
            entries = await self._check_host(capture.connection.kind, config, policy)
            # The owner accepts a config candidate only when the digest it
            # re-verifies at publication accompanies it. That fence applies to
            # an identical config too: it is not a candidate change (no probe,
            # no location revision), but the accepted edit still advances the
            # connection revision.
            policy_digest = policy.policy_digest
        needs_validation = config_changed or request.secret is not None
        if needs_validation:
            if policy is None:
                policy = await self._call(
                    "read_storage_connection_policy",
                    ConnectionPolicyRequest(
                        actor_id=request.actor_id,
                        fallback_allowlist=list(self._options.fallback_allowlist),
                    ),
                )
                entries = await self._check_host(
                    capture.connection.kind, config, policy
                )
            assert entries is not None
            ok, message, _count = await probe_connection(
                capture.connection.kind,
                config,
                candidate_secret
                if candidate_secret is not None
                else self._decrypt(capture),
                allowlist=entries,
                known_hosts_file=self._options.known_hosts_file,
            )
            if not ok:
                raise ConnectionFailure("validation_failed", message)
            fresh = await self._call(
                "read_storage_connection_policy",
                ConnectionPolicyRequest(
                    actor_id=request.actor_id,
                    fallback_allowlist=list(self._options.fallback_allowlist),
                ),
            )
            if fresh.policy_digest != policy.policy_digest:
                raise ConnectionFailure("policy_changed")
        if needs_validation:
            # Re-read the actor and publication snapshot after the probe. No row
            # lock is held while network I/O runs, so a concurrent rename,
            # delete, deactivation or demotion must still fence this candidate.
            fresh_capture = await self._capture(request, "admin")
            if fresh_capture.platform_role not in {"super_admin", "project_admin"}:
                raise ConnectionFailure("manager_required")
            if fresh_capture.source_snapshot != capture.source_snapshot:
                raise ConnectionFailure("connection_revision_conflict")
            capture = fresh_capture
        # A nonempty accepted key must bind its full current scopes/expiry and
        # stored verifier revision; a caller cannot omit the proof.
        if request.api_key_id is not None and request.accepted_api_key_proof is None:
            raise ConnectionFailure("invalid_request")
        encrypted = (
            encrypt_secret(request.secret, self._options.encryption_key)
            if request.secret is not None
            else None
        )
        result = await self._call(
            "update_storage_connection_authorized",
            UpdateConnectionAuthorizedRequest(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                connection_id=request.connection_id,
                intent=intent,
                config=config,
                secret_enc=base64.b64encode(encrypted).decode("ascii")
                if encrypted is not None
                else None,
                source_snapshot=capture.source_snapshot,
                policy_digest=policy_digest,
                fallback_allowlist=list(self._options.fallback_allowlist),
                audit_context=request.audit_context,
                api_key_id=request.api_key_id,
                required_scopes=["datasets:write"]
                if request.api_key_id is not None
                else [],
                expected_proof=request.accepted_api_key_proof,
            ),
        )
        for name in ("id", "kind", "scope", "project_id", "created_by", "created_at"):
            if getattr(result.connection, name) != getattr(capture.connection, name):
                raise ConnectionFailure("invalid_response")
        if request.name is None and result.connection.name != capture.connection.name:
            raise ConnectionFailure("invalid_response")
        return _public_mutation(result)

    async def delete_connection(
        self, request: DeleteConnectionRequest
    ) -> ConnectionMutation:
        capture = await self._capture(request, "admin")
        if capture.platform_role not in {"super_admin", "project_admin"}:
            raise ConnectionFailure("manager_required")
        return _public_mutation(await self._call("delete_storage_connection", request))

    @staticmethod
    async def _run_probe(awaitable):
        """Keep responsibility for the blocking transport until it settles."""
        task = asyncio.create_task(awaitable)
        try:
            return await asyncio.shield(task)
        except BaseException:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except BaseException:
                    continue
            raise

    async def _finish_probe(self, request, lease, *, settled):
        try:
            await self._call(
                "finish_connection_probe",
                FinishConnectionProbeRequest(
                    operation_id=lease.operation_id,
                    connection_id=lease.connection_id,
                    actor_id=request.actor_id,
                    api_key_id=getattr(request, "api_key_id", None),
                    required_scopes=["datasets:read"]
                    if getattr(request, "api_key_id", None) is not None
                    else [],
                    expected_proof=lease.proof,
                    settled=settled,
                ),
            )
        except ConnectionFailure:
            # The ledger row is retained when the transport did not settle.
            if settled:
                raise

    async def test_connection(
        self, request: TestConnectionRequest
    ) -> ConnectionProbeReceipt:
        saved = await self._receipt(request, "test", EmptyConnectionIntent())
        if saved is not None:
            return saved
        capture = await self._capture(request, "use")
        policy = await self._call(
            "read_storage_connection_policy",
            ConnectionPolicyRequest(
                actor_id=request.actor_id,
                fallback_allowlist=list(self._options.fallback_allowlist),
            ),
        )
        entries = await self._check_host(
            capture.connection.kind, capture.connection.config, policy
        )
        secret = self._decrypt(capture)
        api_key_id = getattr(request, "api_key_id", None)
        required = ["datasets:read"] if api_key_id is not None else []
        lease = await self._call(
            "begin_connection_probe",
            BeginConnectionProbeRequest(
                connection_id=request.connection_id,
                actor_id=request.actor_id,
                api_key_id=api_key_id,
                required_scopes=required,
            ),
        )
        try:
            ok, message, count = await self._run_probe(
                probe_connection(
                    capture.connection.kind,
                    capture.connection.config,
                    secret,
                    allowlist=entries,
                    known_hosts_file=self._options.known_hosts_file,
                )
            )
        except BaseException as exc:
            await self._finish_probe(
                request,
                lease,
                settled=not getattr(exc, "storage_operation_unsettled", False),
            )
            raise
        await self._finish_probe(request, lease, settled=True)
        return await self._call(
            "record_storage_connection_test",
            RecordConnectionTestRequest(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                connection_id=request.connection_id,
                source_snapshot=capture.source_snapshot,
                policy_digest=policy.policy_digest,
                fallback_allowlist=list(self._options.fallback_allowlist),
                result=ConnectionProbeResult(
                    ok=ok, message=message, sample_count=count
                ),
                audit_context=request.audit_context,
            ),
        )

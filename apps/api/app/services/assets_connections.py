"""Bind storage-connection APIs to the Assets owner contract."""

from __future__ import annotations

import json
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.connections_contract import (
    CONNECTION_NATIVE_ARGUMENTS,
    CONNECTION_NATIVE_OPERATIONS,
    CONNECTION_NATIVE_SIGNATURES,
    CONNECTION_OPERATIONS,
    RUNTIME_NATIVE_ARGUMENTS,
    RUNTIME_NATIVE_SIGNATURES,
    CaptureConnectionRequest,
    ConnectionCapture,
    ConnectionFailure,
    ConnectionMutation,
    ConnectionPolicy,
    ConnectionPolicyRequest,
    ConnectionProbeFinish,
    ConnectionProbeLease,
    ConnectionProbeReceipt,
    ConnectionReceiptLookup,
    ConnectionReceiptRequest,
    ConnectionRead,
    ConnectionsPage,
    CreateConnectionNativeRequest,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsRequest,
    NativeConnectionMutation,
    NativeConnectionRead,
    NativeConnectionsPage,
    ReadConnectionRequest,
    RecordConnectionTestRequest,
    TestConnectionRequest,
    UpdateConnectionAuthorizedRequest,
    UpdateConnectionRequest,
    decode_connection_probe_reply,
    decode_connection_reply,
    require_connection_scope,
)
from app.config import settings

_RESULT = TypeVar("_RESULT", bound=BaseModel)
_MAX_REQUEST_BYTES = 1 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MUTATING_OPERATIONS = frozenset({"create", "update", "delete", "test"})

_FAILURES: dict[str, tuple[int, str, dict[str, str] | None]] = {
    "invalid_request": (422, "连接器请求无效", None),
    "account_not_active": (
        401,
        "账号已停用或不存在",
        {"WWW-Authenticate": "Bearer"},
    ),
    "account_busy": (409, "账号正在处理其他操作", None),
    "invalid_account_role": (403, "账号角色无效，访问被拒绝", None),
    "manager_required": (403, "仅项目管理员或超级管理员可创建连接器", None),
    "global_creation_forbidden": (403, "仅超级管理员可建全局连接器", None),
    "global_management_forbidden": (403, "仅超级管理员可管理全局连接器", None),
    "connection_management_forbidden": (
        403,
        "仅创建者或超级管理员可管理连接器",
        None,
    ),
    "connection_missing": (404, "连接器不存在", None),
    "connection_busy": (409, "连接器正忙，请稍后重试", None),
    "source_changed": (409, "连接器发生变更，请重试", None),
    "policy_changed": (409, "连接器白名单发生变更，请重试", None),
    "policy_busy": (409, "连接器白名单正忙，请重试", None),
    "policy_unavailable": (503, "连接器白名单不可用", None),
    "operation_conflict": (409, "Idempotency-Key 与之前的请求冲突", None),
    "validation_failed": (400, "连接器配置无效", None),
    "host_denied": (400, "连接器目标主机未通过白名单校验", None),
    "crypto_not_configured": (
        503,
        "CONNECTOR_ENCRYPTION_KEY 未配置，无法加解密连接器凭据",
        None,
    ),
    "crypto_error": (503, "连接器凭据加密或解密失败", None),
    "storage_unavailable": (503, "连接器存储暂不可用", None),
    "storage_maintenance": (409, {"code": "storage_maintenance"}, None),
    "storage_maintenance_changed": (
        409,
        {"code": "storage_maintenance_changed"},
        None,
    ),
    "operation_missing": (409, {"code": "operation_missing"}, None),
    "connection_revision_conflict": (
        409,
        {"code": "connection_revision_conflict"},
        None,
    ),
    "api_key_authority_revoked": (403, {"code": "api_key_authority_revoked"}, None),
    "service_unavailable": (503, "Assets source service is unavailable", None),
    "invalid_response": (502, "Assets source returned an invalid response", None),
    "resource_budget_exceeded": (422, "连接器请求超出资源预算", None),
}


class AssetsConnectionsGatewayError(HTTPException):
    """Stable public errors for local and remote owner failures."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        super().__init__(
            status_code=status_code,
            detail=message,
            headers=headers,
        )


def _peer_url() -> str | None:
    value = settings.assets_source_url
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    token = settings.assets_source_token
    if (
        not valid
        or len(token) < 32
        or not token.isascii()
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in token
        )
    ):
        raise AssetsConnectionsGatewayError(
            "service_unavailable", "Assets source service is unavailable", 503
        )
    return value.rstrip("/")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _encode_request(request: BaseModel) -> bytes:
    try:
        body = request.model_dump_json(exclude_unset=True).encode("utf-8")
    except (TypeError, ValueError, RecursionError, ValidationError) as exc:
        raise AssetsConnectionsGatewayError(
            "invalid_request", "连接器请求无效", 422
        ) from exc
    if len(body) > _MAX_REQUEST_BYTES:
        raise AssetsConnectionsGatewayError(
            "resource_budget_exceeded", "连接器请求超出资源预算", 422
        )
    return body


def _result_bytes(result: BaseModel) -> bytes:
    try:
        encoded = result.model_dump_json().encode("utf-8")
    except (TypeError, ValueError, ValidationError) as exc:
        raise AssetsConnectionsGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        ) from exc
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise AssetsConnectionsGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        )
    return encoded


async def _read_response(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length", "")
    if length.isdecimal() and int(length) > _MAX_RESPONSE_BYTES:
        raise AssetsConnectionsGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        )
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AssetsConnectionsGatewayError(
                "invalid_response", "Assets source returned an invalid response", 502
            )
    return bytes(body)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _raise_failure(exc: ConnectionFailure, *, operation: str) -> None:
    code = exc.code
    if operation == "update" and code in {
        "account_not_active",
        "manager_required",
        "connection_management_forbidden",
    }:
        raise AssetsConnectionsGatewayError(
            "access_revoked", {"code": "access_revoked"}, 403
        ) from None
    if operation == "update" and code == "validation_failed":
        message = (exc.message or "连接器配置无效") + "；原配置未修改"
        raise AssetsConnectionsGatewayError("validation_failed", message, 400) from None
    if code in {
        "connection_missing",
        "connection_management_forbidden",
    } and operation in {
        "read",
        "test",
    }:
        status_code, message, headers = 404, "连接器不存在", None
    else:
        status_code, message, headers = _FAILURES[code]
        if code in {"validation_failed", "host_denied"} and exc.message:
            message = exc.message
    raise AssetsConnectionsGatewayError(code, message, status_code, headers) from None


def _invalid_response(message: str = "Assets source returned an invalid response"):
    return AssetsConnectionsGatewayError("invalid_response", message, 502)


class AssetsConnectionsGateway:
    """Public connection API backed by the Assets workflow or native SQL port."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._peer = _peer_url()

    def _workflow(self):
        from aap_assets_source.connections_workflow import (
            ConnectionsOptions,
            ConnectionsWorkflow,
        )

        return ConnectionsWorkflow(
            native=self,
            options=ConnectionsOptions(
                encryption_key=settings.connector_encryption_key,
                fallback_allowlist=tuple(settings.connector_host_allowlist),
                known_hosts_file=None,
            ),
        )

    async def _remote(
        self, operation: str, request: BaseModel, result_type: type[_RESULT]
    ) -> _RESULT:
        assert self._peer is not None
        body = _encode_request(request)
        try:
            async with httpx.AsyncClient(
                timeout=settings.assets_source_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._peer}/v1/connections/{operation}",
                    headers={
                        "Authorization": f"Bearer {settings.assets_source_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    content=body,
                ) as response:
                    if response.status_code != 200:
                        if response.status_code >= 500 or response.status_code in {
                            401,
                            403,
                        }:
                            raise AssetsConnectionsGatewayError(
                                "service_unavailable",
                                "Assets source service is unavailable",
                                503,
                            )
                        raise _invalid_response()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise _invalid_response()
                    payload = json.loads(
                        await _read_response(response),
                        parse_constant=_reject_json_constant,
                    )
        except AssetsConnectionsGatewayError:
            raise
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _invalid_response() from None
        except (httpx.HTTPError, OSError) as exc:
            raise AssetsConnectionsGatewayError(
                "service_unavailable", "Assets source service is unavailable", 503
            ) from exc
        try:
            return decode_connection_reply(payload, result_type)
        except ConnectionFailure:
            raise
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise _invalid_response() from exc

    async def _invoke(
        self,
        operation: str,
        request: BaseModel,
        result_type: type[_RESULT],
        *,
        commit: bool = False,
    ) -> _RESULT:
        if CONNECTION_OPERATIONS.get(operation) != (type(request), result_type):
            raise TypeError(f"Invalid connection request/result pair for {operation}")
        _encode_request(request)
        local = self._peer is None
        try:
            if local:
                result = await self._workflow().invoke(operation, request)
            else:
                result = await self._remote(operation, request, result_type)
            if not isinstance(result, result_type):
                raise _invalid_response()
            try:
                require_connection_scope(request, result)
            except (TypeError, ValueError, KeyError) as exc:
                raise _invalid_response(
                    "Assets connection reply escaped request scope"
                ) from exc
            _result_bytes(result)
            if local and commit and operation in _MUTATING_OPERATIONS:
                await self.db.commit()
            return result
        except ConnectionFailure as exc:
            if local and operation in _MUTATING_OPERATIONS:
                await self.db.rollback()
            _raise_failure(exc, operation=operation)
        except AssetsConnectionsGatewayError:
            if local and operation in _MUTATING_OPERATIONS:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if local and operation in _MUTATING_OPERATIONS:
                await self.db.rollback()
            raise AssetsConnectionsGatewayError(
                "service_unavailable", "Assets source service is unavailable", 503
            ) from exc
        except Exception as exc:
            if local and operation in _MUTATING_OPERATIONS:
                await self.db.rollback()
            raise AssetsConnectionsGatewayError(
                "service_unavailable", "Assets source service is unavailable", 503
            ) from exc

    async def list_connections(self, actor_id) -> list:
        first_request = ListConnectionsRequest(actor_id=actor_id, page_size=200)
        cursor = None
        first_page: ConnectionsPage | None = None
        items = []
        seen: set[Any] = set()
        previous_key: tuple[Any, Any] | None = None
        while True:
            request = first_request.model_copy(update={"after": cursor})
            page = await self._invoke("list", request, ConnectionsPage)
            if first_page is None:
                first_page = page
            elif (page.snapshot_digest, page.total) != (
                first_page.snapshot_digest,
                first_page.total,
            ):
                raise _invalid_response("Assets connection list snapshot changed")
            if page.after != request.after:
                raise _invalid_response("Assets connection list cursor changed")
            if not page.connections and not page.complete:
                raise _invalid_response("Assets connection list did not advance")
            for connection in page.connections:
                identity = connection.id
                key = (connection.created_at, connection.id)
                if identity in seen or (
                    previous_key is not None and key >= previous_key
                ):
                    raise _invalid_response(
                        "Assets connection list is not unique and descending"
                    )
                seen.add(identity)
                previous_key = key
                items.append(connection)
                if len(items) > first_page.total:
                    raise _invalid_response("Assets connection list exceeds its total")
            if page.complete:
                if page.next_cursor is not None or len(items) != first_page.total:
                    raise _invalid_response("Assets connection list is incomplete")
                return items
            if (
                page.next_cursor is None
                or not page.connections
                or page.next_cursor.created_at != page.connections[-1].created_at
                or page.next_cursor.id != page.connections[-1].id
                or page.next_cursor == cursor
            ):
                raise _invalid_response("Assets connection list cursor did not advance")
            cursor = page.next_cursor

    async def read_connection(self, request: ReadConnectionRequest) -> ConnectionRead:
        return await self._invoke("read", request, ConnectionRead)

    async def create_connection(
        self, request: CreateConnectionRequest
    ) -> ConnectionMutation:
        return await self._invoke("create", request, ConnectionMutation, commit=True)

    async def update_connection(
        self, request: UpdateConnectionRequest
    ) -> ConnectionMutation:
        return await self._invoke("update", request, ConnectionMutation, commit=True)

    async def begin_connection_probe(self, request):
        return await self._runtime_call(
            "begin_connection_probe", request, ConnectionProbeLease
        )

    async def finish_connection_probe(self, request):
        return await self._runtime_call(
            "finish_connection_probe", request, ConnectionProbeFinish
        )

    async def _runtime_call(self, name, request, result_type):
        arguments = RUNTIME_NATIVE_ARGUMENTS[name]
        kinds = RUNTIME_NATIVE_SIGNATURES[name].split(",")
        placeholders = ", ".join(
            f"CAST(:{argument} AS {kind})"
            for argument, kind in zip(arguments, kinds, strict=True)
        )
        parameters = {}
        for argument, kind in zip(arguments, kinds, strict=True):
            value = getattr(request, argument)
            if kind == "jsonb":
                parameters[argument] = (
                    None
                    if value is None
                    else json.dumps(
                        _json_value(value), separators=(",", ":"), allow_nan=False
                    )
                )
            elif kind == "text[]":
                parameters[argument] = list(value)
            else:
                parameters[argument] = value
        statement = text(f"SELECT aap_assets_v1.{name}({placeholders})")
        if name in {"begin_connection_probe", "finish_connection_probe"}:
            # The ledger and the accepted-key fence are independently durable:
            # never hold the caller's publication transaction across physical I/O.
            from app.services.storage_access import engine

            async with engine(self.db).begin() as connection:
                payload = await connection.scalar(statement, parameters)
        else:
            payload = await self.db.scalar(statement, parameters)
        try:
            return decode_connection_probe_reply(payload, result_type)
        except ConnectionFailure:
            raise
        except (ValidationError, ValueError, TypeError):
            raise ConnectionFailure("invalid_response") from None

    async def delete_connection(
        self, request: DeleteConnectionRequest
    ) -> ConnectionMutation:
        return await self._invoke("delete", request, ConnectionMutation, commit=True)

    async def test_connection(
        self, request: TestConnectionRequest
    ) -> ConnectionProbeReceipt:
        return await self._invoke("test", request, ConnectionProbeReceipt, commit=True)

    async def _native_call(
        self,
        operation: str,
        request: BaseModel,
        arguments: tuple[Any, ...],
        result_type: type[_RESULT],
    ) -> _RESULT:
        if CONNECTION_NATIVE_OPERATIONS.get(operation) != (type(request), result_type):
            raise TypeError(
                f"Invalid native connection request/result pair for {operation}"
            )
        casts = CONNECTION_NATIVE_SIGNATURES[operation].split(",")
        names = CONNECTION_NATIVE_ARGUMENTS[operation]
        if len(arguments) != len(casts) or len(names) != len(casts):
            raise TypeError(f"Invalid native connection argument count for {operation}")
        placeholders = ", ".join(
            f"CAST(:{name} AS {type_name})"
            for name, type_name in zip(names, casts, strict=True)
        )
        parameters = {}
        for name, type_name, value in zip(names, casts, arguments, strict=True):
            # Optional jsonb arguments must stay SQL NULL. Encoding None as the
            # JSON literal null would reach the owner helpers as a present value
            # and turn an accepted request into invalid_request.
            if type_name != "jsonb" or value is None:
                parameters[name] = value
            else:
                parameters[name] = json.dumps(
                    _json_value(value), separators=(",", ":"), allow_nan=False
                )
        try:
            result = await self.db.execute(
                text(f"SELECT aap_assets_v1.{operation}({placeholders})"), parameters
            )
            raw = result.scalar_one()
            decoded = decode_connection_reply(raw, result_type)
            require_connection_scope(request, decoded)
            _result_bytes(decoded)
            return decoded
        except ConnectionFailure:
            raise
        except SQLAlchemyError:
            raise
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise ConnectionFailure("invalid_response") from exc

    async def list_storage_connections(
        self, request: ListConnectionsRequest
    ) -> NativeConnectionsPage:
        cursor = request.after
        return await self._native_call(
            "list_storage_connections",
            request,
            (
                request.actor_id,
                cursor.created_at if cursor else None,
                cursor.id if cursor else None,
                request.page_size,
            ),
            NativeConnectionsPage,
        )

    async def read_storage_connection(
        self, request: ReadConnectionRequest
    ) -> NativeConnectionRead:
        return await self._native_call(
            "read_storage_connection",
            request,
            (request.actor_id, request.connection_id),
            NativeConnectionRead,
        )

    async def read_storage_connection_policy(
        self, request: ConnectionPolicyRequest
    ) -> ConnectionPolicy:
        return await self._native_call(
            "read_storage_connection_policy",
            request,
            (request.actor_id, request.fallback_allowlist),
            ConnectionPolicy,
        )

    async def capture_storage_connection(
        self, request: CaptureConnectionRequest
    ) -> ConnectionCapture:
        return await self._native_call(
            "capture_storage_connection",
            request,
            (
                request.actor_id,
                request.connection_id,
                request.access,
            ),
            ConnectionCapture,
        )

    async def read_storage_connection_receipt(
        self, request: ConnectionReceiptRequest
    ) -> ConnectionReceiptLookup:
        return await self._native_call(
            "read_storage_connection_receipt",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.action,
                request.connection_id,
                request.intent,
            ),
            ConnectionReceiptLookup,
        )

    async def create_storage_connection(
        self, request: CreateConnectionNativeRequest
    ) -> NativeConnectionMutation:
        return await self._native_call(
            "create_storage_connection",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.intent,
                request.config,
                request.secret_enc,
                request.policy_digest,
                request.fallback_allowlist,
                request.audit_context,
            ),
            NativeConnectionMutation,
        )

    async def update_storage_connection_authorized(
        self, request: UpdateConnectionAuthorizedRequest
    ) -> NativeConnectionMutation:
        return await self._native_call(
            "update_storage_connection_authorized",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.connection_id,
                request.intent,
                request.config,
                request.secret_enc,
                request.source_snapshot,
                request.policy_digest,
                request.fallback_allowlist,
                request.audit_context,
                request.api_key_id,
                request.required_scopes,
                request.expected_proof,
            ),
            NativeConnectionMutation,
        )

    async def delete_storage_connection(
        self, request: DeleteConnectionRequest
    ) -> NativeConnectionMutation:
        return await self._native_call(
            "delete_storage_connection",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.connection_id,
                request.audit_context,
            ),
            NativeConnectionMutation,
        )

    async def record_storage_connection_test(
        self, request: RecordConnectionTestRequest
    ) -> ConnectionProbeReceipt:
        return await self._native_call(
            "record_storage_connection_test",
            request,
            (
                request.operation_id,
                request.actor_id,
                request.connection_id,
                request.source_snapshot,
                request.policy_digest,
                request.fallback_allowlist,
                request.result,
                request.audit_context,
            ),
            ConnectionProbeReceipt,
        )

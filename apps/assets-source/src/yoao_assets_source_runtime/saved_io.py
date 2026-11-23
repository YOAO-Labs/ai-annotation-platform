"""Registry, function-only SQL port, and bounded Source HTTP dispatch."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import hmac
from importlib import import_module
import json
import logging
import re
from tempfile import TemporaryFile
from uuid import UUID

from anyio import CancelScope
import asyncpg
from fastapi import Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, StreamingResponse

from yoao_assets_source.connectors.crypto import decrypt_secret
from yoao_assets_source.connectors.host_guard import normalize_allowlist
from yoao_assets_source.connectors.registry import (
    build_source_adapter,
    validate_source_path,
)
from yoao_assets_source.connectors.s3 import S3CompatibleSource
from yoao_assets_source.connectors.s3_socket import open_pinned_s3
from yoao_assets_source.saved_io import (
    ACTIONS,
    CHUNK_BYTES,
    JSON,
    OCTETS,
    REPLY_BYTES,
    SUBJECT_BYTES,
    TOKEN_SETTINGS,
    SavedIOError,
    decode_json,
    encode_json,
    encode_metadata,
    valid_token,
)
from .saved_io_storage import (
    Download,
    ManagedPrefix,
    PhysicalTarget,
    PinnedS3,
    managed_roots,
    physical_error,
    physical_thread,
    safe_text,
)

logger = logging.getLogger(__name__)
_CAPABILITY = re.compile(
    r"(yoao_[a-z0-9_]+_v1\.[a-z_][a-z0-9_]*)\(([a-z0-9_,\[\]]*)\)\Z"
)
_SQL_TYPES = frozenset(
    {
        "bigint",
        "boolean",
        "bytea",
        "integer",
        "json",
        "jsonb",
        "smallint",
        "text",
        "text[]",
        "timestamptz",
        "uuid",
        "uuid[]",
    }
)
_BASE = (
    "yoao_assets_v1.saved_io_connector(uuid,integer,jsonb)",
    "yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb)",
    "yoao_assets_v1.saved_io_allowlist(jsonb)",
)
_FAMILIES = ("binding", "reference", "outputs", "media", "training", "migration")


@dataclass(frozen=True)
class _Action:
    mode: str
    access: str
    handler: object


class Registry:
    def __init__(self):
        self.actions = {}
        self.signatures = {}
        self.statements = {}
        self.lifespans = []

    def lifespan(self, factory):
        self.lifespans.append(factory)

    def add(self, family, action, mode, handler, *, access):
        pair = family, action
        valid_modes = {
            "download": {"read"},
            "upload": {"write"},
            "command": TOKEN_SETTINGS.keys(),
        }
        if (
            any(
                not isinstance(part, str) or not ACTIONS.fullmatch(part)
                for part in pair
            )
            or mode not in valid_modes
            or (
                (mode, access) != ("download", "write")
                if pair == ("migration", "copy_get")
                else access not in valid_modes[mode]
            )
            or (access == "cutover" and pair != ("migration", "probe_cutover_target"))
            or not asyncio.iscoroutinefunction(handler)
            or pair in self.actions
        ):
            raise ValueError("Invalid or duplicate Source operation")
        self.actions[pair] = _Action(mode, access, handler)

    def allow(self, signature):
        match = _CAPABILITY.fullmatch(signature)
        if match is None:
            raise ValueError("Invalid Source SQL capability")
        name, arguments = match.groups()
        types = arguments.split(",") if arguments else []
        if name in self.signatures or not set(types) <= _SQL_TYPES:
            raise ValueError("Duplicate or ill-typed Source SQL capability")
        parameters = ",".join(f"${i}::{kind}" for i, kind in enumerate(types, 1))
        self.signatures[name] = signature
        self.statements[name] = f"SELECT {name}({parameters})"


def _registry():
    result = Registry()
    for signature in _BASE:
        result.allow(signature)
    for family in _FAMILIES:
        import_module(f".saved_io_{family}", __package__).register(result)
        if not any(name == family for name, _ in result.actions):
            raise ValueError("Missing Source operation family")
    return result


_PREFLIGHT = """
WITH app_schema AS (
    SELECT oid, nspname FROM pg_catalog.pg_namespace
    WHERE nspname NOT IN ('pg_catalog', 'information_schema')
      AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
), expected AS (
    SELECT pg_catalog.to_regprocedure(signature)::oid AS oid
    FROM pg_catalog.unnest($1::text[]) AS args(signature)
), executable AS (
    SELECT proc.oid
    FROM pg_catalog.pg_proc proc JOIN app_schema ns ON ns.oid = proc.pronamespace
    WHERE ns.nspname ~ '^(yoao|aap)_[a-z0-9_]+_v1$'
      AND pg_catalog.has_schema_privilege(current_user, ns.oid, 'USAGE')
      AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')
)
SELECT current_user = session_user AND r.rolcanlogin
    AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
    AND NOT pg_catalog.has_database_privilege(current_user, current_database(), 'CREATE')
    AND NOT EXISTS (SELECT FROM pg_catalog.pg_auth_members WHERE member = r.oid OR roleid = r.oid)
    AND NOT EXISTS (
        SELECT FROM app_schema WHERE pg_catalog.has_schema_privilege(current_user, oid, 'CREATE')
           OR (nspname ~ '^(yoao|aap)_[a-z0-9_]+_v1$' AND nspname <> ALL($2::text[])
               AND pg_catalog.has_schema_privilege(current_user, oid, 'USAGE'))
    )
    AND NOT EXISTS (
        SELECT FROM pg_catalog.pg_class c JOIN app_schema ns ON ns.oid = c.relnamespace
        WHERE (c.relkind IN ('r','p','v','m','f') AND (
            pg_catalog.has_table_privilege(current_user, c.oid,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR pg_catalog.has_any_column_privilege(current_user, c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')
        )) OR (c.relkind = 'S' AND pg_catalog.has_sequence_privilege(current_user, c.oid, 'USAGE,SELECT,UPDATE'))
    )
    AND NOT EXISTS (
        SELECT FROM expected e LEFT JOIN pg_catalog.pg_proc p ON p.oid = e.oid
        WHERE p.oid IS NULL OR NOT p.prosecdef OR p.proowner = r.oid
    )
    AND ARRAY(SELECT oid FROM expected ORDER BY oid) = ARRAY(SELECT oid FROM executable ORDER BY oid)
FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
"""


async def _init_connection(connection):
    for kind in ("json", "jsonb"):
        await connection.set_type_codec(
            kind, schema="pg_catalog", encoder=json.dumps, decoder=json.loads
        )


class Context:
    """A handler must prove business ownership before requesting physical material."""

    def __init__(self, service):
        self.config = service.config
        self._service = service

    async def fetch(self, capability, *args):
        statement = self._service.registry.statements.get(capability)
        if statement is None:
            raise RuntimeError("Unregistered Source capability")
        async with self._service.pool.acquire(
            timeout=self.config.pool_timeout_seconds
        ) as connection:
            return await connection.fetchval(statement, *args)

    run = staticmethod(physical_thread)

    def download(
        self, body, *, length, metadata, close=None, after_body=None, after_close=None
    ):
        return Download(
            body,
            length=length,
            metadata=metadata,
            close=close,
            after_body=after_body,
            after_close=after_close,
        )

    def _policy(self, value):
        if not isinstance(value, list):
            raise SavedIOError("policy_unavailable", status_code=503)
        try:
            return tuple(normalize_allowlist(value))
        except ValueError:
            raise SavedIOError("policy_unavailable", status_code=503) from None

    async def connector_allowlist(self):
        return self._policy(
            await self.fetch(
                "yoao_assets_v1.saved_io_allowlist",
                list(self.config.connector_host_allowlist),
            )
        )

    def _credentials(self, row):
        if not isinstance(row, dict):
            raise SavedIOError("target_unproven")
        config = row.get("config")
        if not isinstance(config, dict) or row.get("kind") not in {"s3", "sftp"}:
            raise SavedIOError("target_unproven")
        try:
            secret = decrypt_secret(
                bytes.fromhex(row["ciphertext"]), self.config.connector_encryption_key
            )
        except Exception:
            raise SavedIOError("connection_unavailable", status_code=503) from None
        return row["kind"], config, secret, self._policy(row.get("allowlist"))

    async def connector_credentials(self, connection_id, connection_revision):
        if type(connection_revision) is not int or connection_revision < 1:
            raise SavedIOError("target_unproven")
        return self._credentials(
            await self.fetch(
                "yoao_assets_v1.saved_io_connector",
                UUID(str(connection_id)),
                connection_revision,
                list(self.config.connector_host_allowlist),
            )
        )

    @asynccontextmanager
    async def reference(
        self,
        connection_id,
        connection_revision,
        *,
        path=None,
        require_conditional_reads=False,
    ):
        kind, config, secret, allowlist = await self.connector_credentials(
            connection_id, connection_revision
        )

        def open_adapter():
            if kind == "s3":
                if path is not None:
                    validate_source_path(kind, config, path)
                client = open_pinned_s3(
                    config,
                    secret,
                    allowlist,
                    connect_timeout=self.config.s3_connect_timeout_seconds,
                    read_timeout=self.config.s3_read_timeout_seconds,
                )
                try:
                    return S3CompatibleSource(
                        config,
                        secret,
                        client=client,
                        require_conditional_reads=require_conditional_reads,
                    )
                except BaseException:
                    client.close()
                    raise
            return build_source_adapter(
                kind,
                config,
                secret,
                allowlist,
                source_path=path,
                known_hosts_file=self.config.sftp_known_hosts_file,
                require_conditional_reads=require_conditional_reads,
                pin_transport=True,
            )

        def close_adapter(adapter):
            if isinstance(adapter, S3CompatibleSource):
                adapter.client.close()
            else:
                adapter.close()

        adapter = await self.run(open_adapter, _discard=close_adapter)
        try:
            yield adapter
        finally:
            try:
                await self.run(close_adapter, adapter)
            except Exception:
                raise SavedIOError(
                    "source_close_unsettled", status_code=503, unsettled=True
                ) from None

    async def _managed_target(
        self, location_id, config_revision, connection_revision, purpose, bucket, scope
    ):
        if (
            type(config_revision) is not int
            or config_revision < 1
            or type(connection_revision) is not int
            or connection_revision < 1
            or not safe_text(scope, 1024)
        ):
            raise SavedIOError("target_unproven")
        row = await self.fetch(
            "yoao_assets_v1.saved_io_target",
            UUID(str(location_id)),
            config_revision,
            connection_revision,
            list(self.config.connector_host_allowlist),
        )
        if (
            not isinstance(row, dict)
            or row.get("kind") != "s3"
            or not isinstance(row.get("config"), dict)
        ):
            raise SavedIOError("target_unproven")
        if bucket != row["config"].get("bucket") or not any(
            scope.startswith(root) for root in managed_roots(row["config"], purpose)
        ):
            raise SavedIOError("target_unproven")
        _, config, secret, allowlist = self._credentials(row)
        return PhysicalTarget(bucket, self.config, config, secret, allowlist)

    async def s3(
        self,
        location_id,
        config_revision,
        connection_revision,
        purpose,
        bucket,
        key,
        *,
        version=None,
    ):
        target = await self._managed_target(
            location_id, config_revision, connection_revision, purpose, bucket, key
        )
        return PinnedS3(target, key, version)

    @asynccontextmanager
    async def managed(
        self, location_id, config_revision, connection_revision, purpose, bucket, prefix
    ):
        target = await self._managed_target(
            location_id, config_revision, connection_revision, purpose, bucket, prefix
        )
        async with target.client() as client:
            yield ManagedPrefix(target, prefix, client)

    def builtin(self, purpose, key, *, version=None):
        bucket = {
            "annotations": self.config.annotations_bucket,
            "datasets": self.config.datasets_bucket,
            "import": self.config.import_bucket,
            "media-cache": self.config.media_cache_bucket,
        }.get(purpose)
        if not bucket:
            raise SavedIOError("source_unavailable", status_code=503)
        return PinnedS3(PhysicalTarget(bucket, self.config), key, version)

    async def probe(
        self, location_id, config_revision, connection_revision, purpose, bucket, prefix
    ):
        from .saved_io_probe import probe

        target = await self._managed_target(
            location_id, config_revision, connection_revision, purpose, bucket, prefix
        )
        if prefix not in managed_roots(target.config, purpose):
            raise SavedIOError("target_unproven")
        async with target.client() as client:
            try:
                return await self.run(probe, client, bucket, prefix, purpose)
            except Exception as exc:
                raise physical_error(exc) from None


class _RequestBody:
    def __init__(self, request):
        self._stream = request.stream().__aiter__()
        self._pending = memoryview(b"")

    async def read(self, length):
        result = bytearray()
        while len(result) < length:
            if not self._pending:
                try:
                    self._pending = memoryview(await anext(self._stream))
                except StopAsyncIteration:
                    break
            count = min(length - len(result), len(self._pending))
            result.extend(self._pending[:count])
            self._pending = self._pending[count:]
        return result

    async def exact(self, length):
        result = await self.read(length)
        if len(result) != length:
            raise SavedIOError("upload_length_mismatch", status_code=400)
        return result


def _content_length(request):
    lengths = request.headers.getlist("content-length")
    if not lengths:
        return None
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,16}", lengths[0]):
        raise SavedIOError("invalid_request", status_code=400)
    return int(lengths[0])


def _subject(payload):
    try:
        return decode_json(payload)
    except (ValueError, RecursionError):
        raise SavedIOError("invalid_request", status_code=400) from None


def _failure(error):
    return JSONResponse(
        status_code=error.status_code,
        content={
            "code": error.code,
            "storage_code": error.storage_code,
            "unsettled": error.unsettled,
        },
        headers={"Cache-Control": "no-store"},
    )


class _Stream(StreamingResponse):
    def __init__(self, download, headers, release):
        self.download = download
        self.release = release
        super().__init__(self._chunks(), media_type=OCTETS, headers=headers)

    async def _chunks(self):
        remaining = self.download.length
        try:
            while remaining:
                limit = min(remaining, CHUNK_BYTES)
                chunk = await physical_thread(self.download.body.read, limit)
                if not isinstance(chunk, bytes) or not 0 < len(chunk) <= limit:
                    raise SavedIOError(
                        "download_interrupted", status_code=502, unsettled=True
                    )
                remaining -= len(chunk)
                if remaining == 0:
                    await _complete_download(self.download)
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception:
            raise SavedIOError(
                "download_interrupted", status_code=502, unsettled=True
            ) from None

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                try:
                    await _close_download(self.download)
                finally:
                    self.release()


async def _complete_download(download):
    if await physical_thread(download.body.read, 1):
        raise SavedIOError("download_length_mismatch", status_code=502, unsettled=True)
    await _close_download(download)
    if download.after_body is not None:
        await download.after_body()


async def _close_download(download):
    await physical_thread(download.close)
    callback = download.after_close
    if callback is not None:
        download.after_close = None
        await callback()


class SavedIOService:
    def __init__(self, config, registry, pool):
        self.config = config
        self.registry = registry
        self.pool = pool
        self._lifespan = AsyncExitStack()
        self._upload_slot = asyncio.Semaphore(1)
        self._requests = asyncio.Semaphore(config.pool_max_size)

    @classmethod
    async def start(cls, config):
        if config.saved_io is None:
            return None
        registry = _registry()
        pool = await asyncpg.create_pool(
            dsn=config.saved_io.database_url,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            timeout=config.pool_timeout_seconds,
            command_timeout=config.sql_timeout_seconds,
            init=_init_connection,
            server_settings={"search_path": "pg_catalog,pg_temp"},
        )
        service = cls(config, registry, pool)
        try:
            signatures = sorted(registry.signatures.values())
            schemas = sorted({signature.split(".", 1)[0] for signature in signatures})
            async with pool.acquire(timeout=config.pool_timeout_seconds) as connection:
                if (
                    await connection.fetchval(_PREFLIGHT, signatures, schemas)
                    is not True
                ):
                    raise RuntimeError(
                        "Saved I/O login exceeds or lacks the fixed SQL capability set"
                    )
            context = Context(service)
            for factory in registry.lifespans:
                await service._lifespan.enter_async_context(factory(context))
            return service
        except BaseException:
            await service.close()
            raise

    async def ready(self):
        async with self.pool.acquire(
            timeout=self.config.pool_timeout_seconds
        ) as connection:
            return await connection.fetchval("SELECT 1") == 1

    async def close(self):
        try:
            await self._lifespan.aclose()
        finally:
            await self.pool.close()

    async def _upload(self, request, action, length, context):
        if length is None or length < 6:
            raise SavedIOError("invalid_request", status_code=400)
        max_bytes = self.config.saved_io.max_upload_bytes
        if length > max_bytes + SUBJECT_BYTES + 4:
            raise SavedIOError("request_too_large", status_code=413)
        await asyncio.wait_for(
            self._upload_slot.acquire(), self.config.pool_timeout_seconds
        )
        try:
            reader = _RequestBody(request)
            idle = self.config.s3_read_timeout_seconds
            subject_length = int.from_bytes(
                await asyncio.wait_for(reader.exact(4), idle), "big"
            )
            if not 2 <= subject_length <= min(SUBJECT_BYTES, length - 4):
                raise SavedIOError("invalid_request", status_code=400)
            remaining = length - 4 - subject_length
            if remaining > max_bytes:
                raise SavedIOError("request_too_large", status_code=413)
            subject = _subject(
                await asyncio.wait_for(reader.exact(subject_length), idle)
            )
            with TemporaryFile(mode="w+b", dir=self.config.saved_io.spool_dir) as spool:
                while remaining:
                    chunk = await asyncio.wait_for(
                        reader.exact(min(CHUNK_BYTES, remaining)), idle
                    )
                    await physical_thread(spool.write, chunk)
                    remaining -= len(chunk)
                if await asyncio.wait_for(reader.read(1), idle):
                    raise SavedIOError("upload_length_mismatch", status_code=400)
                spool.seek(0)
                return await action.handler(context, subject, spool)
        finally:
            self._upload_slot.release()

    async def invoke(self, request: Request, family, name):
        action = self.registry.actions.get((family, name))
        if action is None:
            return _failure(SavedIOError("operation_unknown", status_code=404))
        expected = self.config.saved_io.tokens.get(action.access)
        if not expected:
            return _failure(SavedIOError("source_unavailable", status_code=503))
        authorization = request.headers.getlist("authorization")
        scheme, _, token = (
            authorization[0] if len(authorization) == 1 else ""
        ).partition(" ")
        if (
            scheme.casefold() != "bearer"
            or not valid_token(token)
            or not hmac.compare_digest(token, expected)
        ):
            return _failure(SavedIOError("access_denied", status_code=401))
        expected_type = OCTETS if action.mode == "upload" else JSON
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != expected_type
        ):
            return _failure(SavedIOError("invalid_request", status_code=415))
        if request.headers.get("content-encoding", "identity") != "identity":
            return _failure(SavedIOError("invalid_request", status_code=415))
        result = None
        acquired = handed_off = dispatched = False
        try:
            await asyncio.wait_for(
                self._requests.acquire(), self.config.pool_timeout_seconds
            )
            acquired = True
            length = _content_length(request)
            context = Context(self)
            if action.mode == "upload":
                dispatched = True
                result = await self._upload(request, action, length, context)
            else:
                limit = min(SUBJECT_BYTES, self.config.max_request_bytes)
                if length is not None and length > limit:
                    raise SavedIOError("request_too_large", status_code=413)
                data = await asyncio.wait_for(
                    _RequestBody(request).read(limit + 1),
                    self.config.s3_read_timeout_seconds,
                )
                if len(data) > limit:
                    raise SavedIOError("request_too_large", status_code=413)
                subject = _subject(data)
                dispatched = True
                result = await action.handler(context, subject)
            if action.mode != "download":
                if type(result) is not dict:
                    raise ValueError("Expected command result")
                reply = encode_json(result)
                if len(reply) > min(self.config.max_response_bytes, REPLY_BYTES):
                    raise ValueError("Oversized command result")
                return Response(
                    reply, media_type=JSON, headers={"Cache-Control": "no-store"}
                )
            if (
                not isinstance(result, Download)
                or type(result.length) is not int
                or result.length < 0
            ):
                raise ValueError("Invalid pinned download")
            headers = {
                "Content-Length": str(result.length),
                "Cache-Control": "no-store",
                "X-Yoao-Source-Metadata": encode_metadata(result.metadata),
            }
            if not result.length:
                await _complete_download(result)
                return Response(b"", media_type=OCTETS, headers=headers)
            reply = _Stream(result, headers, self._requests.release)
            handed_off = True
            return reply
        except SavedIOError as exc:
            return _failure(exc)
        except (ClientDisconnect, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.error("Saved Source operation failed (%s)", type(exc).__name__)
            return _failure(
                SavedIOError(
                    "source_unavailable",
                    status_code=503,
                    unsettled=bool(getattr(exc, "storage_operation_unsettled", False))
                    or dispatched
                    and action.access != "read",
                )
            )
        finally:
            if not handed_off:
                try:
                    if isinstance(result, Download):
                        with CancelScope(shield=True):
                            await _close_download(result)
                finally:
                    if acquired:
                        self._requests.release()


async def dispatch(service, request, family, action):
    if service is None:
        return _failure(SavedIOError("source_unavailable", status_code=503))
    return await service.invoke(request, family, action)

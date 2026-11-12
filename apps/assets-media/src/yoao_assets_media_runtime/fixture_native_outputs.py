"""Exact native cache writes through a per-run private control connection."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from urllib.parse import unquote, urlsplit
import uuid

_active = ContextVar("native_fixture_cache_writer", default=None)
_execution = ContextVar("native_fixture_execution", default=None)
_ORDINARY = object()
_ROLE = re.compile(r"aap_fixture_media_[0-9a-f]{32}")


class FixtureNativeRunSealed(Exception):
    """An exact admitted scan was fenced before its next transaction began."""


def current():
    """Return the fixture writer, or None only for an admitted ordinary output."""
    output = _active.get()
    if output is _ORDINARY:
        return None
    if output is None:
        raise RuntimeError("native output authority is unavailable")
    return output


def _dsn():
    name = os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE")
    if not name or not Path(name).is_absolute():
        raise RuntimeError("fixture native cache control is unavailable")
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise RuntimeError("fixture native cache control is unavailable")
            value = stream.read(8193).strip()
        if len(value) > 8192 or not value.startswith("postgresql+asyncpg://"):
            raise RuntimeError("fixture native cache control is unavailable")
        value = "postgresql://" + value.removeprefix("postgresql+asyncpg://")
        parsed = urlsplit(value)
        parsed.port
        if (
            parsed.scheme != "postgresql"
            or not parsed.hostname
            or not parsed.username
            or not _ROLE.fullmatch(unquote(parsed.username))
            or not parsed.password
            or not parsed.path.startswith("/")
            or "/" in parsed.path[1:]
            or not parsed.path.lower().endswith(("_test", "_e2e"))
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("fixture native cache control is unavailable")
        return value
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError("fixture native cache control is unavailable") from None


async def _verified_connection(business, dsn=None):
    import asyncpg

    connection = await asyncpg.connect(dsn if dsn is not None else _dsn(), command_timeout=30)
    try:
        database = await connection.fetchval("SELECT current_database()")
        if not database.lower().endswith(("_test", "_e2e")):
            raise RuntimeError("fixture native cache database is not isolated")
        if await business.fetchval("SELECT current_database()") != database:
            raise RuntimeError("fixture native cache database changed")
        key = secrets.randbits(63)
        await business.fetchval("SELECT pg_catalog.pg_advisory_lock($1::bigint)", key)
        try:
            other = await connection.fetchval(
                "SELECT pg_catalog.pg_try_advisory_lock($1::bigint)", key
            )
            if other:
                await connection.fetchval(
                    "SELECT pg_catalog.pg_advisory_unlock($1::bigint)", key
                )
                raise RuntimeError("fixture native cache database changed")
        finally:
            if not await business.fetchval(
                "SELECT pg_catalog.pg_advisory_unlock($1::bigint)", key
            ):
                raise RuntimeError("fixture native cache lock was lost")
    except BaseException:
        await connection.close()
        raise
    return connection


def load_target_file(selector, database_url, role_prefix):
    if not selector or not Path(selector).is_absolute():
        raise RuntimeError("fixture native output target is unavailable")
    try:
        descriptor = os.open(selector, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("invalid target file")
            encoded = stream.read(8193)
        if len(encoded) > 8192:
            raise ValueError("invalid target file")
        target = json.loads(encoded)
        fields = {
            "schema", "run_id", "endpoint", "bucket", "storage_identity",
            "receipt_sha256", "access_key", "secret_key",
        }
        if not isinstance(target, dict) or set(target) != fields:
            raise ValueError("invalid target file")
        run_id = uuid.UUID(target["run_id"])
        endpoint = urlsplit(target["endpoint"])
        role = unquote(urlsplit(database_url).username or "")
        if (
            target["schema"] != "aap-fixture-native-output-target/v1"
            or str(run_id) != target["run_id"]
            or role != f"{role_prefix}{run_id.hex}"
            or endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path
            or endpoint.query
            or endpoint.fragment
            or target["bucket"] != f"fixture-media-{run_id.hex}"
            or not all(isinstance(target[name], str) and target[name] for name in (
                "access_key", "secret_key",
            ))
            or not isinstance(target["receipt_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", target["receipt_sha256"]) is None
            or target["storage_identity"] != hashlib.sha256(
                json.dumps([target["endpoint"], target["bucket"]]).encode()
            ).hexdigest()
        ):
            raise ValueError("invalid target file")
        endpoint.port
        return target
    except (OSError, ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        raise RuntimeError("fixture native output target is unavailable") from None


def load_target(selector_name, database_url, role_prefix):
    return load_target_file(os.environ.get(selector_name), database_url, role_prefix)


def verify_object(client, bucket, key, digest, size, etag, version):
    """Verify the immutable version by authenticated HEAD and complete GET."""
    if not isinstance(version, str) or not version or version == "null":
        raise RuntimeError("fixture native output version is unavailable")
    params = {"Bucket": bucket, "Key": key, "VersionId": version}
    head = client.head_object(**params, IfMatch=etag)
    if (
        head.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or head.get("ETag") != etag
        or head.get("VersionId") != version
        or head.get("ContentLength") != size
        or head.get("Metadata", {}).get("sha256") != digest
    ):
        raise RuntimeError("fixture native output identity changed")
    response = client.get_object(**params, IfMatch=etag)
    body = response["Body"]
    observed, count = hashlib.sha256(), 0
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > size:
                raise RuntimeError("fixture native output content changed")
            observed.update(chunk)
    finally:
        failure = sys.exception()
        try:
            body.close()
        except BaseException as close_error:
            close_error.storage_operation_unsettled = True
            if failure is not None:
                combined = builtins.BaseExceptionGroup(
                    "Fixture native output verification and close failed",
                    [failure, close_error],
                )
                combined.storage_operation_unsettled = True
                raise combined from None
            raise
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ETag") != etag
        or response.get("VersionId") != version
        or response.get("ContentLength") != size
        or count != size
        or observed.hexdigest() != digest
    ):
        raise RuntimeError("fixture native output content changed")


async def native_producer_ready(database, family):
    """Validate the process's private target and same-server execution identity."""
    dsn = _dsn()
    target = load_target("AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_TARGET_FILE", dsn, "aap_fixture_media_")
    run_id = uuid.UUID(target["run_id"])
    async with database.fixture_business_connection() as business:
        connection = await _verified_connection(business, dsn)
    try:
        observed = await connection.fetchval(
            "SELECT aap_assets_v1.fixture_native_producer_ready($1::uuid)", run_id
        )
        if isinstance(observed, str):
            observed = json.loads(observed)
        if (
            not isinstance(observed, dict)
            or set(observed) != {
                "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
                "database_oid", "database_owner_marker",
            }
            or any(observed[name] != target[name] for name in (
                "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
            ))
            or type(observed["database_oid"]) is not int
            or observed["database_oid"] <= 0
            or not isinstance(observed["database_owner_marker"], str)
            or not observed["database_owner_marker"]
        ):
            raise RuntimeError("fixture native producer identity changed")
        values = await connection.fetchval(
            "SELECT aap_assets_v1.fixture_native_job_candidates($1::text)", family
        )
        if not isinstance(values, list):
            raise RuntimeError("fixture native queue changed")
    finally:
        await connection.close()
    return run_id


async def native_job_candidates(database, family):
    """Read only this private principal's durable queue; ordinary workers never call it."""
    if not os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE"):
        return None
    async with database.fixture_business_connection() as business:
        connection = await _verified_connection(business)
    try:
        values = await connection.fetchval(
            "SELECT aap_assets_v1.fixture_native_job_candidates($1::text)", family
        )
        if not isinstance(values, list) or any(not isinstance(value, uuid.UUID) for value in values):
            raise RuntimeError("fixture native queue changed")
        return values
    finally:
        await connection.close()


@asynccontextmanager
async def native_execution(database, family, job_id):
    """Hold a private execution until its real worker and heartbeat have returned."""
    if not os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE"):
        # The business claim gates this marker: fixture-native jobs require a
        # live PID-bound admission, including during untargeted polling.
        token = _execution.set((_ORDINARY, None, family, job_id))
        try:
            yield True
        finally:
            _execution.reset(token)
        return
    if job_id is None:
        raise RuntimeError("fixture native execution job is missing")
    async with database.fixture_business_connection() as business:
        connection = await _verified_connection(business)
    execution_id = uuid.uuid4()
    label = f"aap-fixture-native-execution:{execution_id}"
    locked = False
    token = None
    try:
        await connection.fetchval(
            "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended($1,0))", label
        )
        locked = True
        outcome = await connection.fetchval(
            "SELECT aap_assets_v1.begin_fixture_native_execution($1::text,$2::uuid,$3::uuid)",
            family, job_id, execution_id,
        )
        if outcome in {"blocked", "ordinary"}:
            yield False
            return
        if outcome != "started":
            raise RuntimeError("fixture native execution admission changed")
        token = _execution.set((connection, execution_id, family, job_id))
        try:
            yield True
        except BaseException as exc:
            exc.storage_operation_unsettled = True
            raise
        else:
            if await connection.fetchval(
                "SELECT aap_assets_v1.finish_fixture_native_execution($1::uuid)",
                execution_id,
            ) is not True:
                error = RuntimeError("fixture native execution is unsettled")
                error.storage_operation_unsettled = True
                raise error
    finally:
        if token is not None:
            _execution.reset(token)
        failure = sys.exception()

        async def cleanup():
            errors = []
            if locked:
                try:
                    if await connection.fetchval(
                        "SELECT pg_catalog.pg_advisory_unlock(pg_catalog.hashtextextended($1,0))",
                        label,
                    ) is not True:
                        raise RuntimeError("fixture native execution lock was lost")
                except BaseException as exc:
                    errors.append(exc)
            try:
                await connection.close()
            except BaseException as exc:
                errors.append(exc)
            if errors:
                raise builtins.BaseExceptionGroup("Native execution cleanup failed", errors)

        task = asyncio.create_task(cleanup())
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except BaseException:
                break
        try:
            task.result()
        except BaseException as error:
            error.storage_operation_unsettled = True
            if failure is not None:
                combined = builtins.BaseExceptionGroup("Native execution and cleanup failed", [failure, error])
                combined.storage_operation_unsettled = True
                raise combined from None
            raise
        if cancelled is not None:
            cancelled.storage_operation_unsettled = True
            if failure is not None:
                combined = builtins.BaseExceptionGroup("Native execution cancelled while closing", [failure, cancelled])
                combined.storage_operation_unsettled = True
                raise combined from None
            raise cancelled


@asynccontextmanager
async def _business_admission(business, family, request, *, scan):
    """Fence a committed claim or scan to its private execution and business PID."""
    execution = _execution.get()
    if execution is None or execution[0] is _ORDINARY:
        yield
        return
    connection, execution_id, expected_family, job_id = execution
    if family != expected_family or request.job_id != job_id:
        raise RuntimeError("fixture native claim scope changed")
    token = uuid.uuid4()
    label = f"aap-fixture-native-source:{token}"
    locked = admitted = False
    try:
        pid = await business.fetchval("SELECT pg_catalog.pg_backend_pid()")
        await business.fetchval(
            "SELECT pg_catalog.pg_advisory_lock_shared(pg_catalog.hashtextextended($1,0))", label
        )
        locked = True
        if scan:
            accepted = await connection.fetchval(
                "SELECT aap_assets_v1.begin_fixture_native_scan("
                "$1::uuid,$2::uuid,$3::bigint,$4::uuid,$5::integer,$6::uuid)",
                execution_id, job_id, request.generation, request.lease_token, pid, token,
            )
            if accepted == "sealed":
                raise FixtureNativeRunSealed()
            accepted = accepted == "started"
        else:
            accepted = await connection.fetchval(
                "SELECT aap_assets_v1.begin_fixture_native_claim($1::uuid,$2::integer,$3::uuid)",
                execution_id, pid, token,
            )
        if accepted is not True:
            raise RuntimeError("fixture native business admission denied")
        admitted = True
        yield
        finish = "finish_fixture_native_scan" if scan else "finish_fixture_native_claim"
        if await connection.fetchval(
            f"SELECT aap_assets_v1.{finish}($1::uuid,$2::uuid)",
            execution_id, token,
        ) is not True:
            raise RuntimeError("fixture native claim is unsettled")
    except BaseException as exc:
        cause = getattr(exc, "orig", exc)
        if admitted and scan and getattr(cause, "sqlstate", None) == "APF01":
            try:
                finished = await connection.fetchval(
                    "SELECT aap_assets_v1.finish_fixture_native_scan($1::uuid,$2::uuid)",
                    execution_id, token,
                )
                if finished is not True:
                    raise RuntimeError("fixture native scan is unsettled")
            except BaseException as finish_error:
                finish_error.storage_operation_unsettled = True
                raise finish_error from exc
            admitted = False
            raise FixtureNativeRunSealed() from exc
        if admitted:
            exc.storage_operation_unsettled = True
        raise
    finally:
        if locked:
            task = asyncio.create_task(business.fetchval(
                "SELECT pg_catalog.pg_advisory_unlock_shared(pg_catalog.hashtextextended($1,0))",
                label,
            ))
            failure = sys.exception()
            cancelled = None
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except BaseException:
                    break
            try:
                if task.result() is not True:
                    raise RuntimeError("fixture native claim lock was lost")
            except BaseException as error:
                error.storage_operation_unsettled = True
                if failure is not None:
                    combined = builtins.BaseExceptionGroup("Native claim and unlock failed", [failure, error])
                    combined.storage_operation_unsettled = True
                    raise combined from None
                raise
            if cancelled is not None:
                cancelled.storage_operation_unsettled = True
                if failure is not None:
                    combined = builtins.BaseExceptionGroup("Native claim cancelled while unlocking", [failure, cancelled])
                    combined.storage_operation_unsettled = True
                    raise combined from None
                raise cancelled

def claim_admission(business, family, request):
    return _business_admission(business, family, request, scan=False)


def scan_admission(business, family, request):
    return _business_admission(business, family, request, scan=True)



@asynccontextmanager
async def source_admission(business, family, request):
    if not os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE"):
        yield
        return
    connection = None
    locked = admitted = False
    token = uuid.uuid4()
    label = f"aap-fixture-native-source:{token}"
    try:
        connection = await _verified_connection(business)
        pid = await business.fetchval("SELECT pg_catalog.pg_backend_pid()")
        await business.fetchval(
            "SELECT pg_catalog.pg_advisory_lock_shared(pg_catalog.hashtextextended($1,0))",
            label,
        )
        locked = True
        outcome = await connection.fetchval(
            "SELECT aap_assets_v1.begin_fixture_native_source_admission("
            "$1::text,$2::uuid,$3::bigint,$4::uuid,$5::integer,$6::uuid)",
            family,
            request.job_id,
            request.generation,
            request.lease_token,
            pid,
            token,
        )
        if outcome not in {"ordinary", "admitted"}:
            raise RuntimeError("fixture native source admission changed")
        admitted = outcome == "admitted"
        yield
    finally:
        failure = sys.exception()
        if failure is not None and admitted:
            failure.storage_operation_unsettled = True

        async def cleanup():
            errors = []
            if admitted:
                try:
                    result = await connection.fetchval(
                        "SELECT aap_assets_v1.finish_fixture_native_source_admission($1::uuid)",
                        token,
                    )
                    if result is not True:
                        raise RuntimeError("fixture native source admission is unsettled")
                except BaseException as exc:
                    errors.append(exc)
            if locked:
                try:
                    result = await business.fetchval(
                        "SELECT pg_catalog.pg_advisory_unlock_shared("
                        "pg_catalog.hashtextextended($1,0))",
                        label,
                    )
                    if result is not True:
                        raise RuntimeError("fixture native source lock was lost")
                except BaseException as exc:
                    errors.append(exc)
            if connection is not None:
                try:
                    await connection.close()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise builtins.BaseExceptionGroup(
                    "Native fixture source admission cleanup failed", errors
                )

        task = asyncio.create_task(cleanup())
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancelled is None:
                    cancelled = exc
            except BaseException:
                break
        try:
            task.result()
        except BaseException as close_error:
            close_error.storage_operation_unsettled = True
            if isinstance(failure, asyncio.CancelledError):
                failure.storage_operation_unsettled = True
                raise failure from close_error
            if failure is not None:
                combined = builtins.BaseExceptionGroup(
                    "Native fixture source admission and cleanup failed",
                    [failure, close_error],
                )
                combined.storage_operation_unsettled = True
                raise combined from None
            raise
        if cancelled is not None:
            if admitted:
                cancelled.storage_operation_unsettled = True
            if failure is not None:
                combined = builtins.BaseExceptionGroup(
                    "Native fixture source admission failed while closing",
                    [failure, cancelled],
                )
                if admitted or getattr(failure, "storage_operation_unsettled", False):
                    combined.storage_operation_unsettled = True
                raise combined from None
            raise cancelled


class NativeFixtureWriter:
    def __init__(self, database, storage, claim, family, permit):
        self.database = database
        self.storage = storage
        self.claim = claim
        self.family = family
        self.permit = permit
        self.connection = None
        self.lock = asyncio.Lock()
        if family not in {"image", "media"}:
            raise ValueError("invalid fixture native media family")
        if permit.creation_id is None or permit.operation_id is None:
            raise RuntimeError("fixture native source permit is incomplete")
        self.client = None

    def _prepare_target(self):
        if self.client is not None:
            return
        target = load_target(
            "AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_TARGET_FILE", _dsn(), "aap_fixture_media_"
        )
        self.endpoint = target["endpoint"]
        self.bucket = target["bucket"]
        self.receipt_sha256 = target["receipt_sha256"]
        from botocore.config import Config
        import boto3

        original_config = self.storage._client.meta.config
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=target["access_key"],
            aws_secret_access_key=target["secret_key"],
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                connect_timeout=original_config.connect_timeout,
                read_timeout=original_config.read_timeout,
                retries={"mode": "standard", "total_max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )

    async def prepare(self):
        async with self.lock:
            self._prepare_target()

    async def _connect(self):
        if self.connection is not None:
            return
        async with self.database.fixture_business_connection() as business:
            self.connection = await _verified_connection(business)

    async def begin(self, key, sha256, size):
        identifier = uuid.uuid4()
        async with self.lock:
            self._prepare_target()
            await self._connect()
            result = await self.connection.fetchval(
                "SELECT aap_assets_v1.begin_fixture_native_cache_write("
                "$1::text,$2::uuid,$3::bigint,$4::uuid,$5::uuid,$6::uuid,"
                "$7::text,$8::text,$9::text,$10::text,$11::text,$12::bigint,$13::uuid)",
                self.family,
                self.claim.job_id,
                self.claim.generation,
                self.claim.lease_token,
                self.permit.creation_id,
                self.permit.operation_id,
                self.endpoint,
                self.bucket,
                self.receipt_sha256,
                key,
                sha256,
                size,
                identifier,
            )
            if result != identifier:
                raise RuntimeError("fixture native cache intent changed")
        return identifier

    async def advance(self, identifier, state, etag, version):
        async with self.lock:
            if self.connection is None:
                raise RuntimeError("fixture native cache control is unavailable")
            result = await self.connection.fetchval(
                "SELECT aap_assets_v1.advance_fixture_native_cache_write("
                "$1::uuid,$2::uuid,$3::text,$4::text,$5::text)",
                identifier,
                self.permit.operation_id,
                state,
                etag,
                version,
            )
            if result is not True:
                raise RuntimeError("fixture native cache receipt changed")

    async def close(self):
        errors = []
        if self.connection is not None:
            try:
                await self.connection.close()
            except BaseException as exc:
                errors.append(exc)
            self.connection = None
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.close)
            except BaseException as exc:
                errors.append(exc)
            self.client = None
        if errors:
            raise builtins.BaseExceptionGroup("Native fixture writer close failed", errors)


@asynccontextmanager
async def output_scope(database, storage, claim, family, permit):
    execution = _execution.get()
    if execution is None:
        raise RuntimeError("native output execution is unavailable")
    if execution[2] != family or (
        execution[3] is not None and execution[3] != claim.job_id
    ):
        raise RuntimeError("native output execution mismatch")
    if execution[0] is _ORDINARY:
        token = _active.set(_ORDINARY)
        try:
            yield
        finally:
            _active.reset(token)
        return
    if permit is None or execution[2:] != (family, claim.job_id):
        raise RuntimeError("fixture native output execution mismatch")
    writer = NativeFixtureWriter(database, storage, claim, family, permit)
    token = _active.set(writer)
    try:
        yield
    finally:
        failure = sys.exception()
        _active.reset(token)
        close_task = asyncio.create_task(writer.close())
        cancelled = None
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as exc:
                if cancelled is None:
                    cancelled = exc
            except BaseException:
                break
        try:
            close_task.result()
        except BaseException as close_error:
            close_error.storage_operation_unsettled = True
            if isinstance(failure, asyncio.CancelledError):
                failure.storage_operation_unsettled = True
                raise failure from close_error
            if failure is not None:
                combined = builtins.BaseExceptionGroup(
                    "Native fixture output and control close failed", [failure, close_error]
                )
                combined.storage_operation_unsettled = True
                raise combined from None
            raise
        if cancelled is not None:
            if failure is not None:
                combined = builtins.BaseExceptionGroup(
                    "Native fixture output failed while control closed", [failure, cancelled]
                )
                if getattr(failure, "storage_operation_unsettled", False):
                    combined.storage_operation_unsettled = True
                raise combined from None
            raise cancelled

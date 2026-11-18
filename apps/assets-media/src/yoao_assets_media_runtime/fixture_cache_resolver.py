"""Resolve fixture-native outputs without granting business roles fixture access."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
from pathlib import Path
import stat
from urllib.parse import unquote, urlsplit
import uuid

import httpx

from . import fixture_output_capabilities
from .fixture_native_outputs import _verified_connection


def _credentials():
    selector = os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_LOOKUP_CREDENTIAL_FILE")
    if not selector:
        return None
    if not Path(selector).is_absolute():
        raise RuntimeError("fixture native resolver credentials are unavailable")
    try:
        descriptor = os.open(selector, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("invalid resolver file")
            encoded = stream.read(8193)
        if len(encoded) > 8192:
            raise ValueError("invalid resolver file")
        value = json.loads(encoded)
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "suite_id",
            "dsn",
            "database_oid",
            "database_owner_marker",
        }:
            raise ValueError("invalid resolver file")
        suite = uuid.UUID(value["suite_id"])
        dsn = value["dsn"]
        if not isinstance(dsn, str) or not dsn.startswith("postgresql+asyncpg://"):
            raise ValueError("invalid resolver file")
        url = "postgresql://" + dsn.removeprefix("postgresql+asyncpg://")
        parsed = urlsplit(url)
        parsed.port
        if (
            value["schema"] != "aap-fixture-native-resolver/v1"
            or str(suite) != value["suite_id"]
            or not parsed.hostname
            or unquote(parsed.username or "") != f"aap_fixture_media_lookup_{suite.hex}"
            or not parsed.password
            or not parsed.path.lower().endswith(("_test", "_e2e"))
            or "/" in parsed.path[1:]
            or parsed.query
            or parsed.fragment
            or type(value["database_oid"]) is not int
            or value["database_oid"] <= 0
            or not isinstance(value["database_owner_marker"], str)
            or not value["database_owner_marker"]
        ):
            raise ValueError("invalid resolver file")
        return value, url
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise RuntimeError(
            "fixture native resolver credentials are unavailable"
        ) from None


class FixtureCacheResolver:
    def __init__(self, connection, http):
        self.connection = connection
        self.http = http
        self.lock = asyncio.Lock()

    @classmethod
    async def open(cls, business_pool, *, connect_timeout, read_timeout):
        credentials = _credentials()
        if credentials is None:
            return None
        value, dsn = credentials
        async with business_pool.acquire() as business:
            connection = await _verified_connection(business, dsn)
        try:
            identity = await connection.fetchrow(
                "SELECT oid,pg_catalog.shobj_description(oid,'pg_database') "
                "FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()"
            )
            if (
                identity is None
                or identity[0] != value["database_oid"]
                or identity[1] != value["database_owner_marker"]
            ):
                raise RuntimeError("fixture native resolver database changed")
            http = httpx.AsyncClient(
                timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
                trust_env=False,
                follow_redirects=False,
            )
            return cls(connection, http)
        except BaseException:
            await connection.close()
            raise

    async def url(self, key, expires_in):
        async with self.lock:
            result = await self.connection.fetchval(
                "SELECT yoao_assets_v1.resolve_fixture_native_cache($1::text)", key
            )
        if result is None:
            return None
        try:
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict) or set(result) != {
                "run_id",
                "write_id",
                "proxy_origin",
            }:
                raise ValueError("invalid output reference")
            run, write = uuid.UUID(result["run_id"]), uuid.UUID(result["write_id"])
            if result["run_id"] != str(run) or result["write_id"] != str(write):
                raise ValueError("invalid output reference")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise RuntimeError("fixture native output reference changed") from None
        return fixture_output_capabilities.issue(
            run, write, expires_in, result["proxy_origin"]
        )

    async def close(self):
        errors = []
        try:
            await self.http.aclose()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.connection.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise builtins.BaseExceptionGroup(
                "Fixture native resolver close failed", errors
            )

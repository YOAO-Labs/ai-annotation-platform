"""Observe a local disposable PostgreSQL credential fence without changing it.

The output is a point-in-time fact, not a Jobs retirement authorization. Worker
processes, storage credentials, routing scopes and unknown deployments are outside
this observer's claim. No application or pytest graph is loaded.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import stat
import subprocess
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


_POSTGRES_IMAGE = "postgres:16-alpine"
_PG_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_PG_NAME = re.compile(r"aap-module-data-[0-9a-f]{12}\Z")
_PG_ACCEPTANCE_ID = re.compile(r"[0-9a-f]{24}\Z")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LocalFenceRefused(RuntimeError):
    """The local resource or observed credential fence could not be verified."""


@dataclass(frozen=True)
class WorkerDatabaseCredential:
    source_variable: str
    role: str
    database: str
    container_host: str
    container_port: int
    secret_file: bool
    _url: URL = field(repr=False, compare=False)

    def evidence(self) -> dict[str, Any]:
        """Return identity metadata only; the DSN and password stay in memory."""
        return {
            "source_variable": self.source_variable,
            "role": self.role,
            "database": self.database,
            "container_host": self.container_host,
            "container_port": self.container_port,
            "secret_file": self.secret_file,
        }

    def host_url(self, *, host: str, port: int) -> URL:
        """Retarget this observed DSN to the verifier's loopback PG binding."""
        return self._url.set(drivername="postgresql+asyncpg", host=host, port=port)


@dataclass(frozen=True)
class LocalPostgresIdentity:
    container_id: str
    container_name: str
    acceptance_id: str
    database: str
    host: str
    port: int
    database_oid: int
    cluster_system_identifier: int
    owner_role: str

    def evidence(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "container_name": self.container_name,
            "acceptance_id": self.acceptance_id,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "database_oid": self.database_oid,
            "cluster_system_identifier": self.cluster_system_identifier,
            "owner_role": self.owner_role,
        }


def validate_owned_postgres_container(
    record: Mapping[str, Any],
    *,
    container_id: str,
    container_name: str,
    acceptance_id: str,
    host_url: URL,
) -> dict[str, Any]:
    """Verify the exact verifier-owned, loopback-only disposable PG container."""
    labels = (record.get("Config") or {}).get("Labels") or {}
    bindings = ((record.get("NetworkSettings") or {}).get("Ports") or {}).get(
        "5432/tcp"
    ) or []
    if not (
        _PG_CONTAINER.fullmatch(container_id)
        and _PG_NAME.fullmatch(container_name)
        and _PG_ACCEPTANCE_ID.fullmatch(acceptance_id)
        and record.get("Id") == container_id
        and record.get("Name") == f"/{container_name}"
        and (record.get("Config") or {}).get("Image") == _POSTGRES_IMAGE
        and labels.get("org.aap.acceptance") == "module-data-boundary"
        and labels.get("org.aap.acceptance-id") == acceptance_id
        and (record.get("State") or {}).get("Running") is True
        and host_url.drivername == "postgresql+asyncpg"
        and host_url.host in _LOCAL_HOSTS
        and host_url.port is not None
        and host_url.database
        and host_url.database.endswith("_test")
        and host_url.username == "owner"
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and str(bindings[0].get("HostPort")) == str(host_url.port)
    ):
        raise LocalFenceRefused("postgres_resource_ownership_mismatch")
    return {
        "container_id": container_id,
        "container_name": container_name,
        "acceptance_id": acceptance_id,
        "database": host_url.database,
        "host": host_url.host,
        "port": host_url.port,
        "image": _POSTGRES_IMAGE,
    }


async def prove_stale_worker_dsns_denied(
    credentials: tuple[WorkerDatabaseCredential, ...],
    *,
    host: str,
    port: int,
    database: str,
) -> list[dict[str, Any]]:
    """Require a PostgreSQL auth denial, not a timeout or unreachable endpoint."""
    if not credentials or host not in _LOCAL_HOSTS or not database.endswith("_test"):
        raise LocalFenceRefused("stale_worker_database_target_changed")
    denied = []
    for item in credentials:
        url = item.host_url(host=host, port=port)
        if url.database != database:
            raise LocalFenceRefused("stale_worker_database_target_changed")
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 3, "command_timeout": 3},
        )
        try:
            try:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql("SELECT 1")
            except Exception as exc:
                origin = getattr(exc, "orig", exc)
                sqlstate = getattr(origin, "sqlstate", None) or getattr(
                    origin, "pgcode", None
                )
                if sqlstate not in {"28000", "28P01"}:
                    raise LocalFenceRefused(
                        "stale_worker_login_probe_inconclusive"
                    ) from None
            else:
                raise LocalFenceRefused("stale_worker_login_still_succeeds")
        finally:
            await engine.dispose()
        denied.append({"source_variable": item.source_variable, "role": item.role})
    return denied


def _inspect_postgres(postgres: LocalPostgresIdentity, url: URL) -> None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--type", "container", postgres.container_id],
            capture_output=True,
            check=True,
            timeout=10,
        )
        records = json.loads(result.stdout)
        if len(records) != 1:
            raise ValueError
        validate_owned_postgres_container(
            records[0],
            container_id=postgres.container_id,
            container_name=postgres.container_name,
            acceptance_id=postgres.acceptance_id,
            host_url=url,
        )
    except Exception:
        raise LocalFenceRefused("postgres_fixture_ownership_unavailable") from None
    if (
        url.database != postgres.database
        or url.host != postgres.host
        or url.port != postgres.port
        or postgres.owner_role != "owner"
        or url.query
    ):
        raise LocalFenceRefused("postgres_identity_mismatch")


async def _observe_catalog(engine, postgres, roles: list[str]) -> None:
    async with engine.connect() as connection:
        async with connection.begin():
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            row = (
                await connection.execute(
                    text(
                        "SELECT current_user,current_database(),db.oid,"
                        "control.system_identifier,role.rolsuper "
                        "FROM pg_catalog.pg_database AS db "
                        "CROSS JOIN LATERAL pg_catalog.pg_control_system() AS control "
                        "JOIN pg_catalog.pg_roles AS role ON role.rolname=current_user "
                        "WHERE db.datname=pg_catalog.current_database()"
                    )
                )
            ).one()
            if (
                row.current_user != postgres.owner_role
                or row.current_database != postgres.database
                or row.oid != postgres.database_oid
                or int(row.system_identifier) != postgres.cluster_system_identifier
                or row.rolsuper is not True
            ):
                raise LocalFenceRefused("postgres_identity_mismatch")
            states = (
                (
                    await connection.execute(
                        text(
                            "SELECT rolname,rolcanlogin,rolinherit,"
                            "pg_catalog.has_database_privilege(rolname,current_database(),"
                            "'CONNECT') AS can_connect FROM pg_catalog.pg_roles "
                            "WHERE rolname=ANY(:roles) ORDER BY rolname"
                        ),
                        {"roles": roles},
                    )
                )
                .mappings()
                .all()
            )
            if len(states) != len(roles) or any(
                item["rolcanlogin"] or item["rolinherit"] or item["can_connect"]
                for item in states
            ):
                raise LocalFenceRefused("postgres_login_fence_incomplete")
            counts = (
                await connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM pg_catalog.pg_roles "
                        "WHERE rolcanlogin AND rolname<>:owner) AS logins,"
                        "(SELECT count(*) FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND usesysid IS DISTINCT FROM "
                        "(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:owner)) "
                        "AS sessions,(SELECT count(*) FROM pg_catalog.pg_prepared_xacts "
                        "WHERE database=current_database()) AS prepared"
                    ),
                    {"owner": postgres.owner_role},
                )
            ).one()
            if counts.logins or counts.sessions or counts.prepared:
                raise LocalFenceRefused("postgres_login_fence_incomplete")


async def observe_postgres_fence(
    engine,
    postgres: LocalPostgresIdentity,
    credentials: tuple[WorkerDatabaseCredential, ...],
) -> dict[str, Any]:
    """Observe actual catalog state around fresh stale-credential connections."""
    started = datetime.now(timezone.utc).isoformat()
    if not credentials or any(
        item.database != postgres.database
        or item._url.database != postgres.database
        or item._url.username != item.role
        or item.role == postgres.owner_role
        or item._url.query
        for item in credentials
    ):
        raise LocalFenceRefused("worker_database_role_inventory_mismatch")
    roles = sorted({item.role for item in credentials})
    try:
        await asyncio.to_thread(_inspect_postgres, postgres, engine.url)
        await _observe_catalog(engine, postgres, roles)
        denials = await prove_stale_worker_dsns_denied(
            credentials,
            host=postgres.host,
            port=postgres.port,
            database=postgres.database,
        )
        await _observe_catalog(engine, postgres, roles)
        await asyncio.to_thread(_inspect_postgres, postgres, engine.url)
    except LocalFenceRefused:
        raise
    except Exception:
        raise LocalFenceRefused("postgres_fence_observation_unavailable") from None
    return {
        "schema": "aap-local-postgres-fence-facts/v1",
        "postgres": postgres.evidence(),
        "observation_started_at": started,
        "observation_completed_at": datetime.now(timezone.utc).isoformat(),
        "observed_worker_roles": roles,
        "stale_login_denials": denials,
        "all_non_owner_logins_disabled": True,
        "observed_roles_connect_revoked": True,
        "non_owner_sessions_remaining": 0,
        "prepared_transactions_remaining": 0,
        "jobs_legacy_retirement_proof_created": False,
        "process_storage_and_broker_fence_proven": False,
    }


def read_private_json(path: str):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_uid != os.geteuid()
            or info.st_size > 65536
        ):
            raise LocalFenceRefused("invalid_operator_input")
        raw = os.read(descriptor, 65537)
        if len(raw) > 65536:
            raise LocalFenceRefused("invalid_operator_input")
    finally:
        os.close(descriptor)
    return json.loads(raw)


def _input(path: str):
    value = read_private_json(path)
    if (
        set(value) != {"schema", "postgres", "observer_url", "credentials"}
        or value["schema"] != "aap-local-postgres-fence-input/v1"
    ):
        raise LocalFenceRefused("invalid_operator_input")
    postgres = LocalPostgresIdentity(**value["postgres"])
    url = make_url(value["observer_url"])
    credentials = []
    for item in value["credentials"]:
        if set(item) != {"source_variable", "url"}:
            raise LocalFenceRefused("invalid_operator_input")
        credential = make_url(item["url"])
        if (
            credential.drivername not in {"postgresql", "postgresql+asyncpg"}
            or not credential.username
            or not credential.password
            or not credential.host
            or credential.query
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", item["source_variable"])
        ):
            raise LocalFenceRefused("invalid_operator_input")
        credentials.append(
            WorkerDatabaseCredential(
                source_variable=item["source_variable"],
                role=credential.username,
                database=credential.database,
                container_host=credential.host,
                container_port=credential.port or 5432,
                secret_file=False,
                _url=credential,
            )
        )
    if len({item.source_variable for item in credentials}) != len(credentials):
        raise LocalFenceRefused("invalid_operator_input")
    return url, postgres, tuple(credentials)


async def _run(path: str) -> int:
    try:
        url, postgres, credentials = _input(path)
        # Verify the local owned endpoint before constructing a connecting engine.
        await asyncio.to_thread(_inspect_postgres, postgres, url)
    except Exception:
        print(json.dumps({"reason": "invalid_operator_input"}))
        return 2
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": 3, "command_timeout": 3},
    )
    try:
        output = await observe_postgres_fence(engine, postgres, credentials)
        code = 0
    except LocalFenceRefused as error:
        output = {"reason": str(error)}
        code = 2
    finally:
        await engine.dispose()
    print(json.dumps(output, sort_keys=True, allow_nan=False))
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.input_file)))


if __name__ == "__main__":
    main()

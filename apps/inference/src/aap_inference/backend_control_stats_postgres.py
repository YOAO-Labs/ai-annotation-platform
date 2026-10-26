"""Owner-only snapshot capture for the aggregate backend health stats request."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from aap_inference_contract.backend_control import BackendStatsFact
from aap_inference_contract.backend_endpoint import (
    BackendEndpointSubject,
    backend_endpoint_policy_ready,
    make_backend_endpoint_subject,
)

from .backend_endpoint import BackendEndpointSnapshot, _snapshot

_SCHEMA = "aap_inference_backend_endpoint_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.backend_endpoint_policy()"
_STATS_FUNCTIONS = {
    ("backend_endpoint_policy", ""),
    ("capture_backend_endpoint_subject", "uuid, uuid, text, text"),
    ("stats_endpoint_rows", ""),
    ("open_stats_endpoint", "text, uuid, uuid, text"),
}
_ROWS_SQL = f"SELECT row_value FROM {_SCHEMA}.stats_endpoint_rows() AS row_value"
_CAPTURE_SQL = f"SELECT {_SCHEMA}.capture_backend_endpoint_subject($1::uuid,$2::uuid,$3::text,$4::text)"
_OPEN_SQL = f"SELECT {_SCHEMA}.open_stats_endpoint($1::text,$2::uuid,$3::uuid,$4::text)"


class BackendControlStatsUnavailable(Exception):
    """The fixed aggregate backend health stats capability is unavailable."""


@dataclass(frozen=True, slots=True, repr=False)
class BackendStatsEndpoint:
    fact: BackendStatsFact
    subject: BackendEndpointSubject = field(repr=False)
    endpoint: BackendEndpointSnapshot = field(repr=False)


class BackendControlStatsPostgresData:
    """Capture and open every stats endpoint before releasing the owner DB transaction."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: float = 5.0,
        command_timeout: float = 5.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(character.isspace() for character in dsn)
            or type(connect_timeout) not in {int, float}
            or type(command_timeout) not in {int, float}
        ):
            raise BackendControlStatsUnavailable()
        try:
            connect = float(connect_timeout)
            command = float(command_timeout)
        except (OverflowError, TypeError, ValueError):
            raise BackendControlStatsUnavailable() from None
        if (
            not math.isfinite(connect)
            or connect <= 0
            or not math.isfinite(command)
            or command <= 0
        ):
            raise BackendControlStatsUnavailable()
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = connect
        self.command_timeout = command

    async def _connect(self):
        try:
            import asyncpg
        except ImportError:
            raise BackendControlStatsUnavailable() from None
        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        connection = None
        primary_failure: BaseException | None = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={
                    "application_name": "aap-inference-backend-stats",
                    "default_transaction_read_only": "on",
                },
            )
            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_STATS_FUNCTIONS,
                role=await connection.fetchval("SELECT session_user"),
            )
            policy = await connection.fetchval(_POLICY_SQL)
            if not backend_endpoint_policy_ready(_json_object(policy)):
                raise BackendControlStatsUnavailable()
            return connection, asyncpg
        except BackendControlStatsUnavailable as error:
            primary_failure = error
            raise
        except (
            FunctionRoleUnavailable,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            primary_failure = BackendControlStatsUnavailable()
            raise BackendControlStatsUnavailable() from None
        except BaseException as error:
            primary_failure = error
            raise
        finally:
            if primary_failure is not None and connection is not None:
                await self._close(connection, primary_failure=True)

    async def _close(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                return
            raise BackendControlStatsUnavailable() from None

    async def readiness(self) -> bool:
        try:
            connection, _asyncpg = await self._connect()
        except BackendControlStatsUnavailable:
            return False
        try:
            await self._close(connection, primary_failure=False)
        except BackendControlStatsUnavailable:
            return False
        return True

    async def snapshots(self, request_id: UUID) -> tuple[BackendStatsEndpoint, ...]:
        if type(request_id) is not UUID:
            raise BackendControlStatsUnavailable()
        connection, asyncpg = await self._connect()
        primary_failure = False
        try:
            endpoints: list[BackendStatsEndpoint] = []
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                rows = await connection.fetch(_ROWS_SQL)
                for record in rows:
                    row = _json_object(record["row_value"])
                    try:
                        fact = BackendStatsFact.model_validate(
                            {
                                "registry_id": UUID(row["registry_id"]),
                                "name": row["name"],
                                "url": row["url"],
                                "state": row["state"],
                                "source": row["source"],
                                "is_interactive": row["is_interactive"],
                                "health_meta": row["health_meta"],
                                "created_at": _timestamp(row["created_at"]),
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        raise BackendControlStatsUnavailable() from None
                    captured = _json_object(
                        await connection.fetchval(
                            _CAPTURE_SQL,
                            fact.registry_id,
                            request_id,
                            "stats",
                            None,
                        )
                    )
                    if (
                        set(captured) != {"status", "subject"}
                        or captured["status"] != "ok"
                        or type(captured["subject"]) is not str
                    ):
                        raise BackendControlStatsUnavailable()
                    subject = make_backend_endpoint_subject(
                        fact.registry_id,
                        request_id=request_id,
                        purpose="stats",
                        binding=None,
                        subject=captured["subject"],
                    )
                    raw_endpoint = await connection.fetchval(
                        _OPEN_SQL,
                        subject.subject,
                        fact.registry_id,
                        request_id,
                        None,
                    )
                    if type(raw_endpoint) is not str:
                        raise BackendControlStatsUnavailable()
                    endpoint = _snapshot(raw_endpoint, subject)
                    endpoints.append(BackendStatsEndpoint(fact, subject, endpoint))
            return tuple(endpoints)
        except BackendControlStatsUnavailable:
            primary_failure = True
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
            KeyError,
        ):
            primary_failure = True
            raise BackendControlStatsUnavailable() from None
        except BaseException:
            primary_failure = True
            raise
        finally:
            await self._close(connection, primary_failure=primary_failure)


def _json_object(value):
    if type(value) is str:
        try:
            value = json.loads(value)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise BackendControlStatsUnavailable() from None
    if type(value) is not dict:
        raise BackendControlStatsUnavailable()
    return value


def _timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if type(value) is str:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    raise BackendControlStatsUnavailable()


__all__ = [
    "BackendControlStatsPostgresData",
    "BackendControlStatsUnavailable",
    "BackendStatsEndpoint",
]

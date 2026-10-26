"""Function-only owner connection for purpose-limited endpoint subject opening."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from uuid import UUID

from aap_inference_contract.backend_endpoint import (
    BackendEndpointAuthority,
    BackendEndpointPurpose,
    backend_endpoint_policy_ready,
)

from .backend_endpoint import BackendEndpointUnavailable

_SCHEMA = "aap_inference_backend_endpoint_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.backend_endpoint_policy()"
_OPENERS = {
    "health_observation": (
        "open_health_observation_endpoint",
        "text, uuid, uuid, text",
    ),
    "setup": ("open_setup_endpoint", "text, uuid, uuid, text"),
    "catalog_fallback": ("open_catalog_fallback_endpoint", "text, uuid, uuid, text"),
    "stats": ("open_stats_endpoint", "text, uuid, uuid, text"),
    "backend_read": ("open_backend_read_endpoint", "text, uuid, uuid, text"),
    "lifecycle": ("open_lifecycle_endpoint", "text, uuid, uuid, text"),
    "execution": ("open_execution_endpoint", "text, uuid, uuid, text"),
    "gpu_control": ("open_gpu_control_endpoint", "text, uuid, uuid, text"),
}
_LOGGER = logging.getLogger(__name__)


def _function_set(purpose: BackendEndpointPurpose) -> set[tuple[str, str]]:
    try:
        function, signature = _OPENERS[purpose]
    except KeyError:
        raise BackendEndpointUnavailable() from None
    return {
        ("backend_endpoint_policy", ""),
        (function, signature),
    }


class BackendEndpointPostgresData:
    """Open exactly one allowlisted subject purpose per verified connection."""

    def __init__(
        self,
        dsn: str,
        purpose: BackendEndpointPurpose,
        *,
        connect_timeout: float = 5.0,
        command_timeout: float = 5.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(character.isspace() for character in dsn)
            or purpose not in _OPENERS
        ):
            raise BackendEndpointUnavailable()
        if type(connect_timeout) not in {int, float} or type(command_timeout) not in {
            int,
            float,
        }:
            raise ValueError("Invalid backend endpoint database timeout")
        try:
            connect = float(connect_timeout)
            command = float(command_timeout)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("Invalid backend endpoint database timeout") from None
        if (
            not math.isfinite(connect)
            or connect <= 0
            or not math.isfinite(command)
            or command <= 0
        ):
            raise ValueError("Invalid backend endpoint database timeout")
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.purpose = purpose
        self.connect_timeout = connect
        self.command_timeout = command

    async def _close(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _LOGGER.warning("Inference backend endpoint connection cleanup failed")
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _LOGGER.warning("Inference backend endpoint connection cleanup failed")
                return
            raise BackendEndpointUnavailable() from None

    async def _open_verified(self):
        try:
            import asyncpg
        except ImportError:
            raise BackendEndpointUnavailable() from None
        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        connection = None
        primary_error: BaseException | None = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={
                    "application_name": "aap-inference-backend-endpoint",
                    "default_transaction_read_only": "on",
                },
            )
            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_function_set(self.purpose),
                role=await connection.fetchval("SELECT session_user"),
            )
            policy = await connection.fetchval(_POLICY_SQL)
            if not backend_endpoint_policy_ready(policy):
                raise BackendEndpointUnavailable()
            return connection, asyncpg
        except BackendEndpointUnavailable as error:
            primary_error = error
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
            primary_error = BackendEndpointUnavailable()
            raise BackendEndpointUnavailable() from None
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if primary_error is not None and connection is not None:
                await self._close(connection, primary_failure=True)

    async def readiness(self) -> bool:
        try:
            connection, _asyncpg = await self._open_verified()
        except BackendEndpointUnavailable:
            return False
        try:
            await self._close(connection, primary_failure=False)
        except BackendEndpointUnavailable:
            return False
        return True

    async def open_subject(
        self,
        subject: str,
        registry_id: UUID,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        authority: BackendEndpointAuthority | None,
    ) -> str:
        read_only = purpose in {
            "health_observation",
            "setup",
            "catalog_fallback",
            "stats",
            "backend_read",
        }
        if (
            type(subject) is not str
            or not isinstance(registry_id, UUID)
            or not isinstance(request_id, UUID)
            or purpose != self.purpose
            or (read_only and authority is not None)
            or (not read_only and authority is None)
            or (
                authority is not None
                and (
                    not isinstance(authority, BackendEndpointAuthority)
                    or authority.attempt_id != request_id
                )
            )
        ):
            raise BackendEndpointUnavailable()
        function, _signature = _OPENERS[self.purpose]
        statement = f"SELECT {_SCHEMA}.{function}($1::text,$2::uuid,$3::uuid,$4::text)"
        authority_json = (
            None
            if authority is None
            else json.dumps(
                authority.model_dump(mode="json"),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        connection, asyncpg = await self._open_verified()
        primary_failure = False
        try:
            value = await connection.fetchval(
                statement, subject, registry_id, request_id, authority_json
            )
            if type(value) is not str:
                raise BackendEndpointUnavailable()
            return value
        except BackendEndpointUnavailable:
            primary_failure = True
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            primary_failure = True
            raise BackendEndpointUnavailable() from None
        except BaseException:
            primary_failure = True
            raise
        finally:
            await self._close(connection, primary_failure=primary_failure)

    async def close(self) -> None:
        """The owner provider keeps no connection between operations."""


__all__ = ["BackendEndpointPostgresData"]

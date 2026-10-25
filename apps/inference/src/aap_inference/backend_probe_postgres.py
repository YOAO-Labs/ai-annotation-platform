"""Function-only PostgreSQL provider for encrypted backend probe snapshots."""

from __future__ import annotations

import asyncio
import logging
import math
from uuid import UUID

from aap_inference_contract.probe import (
    BackendProbeUnavailable,
    backend_probe_policy_ready,
)

_SCHEMA = "aap_inference_backend_probe_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.backend_probe_policy()"
_OPEN_SUBJECT_SQL = f"SELECT {_SCHEMA}.open_backend_probe_subject($1::text, $2::uuid)"
_FUNCTIONS = {
    ("backend_probe_policy", ""),
    ("open_backend_probe_subject", "text, uuid"),
}
_logger = logging.getLogger(__name__)


class BackendProbePostgresData:
    """Open and close one verified function-only connection per operation."""

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
        ):
            raise BackendProbeUnavailable()
        if (
            type(connect_timeout) not in {int, float}
            or not math.isfinite(float(connect_timeout))
            or connect_timeout <= 0
            or type(command_timeout) not in {int, float}
            or not math.isfinite(float(command_timeout))
            or command_timeout <= 0
        ):
            raise ValueError("Invalid backend probe database timeout")
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = float(connect_timeout)
        self.command_timeout = float(command_timeout)

    async def _close_connection(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference backend probe connection cleanup failed")
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference backend probe connection cleanup failed")
                return
            raise BackendProbeUnavailable() from None

    async def _open_verified_connection(self):
        try:
            import asyncpg
        except ImportError:
            raise BackendProbeUnavailable() from None

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
                    "application_name": "aap-inference-backend-probe",
                    "default_transaction_read_only": "on",
                },
            )
            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_FUNCTIONS,
            )
            policy = await connection.fetchval(_POLICY_SQL)
            if not backend_probe_policy_ready(policy):
                raise BackendProbeUnavailable()
            return connection, asyncpg
        except BackendProbeUnavailable as error:
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
        ) as error:
            primary_error = error
            raise BackendProbeUnavailable() from None
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if primary_error is not None and connection is not None:
                await self._close_connection(connection, primary_failure=True)

    async def readiness(self) -> bool:
        try:
            connection, _asyncpg = await self._open_verified_connection()
        except BackendProbeUnavailable:
            return False
        try:
            await self._close_connection(connection, primary_failure=False)
        except BackendProbeUnavailable:
            return False
        return True

    async def open_subject(self, probe_subject: str, registry_id: UUID) -> str:
        if type(probe_subject) is not str or not isinstance(registry_id, UUID):
            raise BackendProbeUnavailable()
        connection, asyncpg = await self._open_verified_connection()
        primary_failure = False
        try:
            raw_json = await connection.fetchval(
                _OPEN_SUBJECT_SQL,
                probe_subject,
                registry_id,
            )
            if type(raw_json) is not str:
                raise BackendProbeUnavailable()
            return raw_json
        except BackendProbeUnavailable:
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
            raise BackendProbeUnavailable() from None
        except BaseException:
            primary_failure = True
            raise
        finally:
            await self._close_connection(
                connection,
                primary_failure=primary_failure,
            )

    async def close(self) -> None:
        """The provider owns no persistent connection between operations."""


__all__ = ["BackendProbePostgresData"]

"""Function-only PostgreSQL provider for encrypted backend health observations."""

from __future__ import annotations

import asyncio
import logging
import math
from uuid import UUID

from aap_inference_contract.health_observation import (
    BackendHealthObservationUnavailable,
    health_observation_policy_ready,
)

_SCHEMA = "aap_inference_health_observation_v1"
_POLICY_SQL = f"SELECT {_SCHEMA}.health_observation_policy()"
_OPEN_SUBJECT_SQL = (
    f"SELECT {_SCHEMA}.open_health_observation_subject($1::text, $2::uuid, $3::uuid)"
)
_SEAL_OBSERVATION_SQL = (
    f"SELECT {_SCHEMA}.seal_health_observation($1::text, $2::uuid, $3::uuid, $4::text)"
)
_FUNCTIONS = {
    ("health_observation_policy", ""),
    ("open_health_observation_subject", "text, uuid, uuid"),
    ("seal_health_observation", "text, uuid, uuid, text"),
}
_logger = logging.getLogger(__name__)


class BackendHealthObservationPostgresData:
    """Open one verified, read-only owner connection for each operation."""

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
            raise BackendHealthObservationUnavailable()
        if type(connect_timeout) not in {int, float} or type(command_timeout) not in {
            int,
            float,
        }:
            raise ValueError("Invalid health observation database timeout")
        try:
            numeric_connect_timeout = float(connect_timeout)
            numeric_command_timeout = float(command_timeout)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("Invalid health observation database timeout") from None
        if (
            not math.isfinite(numeric_connect_timeout)
            or numeric_connect_timeout <= 0
            or not math.isfinite(numeric_command_timeout)
            or numeric_command_timeout <= 0
        ):
            raise ValueError("Invalid health observation database timeout")
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = numeric_connect_timeout
        self.command_timeout = numeric_command_timeout

    async def _close_connection(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning(
                    "Inference health observation connection cleanup failed"
                )
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning(
                    "Inference health observation connection cleanup failed"
                )
                return
            raise BackendHealthObservationUnavailable() from None

    async def _open_verified_connection(self):
        try:
            import asyncpg
        except ImportError:
            raise BackendHealthObservationUnavailable() from None

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
                    "application_name": "aap-inference-health-observation",
                    "default_transaction_read_only": "on",
                },
            )
            await verify_function_only_role(
                connection,
                schema=_SCHEMA,
                functions=_FUNCTIONS,
            )
            policy = await connection.fetchval(_POLICY_SQL)
            if not health_observation_policy_ready(policy):
                raise BackendHealthObservationUnavailable()
            return connection, asyncpg
        except BackendHealthObservationUnavailable as error:
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
            raise BackendHealthObservationUnavailable() from None
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if primary_error is not None and connection is not None:
                await self._close_connection(connection, primary_failure=True)

    async def readiness(self) -> bool:
        try:
            connection, _asyncpg = await self._open_verified_connection()
        except BackendHealthObservationUnavailable:
            return False
        try:
            await self._close_connection(connection, primary_failure=False)
        except BackendHealthObservationUnavailable:
            return False
        return True

    async def open_subject(
        self,
        subject: str,
        registry_id: UUID,
        request_id: UUID,
    ) -> str:
        if (
            type(subject) is not str
            or not isinstance(registry_id, UUID)
            or not isinstance(request_id, UUID)
        ):
            raise BackendHealthObservationUnavailable()
        connection, asyncpg = await self._open_verified_connection()
        primary_failure = False
        try:
            raw_json = await connection.fetchval(
                _OPEN_SUBJECT_SQL,
                subject,
                registry_id,
                request_id,
            )
            if type(raw_json) is not str:
                raise BackendHealthObservationUnavailable()
            return raw_json
        except BackendHealthObservationUnavailable:
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
            raise BackendHealthObservationUnavailable() from None
        except BaseException:
            primary_failure = True
            raise
        finally:
            await self._close_connection(connection, primary_failure=primary_failure)

    async def seal_observation(
        self,
        subject: str,
        registry_id: UUID,
        request_id: UUID,
        observation_json: str,
    ) -> str:
        if (
            type(subject) is not str
            or not isinstance(registry_id, UUID)
            or not isinstance(request_id, UUID)
            or type(observation_json) is not str
        ):
            raise BackendHealthObservationUnavailable()
        connection, asyncpg = await self._open_verified_connection()
        primary_failure = False
        try:
            evidence = await connection.fetchval(
                _SEAL_OBSERVATION_SQL,
                subject,
                registry_id,
                request_id,
                observation_json,
            )
            if type(evidence) is not str:
                raise BackendHealthObservationUnavailable()
            return evidence
        except BackendHealthObservationUnavailable:
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
            raise BackendHealthObservationUnavailable() from None
        except BaseException:
            primary_failure = True
            raise
        finally:
            await self._close_connection(connection, primary_failure=primary_failure)

    async def close(self) -> None:
        """The provider owns no persistent connection between operations."""


__all__ = ["BackendHealthObservationPostgresData"]

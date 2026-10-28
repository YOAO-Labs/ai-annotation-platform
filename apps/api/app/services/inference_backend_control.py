"""Caller-UOW control adapter with purpose-bound endpoint subjects."""

from __future__ import annotations

import json
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_inference.backend_endpoint import (
    BackendEndpointAuthorityRejected,
    BackendEndpointBinding,
    BackendEndpointManager,
    BackendEndpointUnavailable,
)
from aap_inference.health import BackendHealthCoordinator
from aap_inference_contract.backend_control import (
    BACKEND_CONTROL_PROTOCOL,
    BackendCatalogFact,
    BackendControlFailure,
    BackendControlOperation,
    BackendControlReply,
    BackendControlRequest,
    BackendControlSetupResult,
    BackendCredentialPolicyFact,
    BackendRegistrationLabel,
    BackendRequestOnlyEndpoint,
    BackendRequestOnlyProbeFact,
    BackendRequestOnlyProbeReply,
    BackendRequestOnlyProbeRequest,
    BackendSmokeRequest,
    BackendStatsFact,
    BackendStatsSnapshot,
    BackendStatsSnapshotsRequest,
    BackendStatsSnapshotsResult,
    BackendUnregisteredSmokeResult,
)
from aap_inference_contract.backend_control_client import (
    BackendControlUnavailable,
)
from aap_inference_contract.backend_endpoint import (
    BackendEndpointPurpose,
    BackendEndpointSubject,
)
from app.services.inference_health_data import InferenceHealthData
from app.services.inference_health_observation import health_observation_client_factory
from app.services.inference_limits import capability_limits_from_settings
from app.observability.metrics import observe_ml_backend


async def capture_backend_endpoint_subject(
    db: AsyncSession,
    registry_id: uuid.UUID,
    *,
    request_id: uuid.UUID,
    purpose: BackendEndpointPurpose,
    binding: BackendEndpointBinding | None = None,
) -> BackendEndpointSubject:
    """Capture the only caller-facing endpoint capability; opening stays owner-private."""
    return await BackendEndpointManager(_CallerEndpointData(db)).capture(
        registry_id,
        request_id=request_id,
        purpose=purpose,
        binding=binding,
    )


_ENDPOINT_SCHEMA = "aap_inference_backend_endpoint_v1"
_ENDPOINT_CAPTURE = text(
    f"SELECT {_ENDPOINT_SCHEMA}.capture_backend_endpoint_subject("
    "CAST(:registry_id AS uuid),CAST(:request_id AS uuid),CAST(:purpose AS text),"
    "CAST(:binding_json AS text))"
)
_CATALOG_ROWS = text(
    f"SELECT row_value FROM {_ENDPOINT_SCHEMA}.catalog_endpoint_rows() AS row_value"
)
_STATS_ROWS = text(
    f"SELECT row_value FROM {_ENDPOINT_SCHEMA}.stats_endpoint_rows() AS row_value"
)
_REGISTRATION_LABELS = text(
    f"SELECT url, source FROM {_ENDPOINT_SCHEMA}.observation_registration_labels(:urls)"
)
_CREDENTIAL_POLICY = text(
    f"SELECT {_ENDPOINT_SCHEMA}.controlled_model_credential_policy(:registry_id)"
)
_BACKEND_EXISTS = text(
    f"SELECT {_ENDPOINT_SCHEMA}.health_backend_row(:registry_id, false)"
)


class BackendControlOperationError(Exception):
    """A typed remote operation failure preserved for the API boundary."""

    def __init__(self, failure: BackendControlFailure) -> None:
        self.failure = failure
        super().__init__(failure.code)


class BackendControlClient(Protocol):
    async def execute(self, request: BackendControlRequest) -> BackendControlReply: ...


class BackendControlRequestOnlyClient(Protocol):
    async def execute(
        self,
        request: BackendRequestOnlyProbeRequest
        | BackendSmokeRequest
        | BackendStatsSnapshotsRequest,
    ) -> BackendControlReply: ...


class BackendControlOperationContext(Protocol):
    request_id: uuid.UUID
    binding: BackendEndpointBinding | None
    client: BackendControlClient

    async def commit_before_io(self) -> None: ...


class BackendControlOperationContextFactory(Protocol):
    def __call__(
        self,
        operation: BackendControlOperation,
        registry_id: uuid.UUID,
        *,
        expected_topology: Any = None,
    ) -> AbstractAsyncContextManager[BackendControlOperationContext]: ...


class _CallerEndpointData:
    """Capture only; ordinary API code has no endpoint-open operation."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def capture_subject(self, registry_id, request_id, purpose, binding):
        binding_json = (
            None
            if binding is None
            else json.dumps(
                binding.model_dump(mode="json"),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return await self.db.scalar(
            _ENDPOINT_CAPTURE,
            {
                "registry_id": registry_id,
                "request_id": request_id,
                "purpose": purpose,
                "binding_json": binding_json,
            },
        )

    async def open_subject(self, subject, registry_id, request_id, purpose, authority):
        raise BackendEndpointUnavailable()

    async def readiness(self):
        return False


def _native_json(value):
    if type(value) is str:
        try:
            value = json.loads(value)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise RuntimeError(
                "Native backend control projection is malformed"
            ) from None
    if type(value) is not dict:
        raise RuntimeError("Native backend control projection is malformed")
    return value


def _record_observations(observations) -> None:
    for observation in observations:
        observe_ml_backend(
            observation.backend_id,
            observation.outcome,
            observation.duration_seconds,
        )


def _timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if type(value) is str:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    raise RuntimeError("Native backend stats timestamp is malformed")


class BackendControlService:
    """Strict caller transaction adapter; parent supplies real control/HTTP context."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        operation_context_factory: BackendControlOperationContextFactory | None = None,
        request_only_client: BackendControlRequestOnlyClient | None = None,
    ) -> None:
        self.db = db
        self.operation_context_factory = operation_context_factory
        self.request_only_client = request_only_client

    async def _capture(
        self,
        registry_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None = None,
    ) -> BackendEndpointSubject:
        return await capture_backend_endpoint_subject(
            self.db,
            registry_id,
            request_id=request_id,
            purpose=purpose,
            binding=binding,
        )

    async def _execute(
        self,
        operation: BackendControlOperation,
        registry_id: uuid.UUID,
        *,
        body: dict[str, Any] | None = None,
        expected_topology: Any = None,
    ) -> dict[str, Any] | list[Any]:
        if self.operation_context_factory is None:
            raise BackendControlUnavailable()
        try:
            async with self.operation_context_factory(
                operation, registry_id, expected_topology=expected_topology
            ) as context:
                if type(context.request_id) is not uuid.UUID:
                    raise BackendControlUnavailable()
                if context.binding is not None and not isinstance(
                    context.binding, BackendEndpointBinding
                ):
                    raise BackendControlUnavailable()
                purpose: BackendEndpointPurpose = {
                    "setup": "setup",
                    "health_meta": "health_observation",
                    "unload": "lifecycle",
                    "terminal_drain": "lifecycle",
                    "reload": "lifecycle",
                    "warmup": "execution",
                    "catalog_fallback": "catalog_fallback",
                    "stats": "stats",
                    "install_trained_model": "execution",
                    "trained_model_receipt": "backend_read",
                    "managed_evaluation_profile": "backend_read",
                    "managed_evaluation_receipt": "backend_read",
                    "managed_evaluation_seal": "execution",
                    "versions": "backend_read",
                }[operation]
                subject = await self._capture(
                    registry_id,
                    request_id=context.request_id,
                    purpose=purpose,
                    binding=context.binding,
                )
                if context.binding is not None:
                    await context.commit_before_io()
                request = BackendControlRequest(
                    kind="registered",
                    schema=BACKEND_CONTROL_PROTOCOL,
                    operation=operation,
                    request_id=context.request_id,
                    registry_id=registry_id,
                    endpoint_subject=subject,
                    binding=context.binding,
                    body=body,
                )
                reply = await context.client.execute(request)
                _record_observations(reply.observations)
                if reply.error is not None:
                    raise BackendControlOperationError(reply.error)
                if reply.result is None:
                    raise BackendControlUnavailable()
        except BackendEndpointAuthorityRejected as error:
            raise BackendControlOperationError(
                BackendControlFailure(code=error.reason, status_code=409)
            ) from None
        except BackendEndpointUnavailable:
            raise BackendControlUnavailable() from None
        return reply.result

    async def terminal_drain(
        self, registry_id: uuid.UUID, *, boot_id: str, operation_id: uuid.UUID
    ) -> dict[str, object]:
        result = await self._execute(
            "terminal_drain",
            registry_id,
            body={
                "registry_id": str(registry_id),
                "boot_id": boot_id,
                "operation_id": str(operation_id),
            },
        )
        if (
            type(result) is not dict
            or set(result) != {
                "registry_id", "boot_id", "operation_id", "control_epoch",
                "generation", "pool_ids", "unloaded",
            }
            or result["registry_id"] != str(registry_id)
            or result["boot_id"] != boot_id
            or result["operation_id"] != str(operation_id)
            or result["control_epoch"] is not None
            or result["generation"] is not None
            or result["unloaded"] is not True
            or type(result["pool_ids"]) is not list
            or not result["pool_ids"]
            or any(type(pool) is not str or not pool for pool in result["pool_ids"])
            or result["pool_ids"] != sorted(set(result["pool_ids"]))
        ):
            raise BackendControlUnavailable()
        return result

    async def _registry_exists(self, registry_id: uuid.UUID) -> bool:
        return (
            await self.db.scalar(_BACKEND_EXISTS, {"registry_id": registry_id})
            is not None
        )

    async def credential_policy(
        self, registry_id: uuid.UUID
    ) -> BackendCredentialPolicyFact | None:
        value = await self.db.scalar(_CREDENTIAL_POLICY, {"registry_id": registry_id})
        if value is None:
            return None
        row = _native_json(value)
        if type(row) is not dict or set(row) != {
            "auth_method",
            "auth_token_length_valid",
        }:
            raise BackendControlUnavailable()
        try:
            return BackendCredentialPolicyFact.model_validate(row)
        except (TypeError, ValueError):
            raise BackendControlUnavailable() from None

    async def setup(self, registry_id: uuid.UUID) -> BackendControlSetupResult:
        if not await self._registry_exists(registry_id):
            return BackendControlSetupResult(
                registry_id=registry_id,
                error=BackendControlFailure(code="backend_not_found", status_code=404),
            )
        try:
            setup = await self._execute("setup", registry_id)
            return BackendControlSetupResult(registry_id=registry_id, setup=setup)
        except BackendControlOperationError as error:
            return BackendControlSetupResult(
                registry_id=registry_id, error=error.failure
            )
        except BackendControlUnavailable:
            return BackendControlSetupResult(
                registry_id=registry_id,
                error=BackendControlFailure(
                    code="backend_control_unavailable", status_code=503
                ),
            )

    async def catalog_fallback(self, registry_id: uuid.UUID) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute("catalog_fallback", registry_id)
        if type(result) is not dict:
            raise BackendControlUnavailable()
        return result

    async def check_health(
        self,
        registry_id: uuid.UUID,
        *,
        gpu_health_challenge: str | None = None,
    ) -> bool:
        return await BackendHealthCoordinator(
            InferenceHealthData(self.db),
            client_factory=health_observation_client_factory(self.db),
            capability_limits_factory=capability_limits_from_settings,
        ).check_health(registry_id, gpu_health_challenge=gpu_health_challenge)

    async def health_meta(
        self, registry_id: uuid.UUID
    ) -> tuple[bool, dict[str, Any] | None]:
        if not await self._registry_exists(registry_id):
            return False, None
        result = await self._execute("health_meta", registry_id)
        if (
            type(result) is not dict
            or set(result) != {"healthy", "meta"}
            or type(result["healthy"]) is not bool
            or (result["meta"] is not None and type(result["meta"]) is not dict)
        ):
            raise BackendControlUnavailable()
        return result["healthy"], result["meta"]

    async def install_trained_model(
        self, registry_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute(
            "install_trained_model", registry_id, body=dict(payload)
        )
        if type(result) is not dict:
            raise BackendControlUnavailable()
        return result

    async def trained_model_receipt(
        self, registry_id: uuid.UUID, version_id: str
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute(
            "trained_model_receipt", registry_id, body={"version_id": version_id}
        )
        if type(result) is not dict:
            raise BackendControlUnavailable()
        return result

    async def managed_evaluation_profile(
        self, registry_id: uuid.UUID
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute("managed_evaluation_profile", registry_id)
        if type(result) is not dict:
            raise BackendControlUnavailable()
        return result

    async def managed_evaluation_receipt(
        self,
        registry_id: uuid.UUID,
        request_id: uuid.UUID,
        binding_digest: str,
        storage_identity: str,
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute(
            "managed_evaluation_receipt",
            registry_id,
            body={
                "request_id": str(request_id),
                "binding_digest": binding_digest,
                "storage_identity": storage_identity,
            },
        )
        if type(result) is not dict:
            raise BackendControlUnavailable()
        return result

    async def managed_evaluation_seal(
        self,
        registry_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        binding_digest: str,
        profile_digest: str,
        storage_identity: str,
        issued_at: int,
        expires_at: int,
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute(
            "managed_evaluation_seal",
            registry_id,
            body={
                "request_id": str(request_id),
                "binding_digest": binding_digest,
                "profile_digest": profile_digest,
                "storage_identity": storage_identity,
                "issued_at": issued_at,
                "expires_at": expires_at,
            },
        )
        if type(result) is not dict:
            raise BackendControlUnavailable()
        return result

    async def versions(self, registry_id: uuid.UUID) -> list[str] | None:
        if not await self._registry_exists(registry_id):
            return None
        result = await self._execute("versions", registry_id)
        if type(result) is not list or any(
            type(version) is not str for version in result
        ):
            raise BackendControlUnavailable()
        return result

    async def _request_only_result(
        self,
        request: BackendRequestOnlyProbeRequest
        | BackendSmokeRequest
        | BackendStatsSnapshotsRequest,
    ) -> dict[str, Any]:
        if self.request_only_client is None:
            raise BackendControlUnavailable()
        try:
            reply = await self.request_only_client.execute(request)
        except BackendControlUnavailable:
            raise
        except Exception:
            raise BackendControlUnavailable() from None
        if reply.request_id != request.request_id:
            raise BackendControlUnavailable()
        _record_observations(reply.observations)
        if reply.error is not None:
            raise BackendControlOperationError(reply.error)
        if type(reply.result) is not dict:
            raise BackendControlUnavailable()
        return reply.result

    async def manual_probe(
        self,
        endpoint: BackendRequestOnlyEndpoint,
        *,
        timeout_seconds: int | float,
    ) -> BackendRequestOnlyProbeFact:
        request = BackendRequestOnlyProbeRequest(
            kind="request_only",
            schema=BACKEND_CONTROL_PROTOCOL,
            operation="manual_probe",
            request_id=uuid.uuid4(),
            endpoints=(endpoint,),
            timeout_seconds=timeout_seconds,
        )
        result = await self._request_only_result(request)
        rows = result.get("results")
        if type(rows) is not list or len(rows) != 1:
            raise BackendControlUnavailable()
        return BackendRequestOnlyProbeFact.model_validate(rows[0])

    async def environment_observation(
        self,
        endpoints: tuple[BackendRequestOnlyEndpoint, ...],
        *,
        timeout_seconds: int | float,
    ) -> BackendRequestOnlyProbeReply:
        request = BackendRequestOnlyProbeRequest(
            kind="request_only",
            schema=BACKEND_CONTROL_PROTOCOL,
            operation="environment_observation",
            request_id=uuid.uuid4(),
            endpoints=endpoints,
            timeout_seconds=timeout_seconds,
        )
        result = await self._request_only_result(request)
        if set(result) != {"results"} or type(result["results"]) is not list:
            raise BackendControlUnavailable()
        try:
            return BackendRequestOnlyProbeReply.model_validate(
                {"request_id": request.request_id, "results": tuple(result["results"])}
            )
        except (TypeError, ValueError):
            raise BackendControlUnavailable() from None

    async def smoke_unregistered(
        self,
        endpoint: BackendRequestOnlyEndpoint,
        body: dict[str, Any],
    ) -> BackendUnregisteredSmokeResult:
        request = BackendSmokeRequest(
            kind="smoke_unregistered",
            schema=BACKEND_CONTROL_PROTOCOL,
            request_id=uuid.uuid4(),
            endpoint=endpoint,
            body=dict(body),
        )
        result = await self._request_only_result(request)
        try:
            return BackendUnregisteredSmokeResult.model_validate(result)
        except (TypeError, ValueError):
            raise BackendControlUnavailable() from None

    async def unload(
        self, registry_id: uuid.UUID, *, expected_topology: Any = None
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        return await self._execute(
            "unload", registry_id, expected_topology=expected_topology
        )

    async def reload(
        self,
        registry_id: uuid.UUID,
        sam_variant: str | None = None,
        dino_variant: str | None = None,
        modality: str | None = None,
        task_type: str | None = None,
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        body = {
            key: value
            for key, value in {
                "sam_variant": sam_variant,
                "dino_variant": dino_variant,
                "modality": modality,
                "task_type": task_type,
            }.items()
            if value is not None
        }
        return await self._execute("reload", registry_id, body=body or None)

    async def warmup(
        self, registry_id: uuid.UUID, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not await self._registry_exists(registry_id):
            return None
        return await self._execute("warmup", registry_id, body=dict(body or {}))

    async def catalog_rows(self) -> tuple[BackendCatalogFact, ...]:
        result = await self.db.execute(_CATALOG_ROWS)
        facts: list[BackendCatalogFact] = []
        for value in result.scalars():
            row = _native_json(value)
            facts.append(
                BackendCatalogFact.model_validate(
                    {
                        "registry_id": uuid.UUID(row["registry_id"]),
                        "name": row.get("name"),
                        "url": row.get("url"),
                        "state": row.get("state"),
                        "source": row.get("source"),
                        "is_interactive": row.get("is_interactive"),
                        "health_meta": row.get("health_meta"),
                    }
                )
            )
        return tuple(facts)

    async def stats_rows(self) -> tuple[BackendStatsFact, ...]:
        result = await self.db.execute(_STATS_ROWS)
        facts: list[BackendStatsFact] = []
        for value in result.scalars():
            row = _native_json(value)
            facts.append(
                BackendStatsFact.model_validate(
                    {
                        "registry_id": uuid.UUID(row["registry_id"]),
                        "name": row.get("name"),
                        "url": row.get("url"),
                        "state": row.get("state"),
                        "source": row.get("source"),
                        "is_interactive": row.get("is_interactive"),
                        "health_meta": row.get("health_meta"),
                        "created_at": _timestamp(row.get("created_at")),
                    }
                )
            )
        return tuple(facts)

    async def stats_snapshots(
        self,
    ) -> tuple[BackendStatsSnapshot, ...]:
        request = BackendStatsSnapshotsRequest(
            kind="owner_stats_snapshots",
            schema=BACKEND_CONTROL_PROTOCOL,
            request_id=uuid.uuid4(),
        )
        result = await self._request_only_result(request)
        if set(result) != {"snapshots"} or type(result["snapshots"]) is not list:
            raise BackendControlUnavailable()
        try:
            reply = BackendStatsSnapshotsResult.model_validate_json(
                json.dumps(
                    {
                        "request_id": str(request.request_id),
                        "snapshots": result["snapshots"],
                    },
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError):
            raise BackendControlUnavailable() from None
        if reply.request_id != request.request_id:
            raise BackendControlUnavailable()
        return reply.snapshots

    async def registration_labels(
        self, urls: list[str]
    ) -> tuple[BackendRegistrationLabel, ...]:
        if any(type(url) is not str for url in urls):
            raise ValueError("registration URLs must be strings")
        result = await self.db.execute(_REGISTRATION_LABELS, {"urls": urls})
        return tuple(
            BackendRegistrationLabel(url=row.url, source=row.source) for row in result
        )


def backend_control_service(db: AsyncSession) -> BackendControlService:
    from app.services.inference_control_context import (
        backend_control_http_client,
        backend_control_operation_context_factory,
    )

    class _RequestOnlyClient:
        async def execute(self, request):
            return await backend_control_http_client().execute(request)

    return BackendControlService(
        db,
        operation_context_factory=backend_control_operation_context_factory(db),
        request_only_client=_RequestOnlyClient(),
    )


__all__ = [
    "BackendControlClient",
    "BackendControlOperationContext",
    "BackendControlOperationContextFactory",
    "BackendControlOperationError",
    "BackendControlRequestOnlyClient",
    "BackendControlService",
    "backend_control_service",
    "capture_backend_endpoint_subject",
]

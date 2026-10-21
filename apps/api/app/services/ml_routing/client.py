"""Route-aware ML backend client for prediction traffic.

Lifecycle and discovery calls intentionally stay instance-addressed. Prediction
callers use this adapter so a requested registry first resolves to its logical
service pool and then to the router-selected physical instance.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.gpu_arbitration.contracts import (
    GPUDispatchContextFactory,
    GPUShadowSessionFactory,
)
from app.services.ml_routing.contracts import (
    RejectionReason,
    RouteSelection,
    RouterMode,
    RoutingError,
)
from app.services.ml_routing.ledger import RoutingLedger
from app.services.ml_routing.router import (
    MLBackendRouter,
    _router_mode,
    make_ledger_from_settings,
    route_context,
)


LedgerFactory = Callable[[], RoutingLedger]


class RoutedMLBackendClient:
    """Route each prediction call through the requested registry's service pool."""

    def __init__(
        self,
        db: AsyncSession,
        requested_registry: MLBackendRegistry | uuid.UUID,
        *,
        project_id: uuid.UUID,
        owner: str,
        operation: str,
        shadow_session_factory: GPUShadowSessionFactory | None = None,
        dispatch_context_factory: GPUDispatchContextFactory | None = None,
        mode: RouterMode | None = None,
        ledger_factory: LedgerFactory | None = None,
    ) -> None:
        self.db = db
        if isinstance(requested_registry, uuid.UUID):
            self.requested_registry_id: uuid.UUID | None = requested_registry
            self.requested_backend: MLBackendRegistry | None = None
        else:
            requested_id = getattr(requested_registry, "id", None)
            self.requested_registry_id = (
                requested_id if isinstance(requested_id, uuid.UUID) else None
            )
            self.requested_backend = requested_registry
        self.project_id = project_id
        self.owner = owner
        self.operation = operation
        self.shadow_session_factory = shadow_session_factory
        self.dispatch_context_factory = dispatch_context_factory
        self.mode = mode or _router_mode()
        self.ledger_factory = ledger_factory or make_ledger_from_settings
        self.pool_id: uuid.UUID | None = None
        self.last_instance_id: uuid.UUID | None = None
        self.last_selection: RouteSelection | None = None
        self._transports: dict[object, Any] = {}

    async def _pool(self) -> MLBackendServicePool | None:
        if self.requested_registry_id is None:
            return None
        result = await self.db.execute(
            select(MLBackendServicePool)
            .join(
                MLBackendPoolMember,
                MLBackendPoolMember.pool_id == MLBackendServicePool.id,
            )
            .where(MLBackendPoolMember.registry_id == self.requested_registry_id)
        )
        return result.scalars().first()

    def _transport(self, backend: MLBackendRegistry):
        # Import the module at call time so existing tests and integrations that
        # replace MLBackendClient keep observing the same seam.
        from app.services import ml_client as ml_client_module

        key = getattr(backend, "id", id(backend))
        transport = self._transports.get(key)
        if transport is None:
            transport = ml_client_module.MLBackendClient(
                backend,
                shadow_session_factory=self.shadow_session_factory,
                dispatch_context_factory=self.dispatch_context_factory,
            )
            self._transports[key] = transport
        return transport

    @asynccontextmanager
    async def _routed_transport(
        self, *, required_instance_id: uuid.UUID | None = None
    ) -> AsyncIterator[Any]:
        pool = await self._pool()
        if pool is None:
            if required_instance_id is not None:
                raise RoutingError(
                    RejectionReason.POOL_UNAVAILABLE,
                    "controlled models require a verified service pool",
                )
            # Production registries are singleton-backfilled. Keep off/observe
            # compatible with pre-backfill rows and lightweight unit fixtures;
            # enforce must fail closed when no logical route can be resolved.
            if self.mode == RouterMode.ENFORCE:
                raise RoutingError(
                    RejectionReason.POOL_UNAVAILABLE,
                    f"registry {self.requested_registry_id} has no service pool",
                )
            backend = self.requested_backend
            if backend is None and self.requested_registry_id is not None:
                backend = await self.db.get(
                    MLBackendRegistry, self.requested_registry_id
                )
            if backend is None:
                raise RoutingError(
                    RejectionReason.POOL_UNAVAILABLE,
                    f"registry {self.requested_registry_id} not found",
                )
            self.pool_id = None
            self.last_instance_id = getattr(backend, "id", None)
            self.last_selection = RouteSelection(
                lease=None,
                instance_id=self.last_instance_id,
                rejection=None,
            )
            yield self._transport(backend)
            return

        self.pool_id = pool.id
        ledger: RoutingLedger | None = None
        if self.mode != RouterMode.OFF:
            try:
                ledger = self.ledger_factory()
            except Exception:
                # Circuit hints cannot gate durable execution ownership.
                ledger = None
        try:
            routed = MLBackendRouter(self.db, ledger=ledger, mode=self.mode)
            async with route_context(
                routed,
                pool.id,
                owner=self.owner,
                operation=self.operation,
                project_id=self.project_id,
                **(
                    {"required_instance_id": required_instance_id}
                    if required_instance_id is not None
                    else {}
                ),
            ) as (instance_id, _lease, selection):
                if instance_id is None:
                    raise RoutingError(
                        RejectionReason.POOL_UNAVAILABLE,
                        f"service pool {pool.id} selected no registry instance",
                    )
                backend = await self.db.get(MLBackendRegistry, instance_id)
                if backend is None:
                    raise RoutingError(
                        RejectionReason.POOL_UNAVAILABLE,
                        f"selected registry {instance_id} not found",
                    )
                self.last_instance_id = backend.id
                self.last_selection = selection
                yield self._transport(backend)
        finally:
            if ledger is not None:
                await ledger.aclose()

    async def predict(
        self,
        tasks: list[dict],
        context: dict | None = None,
        *,
        max_response_bytes: int | None = None,
    ) -> list[Any]:
        from app.services import trained_models
        from app.services.trained_model_reference_contract import (
            FreezeModelReferenceError,
            ValidateFrozenModelReferenceRequest,
        )

        model_id = (context or {}).get("model_id") or ""
        ref = (context or {}).get("frozen_model_ref")
        controlled = (
            model_id.startswith("trained:")
            or model_id == "project-release"
            or ref is not None
        )
        required_instance_id = None
        wire = context
        if controlled:
            if not ref or model_id != f"trained:{ref.get('model_version_id')}":
                raise ValueError(
                    "Controlled prediction requires a frozen model identity"
                )
            try:
                required_instance_id = await trained_models.bind_frozen_model_reference(
                    self.db
                ).validate(
                    ValidateFrozenModelReferenceRequest(
                        project_id=self.project_id,
                        reference=ref,
                    )
                )
            except FreezeModelReferenceError as exc:
                trained_models.raise_frozen_model_reference_error(exc)
            if self.requested_registry_id != required_instance_id:
                raise ValueError(
                    "Requested registry differs from the frozen deployment"
                )
            wire = trained_models.wire_context(ref, (context or {}).get("params"))
            if (context or {}).get("classes"):
                wire["classes"] = context["classes"]
            if (context or {}).get("sample_evaluation") is not None:
                wire["sample_evaluation"] = context["sample_evaluation"]
        route_args = (
            {"required_instance_id": required_instance_id} if controlled else {}
        )
        async with self._routed_transport(**route_args) as client:
            response_limit = (
                {"max_response_bytes": max_response_bytes}
                if max_response_bytes is not None
                else {}
            )
            if context is None:
                return await client.predict(tasks, **response_limit)
            results = await client.predict(tasks, context=wire, **response_limit)
            if controlled:
                expected_tasks = {str(task["id"]) for task in tasks}
                if (
                    len(results) != len(tasks)
                    or {str(result.task_id) for result in results} != expected_tasks
                ):
                    raise ValueError(
                        "Controlled prediction returned different task identities"
                    )
                trained_models.validate_and_map_results(results, ref)
            return results

    async def predict_interactive(
        self,
        task_data: dict,
        context: dict,
        *,
        max_response_bytes: int | None = None,
    ) -> Any:
        if (
            (context.get("model_id") or "").startswith("trained:")
            or context.get("model_id") == "project-release"
            or context.get("frozen_model_ref")
        ):
            raise ValueError("Controlled models do not support interactive prediction")
        async with self._routed_transport() as client:
            response_limit = (
                {"max_response_bytes": max_response_bytes}
                if max_response_bytes is not None
                else {}
            )
            return await client.predict_interactive(
                task_data=task_data,
                context=context,
                **response_limit,
            )

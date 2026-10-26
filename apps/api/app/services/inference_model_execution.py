"""Business-UOW adapter for the credential-owning Inference execution service."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import replace

from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionError,
    ModelExecutionPort,
    ModelExecutionRouteBinding,
    ModelOperationResult,
    freeze_json,
    thaw_json,
)
from aap_inference_contract.execution_client import ModelExecutionHttpClient
from aap_inference_contract.routing import RoutingScopeView
from aap_workspace.config import secret_source

from app.config import settings
from app.observability.metrics import observe_ml_backend
from app.services.inference_topology_data import InferenceTopologyData
from app.services.ml_routing.authority import RoutingAuthority
from app.services.ml_routing.contracts import (
    RejectionReason,
    RouteOutcome,
    RouterMode,
    RoutingError,
)
from app.services.ml_routing.router import (
    MLBackendRouter,
    _router_mode,
    make_ledger_from_settings,
)
from app.services.trained_model_reference_contract import FrozenModelReference

logger = logging.getLogger(__name__)


class _ModelExecutionGateway(ModelExecutionPort):
    def __init__(self, db, *, bound_route=None, installation_trial=None) -> None:
        self.db = db
        if bound_route is not None and not isinstance(bound_route, RoutingScopeView):
            raise TypeError("Pinned execution requires the existing RoutingScopeView")
        self.bound_route = bound_route
        self.installation_trial = (
            None if installation_trial is None else freeze_json(installation_trial)
        )

    async def _controlled_context(self, request):
        from app.services import trained_models
        from app.services.trained_model_reference_contract import (
            FreezeModelReferenceError,
            ValidateFrozenModelReferenceRequest,
        )

        context = request.context or {}
        model_id = context.get("model_id") or ""
        reference = context.get("frozen_model_ref")
        controlled = (
            model_id.startswith("trained:")
            or model_id == "project-release"
            or reference is not None
            or self.installation_trial is not None
        )
        if not controlled:
            return request, None
        if request.operation != "batch_predict":
            raise ValueError("Controlled models do not support interactive prediction")
        if not isinstance(reference, Mapping) or model_id != (
            f"trained:{reference.get('model_version_id')}"
        ):
            raise ValueError("Controlled prediction requires a frozen model identity")
        reference = thaw_json(reference)
        if self.installation_trial is not None:
            from app.services.training_dataset_bridge import content_digest

            if (
                reference != thaw_json(self.installation_trial)
                or reference.get("project_id") != str(request.project_id)
                or reference.get("registry_id") != str(request.requested_registry_id)
                or content_digest(
                    {
                        key: value
                        for key, value in reference.items()
                        if key != "reference_digest"
                    }
                )
                != reference.get("reference_digest")
            ):
                raise ValueError("Installation trial reference identity mismatch")
            required_instance_id = uuid.UUID(reference["registry_id"])
        else:
            try:
                required_instance_id = await trained_models.bind_frozen_model_reference(
                    self.db
                ).validate(
                    ValidateFrozenModelReferenceRequest(
                        project_id=request.project_id, reference=reference
                    )
                )
            except FreezeModelReferenceError as error:
                trained_models.raise_frozen_model_reference_error(error)
        if request.requested_registry_id != required_instance_id or (
            request.required_instance_id is not None
            and request.required_instance_id != required_instance_id
        ):
            raise ValueError("Requested registry differs from the frozen deployment")
        wire = trained_models.wire_context(reference, thaw_json(context.get("params")))
        if context.get("classes"):
            wire["classes"] = thaw_json(context["classes"])
        if context.get("sample_evaluation") is not None:
            wire["sample_evaluation"] = thaw_json(context["sample_evaluation"])
        return replace(
            request, context=wire, required_instance_id=required_instance_id
        ), reference

    async def execute(self, request: FrozenModelOperation) -> ModelOperationResult:
        if not isinstance(request, FrozenModelOperation):
            raise TypeError("Model execution requires a frozen logical operation")
        if request.route_binding is not None or request.endpoint_subject is not None:
            raise ValueError(
                "The API gateway, not its caller, binds execution authority"
            )
        if request.operation == "pinned_tracker" and self.bound_route is None:
            raise ValueError(
                "Pinned tracker execution requires its existing route scope"
            )
        request, reference = await self._controlled_context(request)
        data = InferenceTopologyData(self.db)
        authority = RoutingAuthority()
        ledger = None
        lease = None
        router = None
        scope = self.bound_route
        succeeded = False
        try:
            if scope is None:
                pool = (
                    await data.pool_reference(request.requested_pool_id)
                    if request.requested_pool_id is not None
                    else await data.pool_for_registry(request.requested_registry_id)
                    if request.requested_registry_id is not None
                    else None
                )
                mode = _router_mode()
                if self.installation_trial is not None:
                    pool = None
                elif pool is None and (
                    mode == RouterMode.ENFORCE
                    or request.required_instance_id is not None
                ):
                    raise RoutingError(
                        RejectionReason.POOL_UNAVAILABLE,
                        "Prediction requires a verified service pool",
                    )
                if mode != RouterMode.OFF:
                    try:
                        ledger = make_ledger_from_settings()
                    except Exception:
                        # Circuit hints cannot replace durable execution admission.
                        ledger = None
                router = MLBackendRouter(
                    self.db, ledger=ledger, mode=mode, authority=authority
                )
                selection = await router.acquire(
                    pool.id if pool is not None else None,
                    registry_id=request.requested_registry_id if pool is None else None,
                    project_id=request.project_id,
                    owner=request.route_owner,
                    operation=request.route_operation,
                    required_instance_id=request.required_instance_id,
                )
                if selection.rejection is not None:
                    raise RoutingError(
                        selection.rejection, "Backend execution admission rejected"
                    )
                lease = selection.lease
                if lease is None or lease.scope is None:
                    raise RoutingError(
                        RejectionReason.ROUTER_UNAVAILABLE,
                        "Durable routing owner returned no execution scope",
                    )
                scope = lease.scope
                requested_pool_id = (
                    pool.id if pool is not None else request.requested_pool_id
                )
            else:
                requested_pool_id = request.requested_pool_id or scope.pool_id
                if (
                    scope.closed
                    or scope.registry_id != request.requested_registry_id
                    or (
                        scope.project_id is not None
                        and scope.project_id != request.project_id
                    )
                    or (
                        request.required_instance_id is not None
                        and scope.registry_id != request.required_instance_id
                    )
                ):
                    raise RoutingError(
                        RejectionReason.POOL_UNAVAILABLE,
                        "Pinned route identity mismatch",
                    )
            backend = await data.registry_facts(scope.registry_id)
            if backend is None:
                raise RoutingError(
                    RejectionReason.POOL_UNAVAILABLE, "Selected backend is unavailable"
                )
            member_id = None
            if scope.pool_id is not None:
                members = await data.pool_members(scope.pool_id)
                member_id = next(
                    (
                        member.id
                        for member in members
                        if member.registry_id == scope.registry_id
                    ),
                    None,
                )
                if member_id is None:
                    raise RoutingError(
                        RejectionReason.GENERATION_MISMATCH,
                        "Selected membership is unavailable",
                    )
            binding = ModelExecutionRouteBinding(
                scope.id, request.request_id, scope.capability
            )
            from app.services.inference_backend_control import (
                capture_backend_endpoint_subject,
            )

            subject = await capture_backend_endpoint_subject(
                self.db,
                scope.registry_id,
                request_id=request.request_id,
                purpose="execution",
                binding=binding.endpoint_binding(),
            )
            bound = request.with_authority(
                route_binding=binding,
                endpoint_subject=subject,
                endpoint_registry_id=scope.registry_id,
                requested_pool_id=requested_pool_id,
                selected_pool_id=scope.pool_id,
                selected_member_id=member_id,
                topology_generation=scope.generation,
                health_meta=backend.health_meta,
            )
            client = ModelExecutionHttpClient(
                settings.inference_model_execution_url,
                secret_source(
                    settings.inference_model_execution_token,
                    settings.inference_model_execution_token_file,
                ),
            )
            try:
                result = await client.execute(bound)
            except ModelExecutionError as error:
                self._observe(error.failure.observation)
                raise
            finally:
                await client.aclose()
            self._observe(result.observation)
            if reference is not None:
                from app.services import trained_models

                result = replace(
                    result,
                    results=trained_models.validate_and_map_results(
                        result.results, reference
                    ),
                )
            succeeded = True
            return result
        finally:
            if lease is not None:
                try:
                    if succeeded:
                        await router.finish(lease, RouteOutcome.SUCCESS, duration_ms=0)
                    else:
                        await router.cancel(lease)
                except Exception:
                    # An unresolved HTTP attempt must retain its durable scope.
                    logger.warning(
                        "model_execution_scope_close_unacknowledged", exc_info=True
                    )
            if ledger is not None:
                await ledger.aclose()

    @staticmethod
    def _observe(observation) -> None:
        if observation is not None:
            observe_ml_backend(
                observation.backend_id, observation.outcome, observation.elapsed_seconds
            )


def model_execution_port(db, *, bound_route=None) -> ModelExecutionPort:
    """Select and capture in the caller UOW; only Inference opens stored credentials."""
    return _ModelExecutionGateway(db, bound_route=bound_route)


def installation_trial_model_execution_port(
    db, reference: FrozenModelReference
) -> ModelExecutionPort:
    """Run the already-receipt-checked fixed validation image before deployment readiness."""
    return _ModelExecutionGateway(db, installation_trial=reference)

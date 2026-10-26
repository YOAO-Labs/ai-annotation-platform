"""Caller-owned logical routing bindings for registered Inference control RPCs."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aap_inference_contract.backend_control_client import BackendControlHttpClient
from aap_inference_contract.backend_endpoint import BackendEndpointBinding
from aap_workspace.config import secret_source

from app.config import settings
from app.services.ml_routing.authority import RoutingAuthority, lifecycle_control

_READ_OPERATIONS = frozenset(
    {
        "setup",
        "health_meta",
        "catalog_fallback",
        "stats",
        "trained_model_receipt",
        "managed_evaluation_profile",
        "managed_evaluation_receipt",
        "versions",
    }
)
_CONTROL_OPERATIONS = frozenset({"unload", "reload"})
_SCOPE_OPERATIONS = frozenset(
    {"warmup", "install_trained_model", "managed_evaluation_seal"}
)


def backend_control_http_client() -> BackendControlHttpClient:
    return BackendControlHttpClient(
        settings.inference_backend_control_url,
        secret_source(
            settings.inference_backend_control_token,
            settings.inference_backend_control_token_file,
        ),
    )


@dataclass(frozen=True)
class _OperationContext:
    request_id: uuid.UUID
    binding: BackendEndpointBinding | None
    client: BackendControlHttpClient
    db: object

    async def commit_before_io(self) -> None:
        await self.db.commit()


def backend_control_operation_context_factory(db):
    @asynccontextmanager
    async def operation_context(operation, registry_id, *, expected_topology=None):
        request_id = uuid.uuid4()
        client = backend_control_http_client()
        if operation in _READ_OPERATIONS:
            yield _OperationContext(request_id, None, client, db)
        elif operation in _CONTROL_OPERATIONS:
            async with lifecycle_control(
                db,
                registry_id,
                operation,
                expected_topology=expected_topology,
            ) as control_binding:
                control = control_binding.control
                binding = BackendEndpointBinding(
                    kind="control",
                    scope_id=None,
                    control_id=control.control_id,
                    attempt_id=request_id,
                    capability=control.capability,
                )
                yield _OperationContext(request_id, binding, client, db)
        elif operation in _SCOPE_OPERATIONS:
            authority = RoutingAuthority()
            scope = await authority.acquire(
                registry_id=registry_id,
                mode="direct",
                owner=f"backend-control:{operation}",
                operation="backend_http",
            )
            succeeded = False
            try:
                binding = BackendEndpointBinding(
                    kind="scope",
                    scope_id=scope.id,
                    control_id=None,
                    attempt_id=request_id,
                    capability=scope.capability,
                )
                yield _OperationContext(request_id, binding, client, db)
                succeeded = True
            finally:
                await authority.close(scope, "success" if succeeded else "cancel")
        else:
            raise ValueError("Unsupported registered backend control operation")

    return operation_context

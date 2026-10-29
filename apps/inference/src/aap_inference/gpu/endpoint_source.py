"""Private GPU-role endpoint capture/opening inside one short Native transaction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol
from types import SimpleNamespace
from uuid import UUID

from aap_inference.backend_endpoint import BackendEndpointSnapshot, _snapshot
from aap_inference_contract.backend_endpoint import (
    BackendEndpointAuthority,
    BackendEndpointBinding,
    BackendEndpointPurpose,
    backend_endpoint_policy_ready,
    make_backend_endpoint_subject,
)


_ENDPOINT_SCHEMA = "aap_inference_backend_endpoint_v1"
_MAX_ENDPOINT_JSON_BYTES = 64 * 1024
_ENDPOINT_POLICY_SQL = (
    "SELECT aap_inference_backend_endpoint_v1.backend_endpoint_policy()"
)
_ENDPOINT_CAPTURE_SQL = (
    "SELECT aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject("
    "$1::uuid,$2::uuid,$3::text,$4::text)"
)
_ENDPOINT_OPEN_HEALTH_SQL = (
    "SELECT aap_inference_backend_endpoint_v1.open_health_observation_endpoint("
    "$1::text,$2::uuid,$3::uuid,$4::text)"
)
_ENDPOINT_OPEN_GPU_CONTROL_SQL = (
    "SELECT aap_inference_backend_endpoint_v1.open_gpu_control_endpoint("
    "$1::text,$2::uuid,$3::uuid,$4::text)"
)
_ENDPOINT_OPEN_TERMINAL_SQL = (
    "SELECT aap_inference_backend_endpoint_v1.open_installation_terminal_endpoint("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::text)"
)
_ENDPOINT_FUNCTIONS = {
    ("backend_endpoint_policy", ""),
    ("capture_backend_endpoint_subject", "uuid, uuid, text, text"),
}
_ENDPOINT_OPENERS = {
    "health_observation": (
        "open_health_observation_endpoint",
        "text, uuid, uuid, text",
        _ENDPOINT_OPEN_HEALTH_SQL,
    ),
    "gpu_control": (
        "open_gpu_control_endpoint",
        "text, uuid, uuid, text",
        _ENDPOINT_OPEN_GPU_CONTROL_SQL,
    ),
}


class _EndpointConnection(Protocol):
    async def fetchval(self, query: str, *args: Any) -> Any: ...

    def transaction(self) -> AbstractAsyncContextManager[None]: ...


class _EndpointPool(Protocol):
    def acquire(self) -> AbstractAsyncContextManager[_EndpointConnection]: ...


class OwnedBackendEndpointSource(Protocol):
    """GPU-private endpoint source; never expose raw subjects to ordinary callers."""

    async def open_owned(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None = None,
    ) -> BackendEndpointSnapshot: ...

    async def open_installation_terminal(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        request: dict[str, Any],
        preparation: dict[str, Any],
        endpoint_sha256: str,
    ) -> BackendEndpointSnapshot: ...

    async def readiness(self) -> bool: ...


class GPUOwnedBackendEndpointSource:
    """Capture and open endpoints through purpose-scoped owner database roles.

    Each supplied pool is private to the Inference owner and connected with its
    matching GPU endpoint profile. Capture and open happen within the same short
    transaction; this method returns only after PostgreSQL has released the
    connection, before the caller begins backend HTTP.
    """

    __slots__ = ("_pools",)

    def __init__(self, pools: Mapping[str, _EndpointPool]) -> None:
        if set(pools) != set(_ENDPOINT_OPENERS):
            raise ValueError("GPU endpoint pools must exactly match owned purposes")
        self._pools = dict(pools)

    async def open_owned(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        purpose: BackendEndpointPurpose,
        binding: BackendEndpointBinding | None = None,
    ) -> BackendEndpointSnapshot:
        opener = _ENDPOINT_OPENERS.get(purpose)
        if opener is None:
            raise ValueError("GPU endpoint purpose is not owned")
        if purpose == "health_observation" and binding is not None:
            raise ValueError("health observation cannot carry a control binding")
        if purpose == "gpu_control" and binding is None:
            raise ValueError("GPU control endpoint requires a live victim binding")

        binding_json = _compact_model_json(binding)
        authority = (
            None
            if binding is None
            else BackendEndpointAuthority(
                kind=binding.kind,
                scope_id=binding.scope_id,
                control_id=binding.control_id,
                attempt_id=binding.attempt_id,
            )
        )
        authority_json = _compact_model_json(authority)
        pool = self._pools[purpose]
        async with pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await _verify_endpoint_profile(connection, purpose)
                    policy = _decode_object(
                        await connection.fetchval(_ENDPOINT_POLICY_SQL)
                    )
                    if not backend_endpoint_policy_ready(policy):
                        raise GPUOwnedEndpointUnavailable()
                    capture = _decode_object(
                        await connection.fetchval(
                            _ENDPOINT_CAPTURE_SQL,
                            registry_id,
                            request_id,
                            purpose,
                            binding_json,
                        )
                    )
                    if set(capture) != {"status", "subject"}:
                        raise GPUOwnedEndpointUnavailable()
                    if capture["status"] != "ok" or type(capture["subject"]) is not str:
                        raise GPUOwnedEndpointUnavailable()
                    subject = make_backend_endpoint_subject(
                        registry_id,
                        request_id=request_id,
                        purpose=purpose,
                        binding=binding,
                        subject=capture["subject"],
                    )
                    raw_snapshot = await connection.fetchval(
                        opener[2],
                        subject.subject,
                        registry_id,
                        request_id,
                        authority_json,
                    )
                    if type(raw_snapshot) is not str:
                        raise GPUOwnedEndpointUnavailable()
                    snapshot = _snapshot(raw_snapshot, subject)
                except GPUOwnedEndpointUnavailable:
                    raise
                except Exception:
                    raise GPUOwnedEndpointUnavailable() from None
        return snapshot

    async def open_installation_terminal(
        self,
        registry_id: UUID,
        *,
        request_id: UUID,
        request: dict[str, Any],
        preparation: dict[str, Any],
        endpoint_sha256: str,
    ) -> BackendEndpointSnapshot:
        """Open the durable terminal intent under the GPU endpoint role."""
        if not isinstance(registry_id, UUID) or not isinstance(request_id, UUID):
            raise GPUOwnedEndpointUnavailable()
        try:
            request_json = _compact_object_json(request)
            preparation_json = _compact_object_json(preparation)
            async with self._pools["gpu_control"].acquire() as connection:
                async with connection.transaction():
                    await _verify_endpoint_profile(connection, "gpu_control")
                    policy = _decode_object(
                        await connection.fetchval(_ENDPOINT_POLICY_SQL)
                    )
                    if not backend_endpoint_policy_ready(policy):
                        raise GPUOwnedEndpointUnavailable()
                    raw = await connection.fetchval(
                        _ENDPOINT_OPEN_TERMINAL_SQL,
                        registry_id,
                        request_id,
                        request_json,
                        preparation_json,
                        endpoint_sha256,
                    )
                    if type(raw) is not str:
                        raise GPUOwnedEndpointUnavailable()
                    subject = SimpleNamespace(
                        registry_id=registry_id,
                        request_id=request_id,
                        purpose="terminal_drain",
                        authority=None,
                    )
                    snapshot = _snapshot(raw, subject)
                    if snapshot.endpoint_sha256 != endpoint_sha256:
                        raise GPUOwnedEndpointUnavailable()
                    return snapshot
        except GPUOwnedEndpointUnavailable:
            raise
        except Exception:
            raise GPUOwnedEndpointUnavailable() from None

    async def readiness(self) -> bool:
        try:
            for purpose in _ENDPOINT_OPENERS:
                async with self._pools[purpose].acquire() as connection:
                    async with connection.transaction():
                        await _verify_endpoint_profile(connection, purpose)
                        policy = _decode_object(
                            await connection.fetchval(_ENDPOINT_POLICY_SQL)
                        )
                        if not backend_endpoint_policy_ready(policy):
                            return False
            return True
        except Exception:
            return False


class GPUOwnedEndpointUnavailable(RuntimeError):
    """The private endpoint source could not safely capture/open a live endpoint."""

    def __init__(self) -> None:
        super().__init__("GPU-owned backend endpoint is unavailable")


async def _verify_endpoint_profile(
    connection: _EndpointConnection,
    purpose: BackendEndpointPurpose,
) -> None:
    opener = _ENDPOINT_OPENERS[purpose]
    from aap_owner_data.postgres import verify_function_only_role

    functions = _ENDPOINT_FUNCTIONS | {(opener[0], opener[1])}
    if purpose == "gpu_control":
        functions.add(
            ("open_installation_terminal_endpoint", "uuid, uuid, text, text, text")
        )
    await verify_function_only_role(
        connection,
        schema=_ENDPOINT_SCHEMA,
        functions=functions,
        role=await connection.fetchval("SELECT session_user"),
    )


def _compact_object_json(value: dict[str, Any]) -> str:
    if type(value) is not dict:
        raise ValueError("terminal endpoint input must be an object")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8", errors="strict")) > _MAX_ENDPOINT_JSON_BYTES:
        raise ValueError("terminal endpoint input is too large")
    return encoded


def _compact_model_json(value: Any) -> str | None:
    if value is None:
        return None
    if not hasattr(value, "model_dump"):
        raise ValueError("backend endpoint authority value is malformed")
    try:
        encoded = json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise ValueError("backend endpoint authority value is malformed") from None
    if len(encoded.encode("utf-8", errors="strict")) > _MAX_ENDPOINT_JSON_BYTES:
        raise ValueError("backend endpoint authority value is too large")
    return encoded


def _decode_object(value: Any) -> dict[str, Any]:
    try:
        if isinstance(value, Mapping):
            raw = json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
            if len(raw) > _MAX_ENDPOINT_JSON_BYTES:
                raise GPUOwnedEndpointUnavailable()
            result = json.loads(raw, object_pairs_hook=_unique_object)
        elif type(value) is str:
            raw = value.encode("utf-8", errors="strict")
            if len(raw) > _MAX_ENDPOINT_JSON_BYTES:
                raise GPUOwnedEndpointUnavailable()
            result = json.loads(raw, object_pairs_hook=_unique_object)
        elif type(value) is bytes:
            if len(value) > _MAX_ENDPOINT_JSON_BYTES:
                raise GPUOwnedEndpointUnavailable()
            result = json.loads(value, object_pairs_hook=_unique_object)
        else:
            raise ValueError
    except GPUOwnedEndpointUnavailable:
        raise
    except (UnicodeError, TypeError, ValueError, RecursionError, OverflowError):
        raise GPUOwnedEndpointUnavailable() from None
    if type(result) is not dict:
        raise GPUOwnedEndpointUnavailable()
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate backend endpoint field")
        result[key] = value
    return result

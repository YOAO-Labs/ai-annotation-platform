"""Authenticated, single-shot HTTP client for the model execution owner."""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

from .execution import (
    ExecutionFailure,
    FrozenModelOperation,
    ModelExecutionError,
    ModelExecutionPort,
    ModelOperationResult,
    decode_model_operation_result,
    model_operation_request_bytes,
)

MODEL_EXECUTION_PATH = "/v1/model-execution/execute"


class ModelExecutionUnavailable(RuntimeError):
    """The remote model execution owner cannot be contacted safely."""


class ModelExecutionHttpClient(ModelExecutionPort):
    """Send one model request once; an untrusted acknowledgement is never replayed."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float | None = None,
        transport=None,
    ) -> None:
        if (
            type(base_url) is not str
            or not base_url
            or any(character.isspace() for character in base_url)
            or type(token) is not str
            or not token
            or not token.isascii()
            or any(character.isspace() for character in token)
            or (
                timeout_seconds is not None
                and (
                    type(timeout_seconds) not in {int, float}
                    or isinstance(timeout_seconds, bool)
                    or not math.isfinite(float(timeout_seconds))
                    or timeout_seconds <= 0
                )
            )
        ):
            raise ModelExecutionUnavailable()
        parsed = urlsplit(base_url)
        try:
            _ = parsed.port
        except ValueError:
            raise ModelExecutionUnavailable() from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ModelExecutionUnavailable()
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = (
            None if timeout_seconds is None else float(timeout_seconds)
        )
        self._transport = transport
        self._client = None

    async def _http_client(self):
        if self._client is None:
            try:
                import httpx
            except ImportError:
                raise ModelExecutionUnavailable() from None
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    async def execute(self, request: FrozenModelOperation) -> ModelOperationResult:
        if not isinstance(request, FrozenModelOperation):
            raise ModelExecutionUnavailable()
        if (
            request.route_binding is None
            or request.endpoint_subject is None
            or request.endpoint_registry_id is None
        ):
            raise ModelExecutionError(
                ExecutionFailure(
                    request_id=request.request_id,
                    kind="invalid_request",
                    code="model_execution_authority_required",
                    status_code=400,
                    detail={"reason": "endpoint_and_route_authority_required"},
                )
            )
        try:
            import httpx
        except ImportError:
            raise ModelExecutionUnavailable() from None
        body = model_operation_request_bytes(request)
        client = await self._http_client()
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}{MODEL_EXECUTION_PATH}",
                content=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                content = await _read_response(response)
                if response.status_code != 200:
                    failure = _decode_failure(content)
                    if (
                        failure is None
                        or failure.request_id != request.request_id
                        or failure.status_code != response.status_code
                        or not _failure_matches_request(failure, request)
                    ):
                        raise _uncertain_failure(request, response.status_code)
                    raise ModelExecutionError(failure)
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if (
                    content_type != "application/json"
                    or response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    raise _uncertain_failure(request)
                result = decode_model_operation_result(content)
                provenance = result.provenance
                route = request.route_binding
                expected_observation_operation = (
                    "predict"
                    if request.operation == "batch_predict"
                    else "predict_interactive"
                )
                expected_task_ids = {item["id"] for item in request.task_inputs}
                if (
                    result.request_id != request.request_id
                    or result.operation != request.operation
                    or provenance.requested_registry_id != request.requested_registry_id
                    or provenance.requested_pool_id != request.requested_pool_id
                    or provenance.registry_id != request.endpoint_registry_id
                    or provenance.instance_id != request.endpoint_registry_id
                    or (
                        request.required_instance_id is not None
                        and provenance.instance_id != request.required_instance_id
                    )
                    or provenance.pool_id != request.selected_pool_id
                    or provenance.member_id != request.selected_member_id
                    or provenance.topology_generation != request.topology_generation
                    or provenance.route_scope_id != route.scope_id
                    or provenance.attempt_id != request.request_id
                    or result.observation.backend_id != str(provenance.instance_id)
                    or result.observation.operation != expected_observation_operation
                    or result.observation.outcome != "success"
                    or any(
                        item.task_id not in expected_task_ids for item in result.results
                    )
                    or (
                        request.operation != "batch_predict"
                        and result.task_ids != (request.task_inputs[0]["id"],)
                    )
                ):
                    raise _uncertain_failure(request)
                return result
        except ModelExecutionError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise _uncertain_failure(request) from None
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise _uncertain_failure(request) from None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


async def _read_response(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_failure(content: bytes) -> ExecutionFailure | None:
    try:
        value = json.loads(content)
        return ExecutionFailure.from_wire(value)
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return None


def _failure_matches_request(
    failure: ExecutionFailure, request: FrozenModelOperation
) -> bool:
    provenance = failure.provenance
    if provenance is None:
        return failure.observation is None
    route = request.route_binding
    if route is None:
        return False
    if (
        provenance.requested_registry_id != request.requested_registry_id
        or provenance.requested_pool_id != request.requested_pool_id
        or provenance.registry_id != request.endpoint_registry_id
        or provenance.instance_id != request.endpoint_registry_id
        or (
            request.required_instance_id is not None
            and provenance.instance_id != request.required_instance_id
        )
        or provenance.pool_id != request.selected_pool_id
        or provenance.member_id != request.selected_member_id
        or provenance.topology_generation != request.topology_generation
        or provenance.route_scope_id != route.scope_id
        or provenance.attempt_id != request.request_id
    ):
        return False
    observation = failure.observation
    if observation is None:
        return True
    expected_operation = (
        "predict" if request.operation == "batch_predict" else "predict_interactive"
    )
    return (
        observation.backend_id == str(provenance.instance_id)
        and observation.operation == expected_operation
    )


def _uncertain_failure(
    request: FrozenModelOperation, status_code: int | None = None
) -> ModelExecutionError:
    return ModelExecutionError(
        ExecutionFailure(
            request_id=request.request_id,
            kind="uncertain",
            code="model_execution_ack_unavailable",
            status_code=(
                status_code
                if type(status_code) is int and 400 <= status_code <= 599
                else None
            ),
            detail={"reason": "untrusted_execution_response"},
            uncertain=True,
        )
    )

"""Fence and prove terminal quiescence of one GPU plugin installation."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from aap_protocol_v2.lifecycle import AdmissionScope, AdmissionTokenClaims
from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference.gpu.contracts import (
    GPUArbiterDispatchError,
    GPUArbiterErrorCode,
    GPUDispatchGrant,
)
from aap_inference.gpu.ledger.installation_domain import ensure_installation_domain
from aap_inference_contract.gpu import (
    GPUAuthorityUnavailable,
    GPUInstallationControlRequest,
    GPUInstallationQuiesceReply,
)


async def _health(endpoint, transport, request_id):
    challenge = secrets.token_hex(32)
    healthy, metadata = await transport.health_meta(
        endpoint,
        request_id=request_id,
        gpu_health_challenge=challenge,
        timeout_seconds=8.0,
    )
    if (
        not healthy
        or not isinstance(metadata, Mapping)
        or metadata.get(GPU_HEALTH_CHALLENGE_ECHO_MARKER) != challenge
    ):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.UNAVAILABLE,
            message="GPU installation health challenge failed",
        )
    residency = metadata.get("residency")
    if not isinstance(residency, Mapping):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.NOT_READY,
            message="GPU installation residency unavailable",
        )
    return dict(residency)


def _result(result, status):
    if not isinstance(result, dict) or result.get("status") != status:
        reason = result.get("reason") if isinstance(result, dict) else None
        code = (
            GPUArbiterErrorCode.CAPACITY_UNAVAILABLE
            if reason == "registry_busy"
            else GPUArbiterErrorCode.NOT_READY
        )
        raise GPUArbiterDispatchError(
            code, message=reason or "GPU installation terminal authority unavailable"
        )
    return result


async def quiesce_gpu_installation(
    request: GPUInstallationControlRequest,
    *,
    data,
    store_factory,
    signer_factory,
    endpoint_source,
    backend_transport,
    readiness_demoter,
    clock,
) -> GPUInstallationQuiesceReply:
    """Prove an irreversible signed terminal drain, then settle its ledger fence.

    Failures never release the installation fence. A repeated operation uses the
    Native durable intent and ledger owner to resume with the same generation.
    """
    request_id = uuid.uuid4()
    document = request.model_dump(mode="json", exclude={"protocol"})
    document["pool_ids"] = sorted(document["pool_ids"])
    if len(set(document["pool_ids"])) != len(document["pool_ids"]):
        raise ValueError("duplicate GPU installation pool")
    owner = f"installation:{request.operation_id}"
    await readiness_demoter(request.resource_id)
    endpoint = await endpoint_source.open_owned(
        request.registry_id,
        request_id=request_id,
        purpose="health_observation",
        binding=None,
    )
    if (
        endpoint.registry_id != request.registry_id
        or endpoint.url != request.endpoint
        or endpoint.gpu_resource_id != request.resource_id
    ):
        raise ValueError("GPU installation endpoint changed")
    initial = await _health(endpoint, backend_transport, request_id)
    store = store_factory()
    try:
        async with data.transaction() as transaction:
            candidates = await transaction.step(
                "membership_candidates",
                {
                    "gpu_resource_id": request.resource_id,
                    "states": ["pending", "active"],
                },
            )
        rows = candidates.get("candidates") if isinstance(candidates, dict) else None
        matches = (
            [
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("backend_id") == str(request.registry_id)
                and row.get("state") in {"pending", "active"}
            ]
            if isinstance(rows, list)
            else []
        )
        if len(matches) != 1 or type(matches[0].get("membership_epoch")) is not int:
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.NOT_READY,
                message="GPU installation active membership unavailable",
            )
        snapshot = await store.snapshot(request.resource_id)
        transition = snapshot.transition
        if isinstance(transition, dict) and transition.get("owner_id") == owner:
            source_generation = transition.get("installation_source_generation")
        else:
            allocation = next(
                (
                    item
                    for item in snapshot.allocations
                    if item.backend_id == str(request.registry_id)
                ),
                None,
            )
            source_generation = (
                initial.get("generation")
                if allocation is None
                else allocation.generation
            )
        expires = datetime.fromtimestamp(clock.time(), UTC) + timedelta(seconds=240)
        expiration = expires.isoformat()
        args = {
            "phase": "reserve",
            "request": document,
            "residency": initial,
            "preparation": None,
            "token_expires_at": expiration,
            "ledger_source_generation": source_generation,
            "probe_started_at": None,
        }
        async with data.transaction() as transaction:
            prepared = _result(
                await transaction.step("installation_terminal", args), "prepared"
            )["preparation"]
        if not isinstance(prepared, dict):
            raise ValueError("GPU installation preparation invalid")
        generation = prepared["generation"]
        expiration = prepared["token_expires_at"]
        expires = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        membership_epoch = prepared["membership_epoch"]
        await ensure_installation_domain(
            data, store, request.resource_id, str(request.registry_id), membership_epoch
        )
        if prepared["settled"] is True:
            args.update(
                phase="authorize",
                preparation=prepared,
                ledger_source_generation=prepared["source_generation"],
            )
            async with data.transaction() as transaction:
                prepared = _result(
                    await transaction.step("installation_terminal", args), "prepared"
                )["preparation"]
            expiration = prepared["token_expires_at"]
        if prepared["settled"] is True:
            verified = {
                "phase": "verify",
                "request": document,
                "residency": initial,
                "preparation": prepared,
                "token_expires_at": expiration,
                "ledger_source_generation": prepared["source_generation"],
                "probe_started_at": None,
            }
            async with data.transaction() as transaction:
                _result(
                    await transaction.step("installation_terminal", verified),
                    "verified",
                )
            released = await store.release_installation_drain(
                request.resource_id,
                backend_id=str(request.registry_id),
                membership_epoch=membership_epoch,
                generation=generation,
                owner_id=owner,
            )
            if released.status != "released":
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.NOT_READY,
                    message="GPU installation ledger fence release failed",
                )
            return GPUInstallationQuiesceReply(
                registry_id=request.registry_id,
                resource_id=request.resource_id,
                endpoint=request.endpoint,
                boot_id=request.boot_id,
                control_epoch=prepared["control_epoch"],
                pool_ids=document["pool_ids"],
                generation=generation,
                operation_id=request.operation_id,
                container_id=request.container_id,
                unloaded=True,
            )
        if prepared["settled"] is not False:
            raise ValueError("GPU installation settlement state invalid")
        deadline = clock.monotonic() + 120.0
        begin = {
            "backend_id": str(request.registry_id),
            "membership_epoch": membership_epoch,
            # Recovery may retain the Native intent after Redis loses its generation.
            # Fence the observed ledger state; keep the durable intent for verification.
            "source_generation": source_generation,
            "generation": generation,
            "owner_id": owner,
        }
        while True:
            acquired = await store.begin_installation_drain(
                request.resource_id, **begin
            )
            if acquired.status == "owned":
                break
            if acquired.status != "active_leases":
                code = (
                    GPUArbiterErrorCode.CAPACITY_UNAVAILABLE
                    if acquired.status == "busy"
                    else GPUArbiterErrorCode.NOT_READY
                )
                raise GPUArbiterDispatchError(
                    code, message="GPU installation transition unavailable"
                )
            if clock.monotonic() >= deadline:
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.DRAIN_TIMEOUT,
                    message="GPU installation leases remain active",
                    retry_after_s=1,
                )
            snapshot = await store.snapshot(request.resource_id)
            target_leases = [
                lease
                for lease in snapshot.leases
                if lease.backend_id == str(request.registry_id)
            ]
            if target_leases and all(
                lease.hard_deadline_ms < int(clock.time() * 1000)
                for lease in target_leases
            ):
                current = await endpoint_source.open_owned(
                    request.registry_id,
                    request_id=request_id,
                    purpose="health_observation",
                    binding=None,
                )
                if current.endpoint_sha256 != endpoint.endpoint_sha256:
                    raise GPUArbiterDispatchError(
                        GPUArbiterErrorCode.NOT_READY,
                        message="GPU installation endpoint changed",
                    )
                probe_started = datetime.fromtimestamp(clock.time(), UTC)
                idle = await _health(current, backend_transport, request_id)
                proof = {
                    "phase": "prove_idle",
                    "request": document,
                    "residency": idle,
                    "preparation": prepared,
                    "token_expires_at": expiration,
                    "ledger_source_generation": prepared["source_generation"],
                    "probe_started_at": probe_started.isoformat(),
                }
                async with data.transaction() as transaction:
                    result = await transaction.step("installation_terminal", proof)
                    if isinstance(result, dict) and result.get("status") == "verified":
                        await store.conclude_installation_leases(
                            request.resource_id,
                            backend_id=str(request.registry_id),
                            membership_epoch=membership_epoch,
                            generation=generation,
                            owner_id=owner,
                            ledger_incarnation=snapshot.ledger_incarnation,
                            ledger_revision=snapshot.ledger_revision,
                            probe_started_at_ms=int(probe_started.timestamp() * 1000),
                        )
                    elif (
                        not isinstance(result, dict)
                        or result.get("reason") != "terminal_idle_unproven"
                    ):
                        code = (
                            GPUArbiterErrorCode.CAPACITY_UNAVAILABLE
                            if isinstance(result, dict)
                            and result.get("reason") == "registry_busy"
                            else GPUArbiterErrorCode.NOT_READY
                        )
                        raise GPUArbiterDispatchError(
                            code, message="GPU installation idle proof unavailable"
                        )
            await clock.sleep(min(1.0, max(0.0, deadline - clock.monotonic())))
        current = await endpoint_source.open_owned(
            request.registry_id,
            request_id=request_id,
            purpose="health_observation",
            binding=None,
        )
        if current.endpoint_sha256 != endpoint.endpoint_sha256:
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.NOT_READY,
                message="GPU installation endpoint changed",
            )
        fresh = await _health(current, backend_transport, request_id)
        expires = datetime.fromtimestamp(clock.time(), UTC) + timedelta(seconds=240)
        expiration = expires.isoformat()
        args = {
            "phase": "authorize",
            "request": document,
            "residency": fresh,
            "preparation": prepared,
            "token_expires_at": expiration,
            "ledger_source_generation": prepared["source_generation"],
            "probe_started_at": None,
        }
        async with data.transaction() as transaction:
            prepared = _result(
                await transaction.step("installation_terminal", args), "prepared"
            )["preparation"]
        expiration = prepared["token_expires_at"]
        expires = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        claims = AdmissionTokenClaims(
            backend_registry_id=str(request.registry_id),
            gpu_resource_id=request.resource_id,
            boot_id=request.boot_id,
            generation=generation,
            control_epoch=prepared["control_epoch"],
            exp=int(expires.timestamp()),
            owner=owner,
            operation=str(request.operation_id),
            scope=AdmissionScope.DRAIN,
            jti=f"transition:{uuid.uuid4()}",
        )
        grant = GPUDispatchGrant(
            generation=generation, admission_token=signer_factory().sign(claims)
        )
        terminal_endpoint = await endpoint_source.open_installation_terminal(
            request.registry_id,
            request_id=request_id,
            request=document,
            preparation=prepared,
            endpoint_sha256=endpoint.endpoint_sha256,
        )
        if (
            terminal_endpoint.registry_id != request.registry_id
            or terminal_endpoint.request_id != request_id
            or terminal_endpoint.purpose != "terminal_drain"
            or terminal_endpoint.authority is not None
            or terminal_endpoint.endpoint_sha256 != endpoint.endpoint_sha256
            or terminal_endpoint.url != request.endpoint
            or terminal_endpoint.gpu_resource_id != request.resource_id
        ):
            raise ValueError("GPU installation endpoint changed")
        receipt = await backend_transport.lifecycle_terminal_drain(
            terminal_endpoint,
            grant,
            registry_id=request.registry_id,
            request_id=request_id,
            timeout_seconds=120.0,
        )
        expected = {
            "registry_id": str(request.registry_id),
            "gpu_resource_id": request.resource_id,
            "boot_id": request.boot_id,
            "operation_id": str(request.operation_id),
            "control_epoch": prepared["control_epoch"],
            "generation": generation,
            "pool_ids": document["pool_ids"],
            "unloaded": True,
        }
        if not isinstance(receipt, dict) or receipt != expected:
            raise ValueError("GPU installation terminal receipt mismatch")
        current = await endpoint_source.open_owned(
            request.registry_id,
            request_id=request_id,
            purpose="health_observation",
            binding=None,
        )
        if current.endpoint_sha256 != endpoint.endpoint_sha256:
            raise ValueError("GPU installation endpoint changed")
        terminal = await _health(current, backend_transport, request_id)
        args = {
            "phase": "verify",
            "request": document,
            "residency": terminal,
            "preparation": prepared,
            "token_expires_at": expiration,
            "ledger_source_generation": prepared["source_generation"],
            "probe_started_at": None,
        }
        ledger = {
            "backend_id": str(request.registry_id),
            "membership_epoch": membership_epoch,
            "generation": generation,
            "owner_id": owner,
        }
        async with data.transaction() as transaction:
            _result(await transaction.step("installation_terminal", args), "verified")
            settled = await store.finish_installation_drain(
                request.resource_id, **ledger
            )
            if settled.status != "settled":
                raise GPUArbiterDispatchError(
                    GPUArbiterErrorCode.NOT_READY,
                    message="GPU installation ledger is not settled",
                )
            args["phase"] = "commit"
            _result(await transaction.step("installation_terminal", args), "verified")
        released = await store.release_installation_drain(request.resource_id, **ledger)
        if released.status != "released":
            raise GPUArbiterDispatchError(
                GPUArbiterErrorCode.NOT_READY,
                message="GPU installation ledger fence release failed",
            )
        return GPUInstallationQuiesceReply(
            registry_id=request.registry_id,
            resource_id=request.resource_id,
            endpoint=request.endpoint,
            boot_id=request.boot_id,
            control_epoch=prepared["control_epoch"],
            pool_ids=document["pool_ids"],
            generation=generation,
            operation_id=request.operation_id,
            container_id=request.container_id,
            unloaded=True,
        )
    except GPUAuthorityUnavailable as error:
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.UNAVAILABLE,
            message="GPU installation authority is unavailable",
            retry_after_s=1,
        ) from error
    finally:
        await store.aclose()

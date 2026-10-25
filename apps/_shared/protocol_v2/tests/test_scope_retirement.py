from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from uuid import uuid4

import jwt
import pytest

from aap_protocol_v2.routing import (
    ROUTE_ADMISSION_AUDIENCE,
    ROUTE_ADMISSION_PROTOCOL,
    ROUTE_CONTROL_PROTOCOL,
    ROUTE_STATUS_PATH,
    RoutingProtocolError,
    decode_backend_route_control_command,
)
from aap_protocol_v2.routing_runtime import (
    BackendRoutingController,
    BackendRoutingMiddleware,
    RouteControlConflict,
)
from aap_protocol_v2.scope_retirement import (
    SCOPE_RETIREMENT_PATH,
    SCOPE_RETIREMENT_PROTOCOL,
    SCOPE_RETIREMENT_REPLY_PROTOCOL,
    BackendScopeRetirementCommand,
    decode_backend_scope_retirement_command,
    decode_backend_scope_retirement_reply,
)


def _command_payload(**overrides):
    value = {
        "schema": SCOPE_RETIREMENT_PROTOCOL,
        "retirement_id": str(uuid4()),
        "registry_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "authority_epoch": str(uuid4()),
        "backend_incarnation": str(uuid4()),
        "challenge": "c" * 64,
        "fence_until": int(time.time()) + 300,
    }
    value.update(overrides)
    return value


def _route_control_body(
    *,
    action: str,
    command_id: str,
    registry_id: str,
    authority_epoch: str,
    retired_epoch: str | None,
    challenge: str,
    signing_key: bytes | None = None,
) -> bytes:
    import base64

    return json.dumps(
        {
            "schema": ROUTE_CONTROL_PROTOCOL,
            "command_id": command_id,
            "registry_id": registry_id,
            "authority_epoch": authority_epoch,
            "retired_epoch": retired_epoch,
            "challenge": challenge,
            "action": action,
            "signing_key": (
                None
                if signing_key is None
                else base64.urlsafe_b64encode(signing_key).decode("ascii").rstrip("=")
            ),
        },
        separators=(",", ":"),
    ).encode()


async def _bind(controller: BackendRoutingController):
    command_id = str(uuid4())
    epoch = str(uuid4())
    challenge = "b" * 64
    key = b"r" * 32
    drain = await controller.apply_control(
        decode_backend_route_control_command(
            _route_control_body(
                action="drain",
                command_id=command_id,
                registry_id=controller.registry_id or str(uuid4()),
                authority_epoch=epoch,
                retired_epoch=None,
                challenge=challenge,
            )
        )
    )
    bound = await controller.apply_control(
        decode_backend_route_control_command(
            _route_control_body(
                action="bind",
                command_id=command_id,
                registry_id=drain.registry_id,
                authority_epoch=epoch,
                retired_epoch=None,
                challenge=challenge,
                signing_key=key,
            )
        )
    )
    return bound, key


def _admission_token(
    controller: BackendRoutingController,
    key: bytes,
    scope_id: str,
    *,
    path: str = "/predict",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "schema": ROUTE_ADMISSION_PROTOCOL,
            "aud": ROUTE_ADMISSION_AUDIENCE,
            "registry_id": controller.registry_id,
            "backend_incarnation": controller.backend_incarnation,
            "authority_epoch": controller._authority_epoch,
            "scope_id": scope_id,
            "attempt_id": str(uuid4()),
            "operation": "predict",
            "method": "POST",
            "path": path,
            "iat": now,
            "exp": now + 300,
        },
        key,
        algorithm="HS256",
        headers={"typ": "JWT"},
    )


def _retirement_command(
    controller: BackendRoutingController,
    scope_id: str,
    *,
    retirement_id: str | None = None,
    challenge: str = "d" * 64,
    fence_until: int | None = None,
) -> BackendScopeRetirementCommand:
    return BackendScopeRetirementCommand(
        retirement_id=retirement_id or str(uuid4()),
        registry_id=controller.registry_id,
        scope_id=scope_id,
        authority_epoch=controller._authority_epoch,
        backend_incarnation=controller.backend_incarnation,
        challenge=challenge,
        fence_until=(int(time.time()) + 300 if fence_until is None else fence_until),
    )


async def _asgi_call(app, *, path: str, method: str = "POST", headers=(), body=b""):
    sent = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": list(headers),
            "server": ("test", 80),
            "client": ("test", 1000),
            "scheme": "http",
        },
        receive,
        send,
    )
    start = next(item for item in sent if item["type"] == "http.response.start")
    payload = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return start["status"], payload


async def _retire_http(controller, command, *, authorization=b"Bearer secret"):
    body = json.dumps(command.as_dict(), separators=(",", ":")).encode()
    middleware = BackendRoutingMiddleware(_ok, controller)
    return await _asgi_call(
        middleware,
        path=SCOPE_RETIREMENT_PATH,
        headers=[
            (b"authorization", authorization),
            (b"content-length", str(len(body)).encode()),
        ],
        body=body,
    )


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


async def _wait_for_active_count(controller, expected: int) -> None:
    async def wait():
        while controller.active_requests != expected:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=2)


def test_scope_retirement_wire_requires_exact_canonical_command():
    payload = _command_payload()
    command = decode_backend_scope_retirement_command(
        json.dumps(payload, separators=(",", ":")).encode()
    )
    assert command.as_dict() == payload

    malformed = [
        {**payload, "extra": "field"},
        {key: value for key, value in payload.items() if key != "challenge"},
        {**payload, "scope_id": payload["scope_id"].upper()},
        {**payload, "challenge": "C" * 64},
        {**payload, "fence_until": True},
        {**payload, "fence_until": 1 << 63},
        {**payload, "schema": "aap-backend-scope-retirement/v2"},
    ]
    for value in malformed:
        with pytest.raises(RoutingProtocolError):
            decode_backend_scope_retirement_command(json.dumps(value).encode())

    duplicate = json.dumps(payload, separators=(",", ":")).replace(
        '"schema":', '"schema":"wrong","schema":', 1
    )
    with pytest.raises(RoutingProtocolError, match="duplicate"):
        decode_backend_scope_retirement_command(duplicate.encode())


def test_scope_retirement_reply_decoder_checks_count_phase_and_exact_fields():
    reply = {
        "schema": SCOPE_RETIREMENT_REPLY_PROTOCOL,
        "retirement_id": str(uuid4()),
        "registry_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "authority_epoch": str(uuid4()),
        "backend_incarnation": str(uuid4()),
        "challenge": "f" * 64,
        "phase": "draining",
        "active_requests": 2,
    }
    decoded = decode_backend_scope_retirement_reply(
        json.dumps(reply, separators=(",", ":")).encode()
    )
    assert decoded.as_dict() == reply
    assert decode_backend_scope_retirement_reply(reply) == decoded
    for invalid in (
        {**reply, "phase": "retired", "active_requests": 1},
        {**reply, "phase": "draining", "active_requests": 0},
        {**reply, "active_requests": True},
        {**reply, "active_requests": -1},
        {**reply, "active_requests": 1 << 31},
        {**reply, "extra": "field"},
    ):
        with pytest.raises(RoutingProtocolError):
            decode_backend_scope_retirement_reply(invalid)


@pytest.mark.asyncio
async def test_retirement_fences_one_scope_and_keeps_unrelated_scope_admissible():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    target_scope = str(uuid4())
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_app(scope, receive, send):
        if scope["path"] == "/slow":
            started.set()
            await finish.wait()
        await _ok(scope, receive, send)

    middleware = BackendRoutingMiddleware(slow_app, controller)
    token = _admission_token(controller, key, target_scope, path="/slow")
    stale_same_scope_token = _admission_token(controller, key, target_scope)
    request = asyncio.create_task(
        _asgi_call(
            middleware,
            path="/slow",
            headers=[(b"x-aap-route-admission", token.encode())],
        )
    )
    await started.wait()
    command = _retirement_command(controller, target_scope)
    status, payload = await _retire_http(controller, command)
    reply = decode_backend_scope_retirement_reply(payload)
    assert status == 200
    assert (reply.phase, reply.active_requests) == ("draining", 1)

    assert (
        await _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", stale_same_scope_token.encode())],
        )
    )[0] == 401
    unrelated_scope = str(uuid4())
    unrelated_token = _admission_token(controller, key, unrelated_scope)
    assert (
        await _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", unrelated_token.encode())],
        )
    )[0] == 200
    assert controller.phase == "bound"

    finish.set()
    assert (await request)[0] == 200
    status, payload = await _retire_http(controller, command)
    reply = decode_backend_scope_retirement_reply(payload)
    assert status == 200
    assert (reply.phase, reply.active_requests) == ("retired", 0)
    assert controller.active_requests == 0


@pytest.mark.asyncio
async def test_retirement_counts_registered_future_until_it_finishes():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    scope_id = str(uuid4())
    started, finish = asyncio.Event(), asyncio.Event()

    async def background():
        started.set()
        await finish.wait()

    async def app(scope, receive, send):
        assert controller.track_background_future(asyncio.create_task(background()))
        await _ok(scope, receive, send)

    middleware = BackendRoutingMiddleware(app, controller)
    token = _admission_token(controller, key, scope_id)
    request = asyncio.create_task(
        _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", token.encode())],
        )
    )
    await started.wait()
    command = _retirement_command(controller, scope_id)
    status, payload = await _retire_http(controller, command)
    reply = decode_backend_scope_retirement_reply(payload)
    assert status == 200
    assert (reply.phase, reply.active_requests) == ("draining", 1)

    finish.set()
    assert (await request)[0] == 200
    status, payload = await _retire_http(controller, command)
    reply = decode_backend_scope_retirement_reply(payload)
    assert status == 200
    assert (reply.phase, reply.active_requests) == ("retired", 0)


@pytest.mark.asyncio
async def test_late_future_registration_uses_the_current_scope_counter():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    scope_id = str(uuid4())
    register_late, registered_late = asyncio.Event(), asyncio.Event()
    finish_a, finish_b, started_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
    future_a = []
    late_result = []

    async def work_a():
        await finish_a.wait()

    async def app_a(scope, receive, send):
        future = asyncio.create_task(work_a())
        future_a.append(future)

        async def late_register():
            await register_late.wait()
            late_result.append(controller.track_background_future(future))
            registered_late.set()

        asyncio.create_task(late_register())
        await _ok(scope, receive, send)

    middleware_a = BackendRoutingMiddleware(app_a, controller)
    token_a = _admission_token(controller, key, scope_id)
    assert (
        await _asgi_call(
            middleware_a,
            path="/predict",
            headers=[(b"x-aap-route-admission", token_a.encode())],
        )
    )[0] == 200
    assert controller.active_requests == 0

    async def slow_app(scope, receive, send):
        started_b.set()
        await finish_b.wait()
        await _ok(scope, receive, send)

    middleware_b = BackendRoutingMiddleware(slow_app, controller)
    token_b = _admission_token(controller, key, scope_id, path="/slow")
    request_b = asyncio.create_task(
        _asgi_call(
            middleware_b,
            path="/slow",
            headers=[(b"x-aap-route-admission", token_b.encode())],
        )
    )
    await started_b.wait()
    await _wait_for_active_count(controller, 1)

    register_late.set()
    await registered_late.wait()
    assert late_result == [True]
    await _wait_for_active_count(controller, 2)
    command = _retirement_command(controller, scope_id)
    reply = await controller.retire_scope(command)
    assert (reply.phase, reply.active_requests) == ("draining", 2)

    finish_a.set()
    await future_a[0]
    await _wait_for_active_count(controller, 1)
    reply = await controller.retire_scope(command)
    assert (reply.phase, reply.active_requests) == ("draining", 1)

    finish_b.set()
    assert (await request_b)[0] == 200
    await _wait_for_active_count(controller, 0)
    reply = await controller.retire_scope(command)
    assert (reply.phase, reply.active_requests) == ("retired", 0)


@pytest.mark.asyncio
async def test_late_future_after_retirement_latches_controller_closed():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    scope_id = str(uuid4())
    register_late, registered_late = asyncio.Event(), asyncio.Event()
    finish = asyncio.Event()
    future_ref = []
    late_result = []

    async def work():
        await finish.wait()

    async def app(scope, receive, send):
        future = asyncio.create_task(work())
        future_ref.append(future)

        async def late_register():
            await register_late.wait()
            late_result.append(controller.track_background_future(future))
            registered_late.set()

        asyncio.create_task(late_register())
        await _ok(scope, receive, send)

    middleware = BackendRoutingMiddleware(app, controller)
    token = _admission_token(controller, key, scope_id)
    assert (
        await _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", token.encode())],
        )
    )[0] == 200
    command = _retirement_command(controller, scope_id)
    reply = await controller.retire_scope(command)
    assert (reply.phase, reply.active_requests) == ("retired", 0)

    register_late.set()
    await registered_late.wait()
    assert late_result == [False]
    assert scope_id in controller._scope_tracking_errors
    await _wait_for_active_count(controller, 1)
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(command)

    other_scope_token = _admission_token(controller, key, str(uuid4()))
    assert (
        await _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", other_scope_token.encode())],
        )
    )[0] == 503

    finish.set()
    await future_ref[0]
    await _wait_for_active_count(controller, 0)
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(command)
    assert controller._accepted_control is not None
    with pytest.raises(RouteControlConflict):
        controller._control_reply(controller._accepted_control)

    with pytest.raises(RouteControlConflict):
        await controller.control_status()
    status, _ = await _asgi_call(
        middleware,
        method="GET",
        path=ROUTE_STATUS_PATH,
        headers=[(b"authorization", b"Bearer secret")],
    )
    assert status == 409

    another_epoch, another_id = str(uuid4()), str(uuid4())
    drain_command = decode_backend_route_control_command(
        _route_control_body(
            action="drain",
            command_id=another_id,
            registry_id=controller.registry_id,
            authority_epoch=another_epoch,
            retired_epoch=controller._authority_epoch,
            challenge="b" * 64,
        )
    )
    with pytest.raises(RouteControlConflict):
        await controller.apply_control(drain_command)
    with pytest.raises(RouteControlConflict):
        await controller.apply_control(
            decode_backend_route_control_command(
                _route_control_body(
                    action="bind",
                    command_id=another_id,
                    registry_id=controller.registry_id,
                    authority_epoch=another_epoch,
                    retired_epoch=controller._authority_epoch,
                    challenge="b" * 64,
                    signing_key=b"t" * 32,
                )
            )
        )


@pytest.mark.asyncio
async def test_late_future_after_epoch_bind_latches_controller_closed():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    bound, key = await _bind(controller)
    scope_id = str(uuid4())
    register_late, registered_late = asyncio.Event(), asyncio.Event()
    finish = asyncio.Event()
    future_ref = []
    late_result = []

    async def work():
        await finish.wait()

    async def app(scope, receive, send):
        future = asyncio.create_task(work())
        future_ref.append(future)

        async def late_register():
            await register_late.wait()
            late_result.append(controller.track_background_future(future))
            registered_late.set()

        asyncio.create_task(late_register())
        await _ok(scope, receive, send)

    middleware = BackendRoutingMiddleware(app, controller)
    old_token = _admission_token(controller, key, scope_id)
    assert (
        await _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", old_token.encode())],
        )
    )[0] == 200

    next_epoch, next_id, challenge = str(uuid4()), str(uuid4()), "a" * 64
    drain = await controller.apply_control(
        decode_backend_route_control_command(
            _route_control_body(
                action="drain",
                command_id=next_id,
                registry_id=controller.registry_id,
                authority_epoch=next_epoch,
                retired_epoch=bound.authority_epoch,
                challenge=challenge,
            )
        )
    )
    assert (drain.phase, drain.active_requests) == ("drained", 0)
    next_key = b"s" * 32
    await controller.apply_control(
        decode_backend_route_control_command(
            _route_control_body(
                action="bind",
                command_id=next_id,
                registry_id=controller.registry_id,
                authority_epoch=next_epoch,
                retired_epoch=bound.authority_epoch,
                challenge=challenge,
                signing_key=next_key,
            )
        )
    )

    register_late.set()
    await registered_late.wait()
    assert late_result == [False]
    assert scope_id in controller._scope_tracking_errors
    await _wait_for_active_count(controller, 1)
    new_epoch_token = _admission_token(controller, next_key, str(uuid4()))
    assert (
        await _asgi_call(
            middleware,
            path="/predict",
            headers=[(b"x-aap-route-admission", new_epoch_token.encode())],
        )
    )[0] == 503

    finish.set()
    await future_ref[0]
    await _wait_for_active_count(controller, 0)
    another_epoch, another_id = str(uuid4()), str(uuid4())
    drain_command = decode_backend_route_control_command(
        _route_control_body(
            action="drain",
            command_id=another_id,
            registry_id=controller.registry_id,
            authority_epoch=another_epoch,
            retired_epoch=next_epoch,
            challenge="b" * 64,
        )
    )
    with pytest.raises(RouteControlConflict):
        await controller.apply_control(drain_command)
    with pytest.raises(RouteControlConflict):
        await controller.control_status()
    with pytest.raises(RouteControlConflict):
        await controller.apply_control(
            decode_backend_route_control_command(
                _route_control_body(
                    action="bind",
                    command_id=another_id,
                    registry_id=controller.registry_id,
                    authority_epoch=another_epoch,
                    retired_epoch=next_epoch,
                    challenge="b" * 64,
                    signing_key=b"t" * 32,
                )
            )
        )


@pytest.mark.asyncio
async def test_retirement_replays_are_immutable_and_require_current_binding():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    bound, _ = await _bind(controller)
    scope_id = str(uuid4())
    command = _retirement_command(controller, scope_id)
    assert (await controller.retire_scope(command)).phase == "retired"
    assert await controller.retire_scope(command) == await controller.retire_scope(
        command
    )

    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(
            _retirement_command(
                controller,
                scope_id,
                retirement_id=command.retirement_id,
                challenge="e" * 64,
            )
        )
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(_retirement_command(controller, scope_id))
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(
            replace(command, fence_until=command.fence_until + 1)
        )
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(replace(command, scope_id=str(uuid4())))
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(replace(command, authority_epoch=str(uuid4())))
    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(
            replace(command, backend_incarnation=str(uuid4()))
        )

    old_epoch = bound.authority_epoch
    next_epoch = str(uuid4())
    next_id = str(uuid4())
    challenge = "a" * 64
    await controller.apply_control(
        decode_backend_route_control_command(
            _route_control_body(
                action="drain",
                command_id=next_id,
                registry_id=controller.registry_id,
                authority_epoch=next_epoch,
                retired_epoch=old_epoch,
                challenge=challenge,
            )
        )
    )
    await controller.apply_control(
        decode_backend_route_control_command(
            _route_control_body(
                action="bind",
                command_id=next_id,
                registry_id=controller.registry_id,
                authority_epoch=next_epoch,
                retired_epoch=old_epoch,
                challenge=challenge,
                signing_key=b"s" * 32,
            )
        )
    )
    assert not controller._scope_retirements
    assert not controller._scope_retirements_by_id
    assert not controller._scope_retirement_ids_by_scope


@pytest.mark.asyncio
async def test_retirement_http_requires_management_auth_and_strict_body():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    await _bind(controller)
    command = _retirement_command(controller, str(uuid4()))
    body = json.dumps(command.as_dict(), separators=(",", ":")).encode()
    middleware = BackendRoutingMiddleware(_ok, controller)
    headers = [(b"content-length", str(len(body)).encode())]

    assert (
        await _asgi_call(
            middleware,
            path=SCOPE_RETIREMENT_PATH,
            headers=[*headers, (b"authorization", b"Bearer wrong")],
            body=body,
        )
    )[0] == 401
    assert (
        await _asgi_call(
            middleware,
            path=SCOPE_RETIREMENT_PATH,
            headers=[*headers, (b"authorization", b"Bearer secret")],
            body=body + b" ",
        )
    )[0] == 400
    assert (
        await _asgi_call(
            middleware,
            path=SCOPE_RETIREMENT_PATH,
            method="GET",
            headers=[(b"authorization", b"Bearer secret")],
        )
    )[0] == 405


def test_scope_tombstone_outlives_attempt_ttl_and_expires_after_fence_skew():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )

    async def setup():
        await _bind(controller)
        command = _retirement_command(
            controller, str(uuid4()), fence_until=int(time.time()) + 100
        )
        await controller.retire_scope(command)
        return command

    command = asyncio.run(setup())
    controller._prune_attempts(command.fence_until + 1000)
    assert command.scope_id in controller._scope_retirements
    controller._prune_scope_retirements(command.fence_until + 30)
    assert command.scope_id in controller._scope_retirements
    controller._prune_scope_retirements(command.fence_until + 31)
    assert command.scope_id not in controller._scope_retirements
    assert command.retirement_id in controller._scope_retirements_by_id

    async def conflict_replay():
        with pytest.raises(RouteControlConflict):
            await controller.retire_scope(replace(command, challenge="e" * 64))
        with pytest.raises(RouteControlConflict):
            await controller.retire_scope(replace(command, retirement_id=str(uuid4())))

    asyncio.run(conflict_replay())


@pytest.mark.asyncio
async def test_unverifiable_scope_work_fails_closed_and_cannot_bind_new_epoch():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    bound, _ = await _bind(controller)
    scope_id = str(uuid4())
    command = _retirement_command(controller, scope_id)
    controller._scope_tracking_errors.add(scope_id)

    with pytest.raises(RouteControlConflict):
        await controller.retire_scope(command)
    assert scope_id in controller._scope_retirements

    next_epoch, next_id, challenge = str(uuid4()), str(uuid4()), "a" * 64
    with pytest.raises(RouteControlConflict):
        await controller.apply_control(
            decode_backend_route_control_command(
                _route_control_body(
                    action="drain",
                    command_id=next_id,
                    registry_id=controller.registry_id,
                    authority_epoch=next_epoch,
                    retired_epoch=bound.authority_epoch,
                    challenge=challenge,
                )
            )
        )
    with pytest.raises(RouteControlConflict):
        await controller.control_status()
    with pytest.raises(RouteControlConflict):
        await controller.apply_control(
            decode_backend_route_control_command(
                _route_control_body(
                    action="bind",
                    command_id=next_id,
                    registry_id=controller.registry_id,
                    authority_epoch=next_epoch,
                    retired_epoch=bound.authority_epoch,
                    challenge=challenge,
                    signing_key=b"s" * 32,
                )
            )
        )

from __future__ import annotations

import base64
import json
import time
from uuid import uuid4

import asyncio
import jwt
import pytest

from aap_protocol_v2.routing import (
    ROUTE_ADMISSION_AUDIENCE,
    ROUTE_ADMISSION_PROTOCOL,
    ROUTE_CONTROL_PATH,
    ROUTE_CONTROL_PROTOCOL,
    ROUTE_STATUS_PATH,
    decode_backend_route_control_command,
)
from aap_protocol_v2.routing_runtime import (
    BackendRoutingController,
    BackendRoutingMiddleware,
    RouteConfigurationError,
    install_backend_routing_controller,
)


def _claims(
    controller: BackendRoutingController,
    key: bytes,
    *,
    path: str = "/predict",
    method: str = "POST",
    operation: str = "predict",
    attempt_id: str | None = None,
    scope_id: str | None = None,
):
    now = int(time.time())
    return jwt.encode(
        {
            "schema": ROUTE_ADMISSION_PROTOCOL,
            "aud": ROUTE_ADMISSION_AUDIENCE,
            "registry_id": controller.registry_id,
            "backend_incarnation": controller.backend_incarnation,
            "authority_epoch": controller._authority_epoch,
            "scope_id": scope_id or str(uuid4()),
            "attempt_id": attempt_id or str(uuid4()),
            "operation": operation,
            "method": method,
            "path": path,
            "iat": now,
            "exp": now + 60,
        },
        controller._signing_key,
        algorithm="HS256",
        headers={"typ": "JWT"},
    )


def _command(
    *,
    action: str,
    command_id: str,
    registry_id: str,
    authority_epoch: str,
    retired_epoch: str | None,
    challenge: str,
    signing_key: bytes | None = None,
) -> bytes:
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
    ).encode("utf-8")


def _asgi_call(app, *, method="POST", path="/predict", headers=(), body=b""):
    async def run():
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
        payload = next(
            (
                item.get("body", b"")
                for item in sent
                if item["type"] == "http.response.body"
            ),
            b"",
        )
        return start["status"], payload, sent

    return run()


async def _app_response(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


async def _bind(controller: BackendRoutingController, *, previous_epoch=None):
    command_id = str(uuid4())
    epoch = str(uuid4())
    challenge = "c" * 64
    key = b"r" * 32
    drain = await controller.apply_control(
        decode_backend_route_control_command(
            _command(
                action="drain",
                command_id=command_id,
                registry_id=controller.registry_id or str(uuid4()),
                authority_epoch=epoch,
                retired_epoch=previous_epoch,
                challenge=challenge,
            )
        )
    )
    assert drain.active_requests == 0
    bound = await controller.apply_control(
        decode_backend_route_control_command(
            _command(
                action="bind",
                command_id=command_id,
                registry_id=drain.registry_id,
                authority_epoch=epoch,
                retired_epoch=previous_epoch,
                challenge=challenge,
                signing_key=key,
            )
        )
    )
    return bound, key


def _middleware(controller, app=_app_response):
    return BackendRoutingMiddleware(app, controller)


@pytest.mark.asyncio
async def test_legacy_mode_tracks_mutations_and_management_requires_control_secret():
    controller = BackendRoutingController()
    middleware = _middleware(controller)

    status, _, _ = await _asgi_call(middleware, method="GET", path="/health")
    assert status == 200
    status, _, _ = await _asgi_call(middleware)
    assert status == 200
    assert controller.active_requests == 0
    status, _, _ = await _asgi_call(
        middleware,
        method="GET",
        path=ROUTE_STATUS_PATH,
        headers=[(b"authorization", b"Bearer secret")],
    )
    assert status == 503


@pytest.mark.asyncio
async def test_unbound_legacy_migration_closes_when_drain_is_accepted():
    registry_id = str(uuid4())
    controller = BackendRoutingController(
        registry_id=registry_id,
        control_token="management-secret",
        allow_legacy_execution=True,
    )
    middleware = _middleware(controller)
    status, _, _ = await _asgi_call(middleware)
    assert status == 200

    command_id, epoch = str(uuid4()), str(uuid4())
    raw = _command(
        action="drain",
        command_id=command_id,
        registry_id=registry_id,
        authority_epoch=epoch,
        retired_epoch=None,
        challenge="d" * 64,
    )
    status, body, _ = await _asgi_call(
        middleware,
        path=ROUTE_CONTROL_PATH,
        headers=[(b"authorization", b"Bearer management-secret")],
        body=raw,
    )
    assert status == 200
    assert json.loads(body)["phase"] == "drained"
    status, _, _ = await _asgi_call(middleware)
    assert status == 503


@pytest.mark.asyncio
async def test_bound_admission_rejects_attempt_replay():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    middleware = _middleware(controller)
    token = _claims(controller, key)
    headers = [(b"x-aap-route-admission", token.encode("ascii"))]

    assert (await _asgi_call(middleware, headers=headers))[0] == 200
    assert (await _asgi_call(middleware, headers=headers))[0] == 401
    assert controller.active_requests == 0


@pytest.mark.asyncio
async def test_management_admission_holds_exclusive_fence_until_handler_finishes():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    started, finish = asyncio.Event(), asyncio.Event()

    async def unload_app(scope, receive, send):
        started.set()
        await finish.wait()
        await _app_response(scope, receive, send)

    middleware = _middleware(controller, unload_app)
    control_id = str(uuid4())
    management = _claims(
        controller,
        key,
        path="/unload",
        operation="control:unload",
        scope_id=control_id,
    )
    call = asyncio.create_task(
        _asgi_call(
            middleware,
            path="/unload",
            headers=[(b"x-aap-route-admission", management.encode("ascii"))],
        )
    )
    await started.wait()
    assert controller.active_requests == 1

    prediction = _claims(controller, key)
    assert (
        await _asgi_call(
            middleware,
            headers=[(b"x-aap-route-admission", prediction.encode("ascii"))],
        )
    )[0] == 401

    finish.set()
    assert (await call)[0] == 200
    assert controller.active_requests == 0


@pytest.mark.asyncio
async def test_drain_waits_for_existing_asgi_work_and_rejects_new_admission():
    controller = BackendRoutingController(
        registry_id=str(uuid4()), control_token="secret"
    )
    _, key = await _bind(controller)
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_app(scope, receive, send):
        started.set()
        await finish.wait()
        await _app_response(scope, receive, send)

    middleware = _middleware(controller, slow_app)
    token = _claims(controller, key)
    request = asyncio.create_task(
        _asgi_call(
            middleware,
            headers=[(b"x-aap-route-admission", token.encode("ascii"))],
        )
    )
    await started.wait()
    old_epoch = controller._authority_epoch
    command_id, next_epoch = str(uuid4()), str(uuid4())
    reply = await controller.apply_control(
        decode_backend_route_control_command(
            _command(
                action="drain",
                command_id=command_id,
                registry_id=controller.registry_id,
                authority_epoch=next_epoch,
                retired_epoch=old_epoch,
                challenge="e" * 64,
            )
        )
    )
    assert (reply.phase, reply.active_requests) == ("draining", 1)
    next_token = _claims(controller, key)
    assert (
        await _asgi_call(
            middleware,
            headers=[(b"x-aap-route-admission", next_token.encode("ascii"))],
        )
    )[0] == 503

    finish.set()
    assert (await request)[0] == 200
    assert (await controller.control_status()).phase == "drained"


@pytest.mark.asyncio
async def test_cancellation_keeps_work_counted_until_asgi_child_finishes():
    controller = BackendRoutingController()
    started, stream_finish, background_finish = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def continuing_app(scope, receive, send):
        started.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )
        await stream_finish.wait()
        await send({"type": "http.response.body", "body": b"done", "more_body": False})
        await background_finish.wait()

    middleware = _middleware(controller, continuing_app)
    call = asyncio.create_task(_asgi_call(middleware))
    await started.wait()
    assert controller.active_requests == 1

    call.cancel()
    await asyncio.sleep(0)
    assert controller.active_requests == 1
    stream_finish.set()
    # The endpoint remains active after its final stream chunk for awaited background work.
    await asyncio.sleep(0)
    assert controller.active_requests == 1
    background_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert controller.active_requests == 0


@pytest.mark.asyncio
async def test_explicit_background_future_keeps_request_lease_after_response():
    controller = BackendRoutingController()
    started, finish = asyncio.Event(), asyncio.Event()

    async def background_work():
        started.set()
        await finish.wait()

    async def app(scope, receive, send):
        future = asyncio.create_task(background_work())
        assert controller.track_background_future(future)
        await _app_response(scope, receive, send)

    middleware = _middleware(controller, app)
    call = asyncio.create_task(_asgi_call(middleware))
    await started.wait()
    await asyncio.sleep(0)
    assert controller.active_requests == 1
    assert not call.done()

    finish.set()
    assert (await call)[0] == 200
    assert controller.active_requests == 0


def test_replay_cache_prunes_only_expired_completed_attempts():
    controller = BackendRoutingController(control_token="secret")
    now = int(time.time())
    controller._used_attempts.update(
        {
            "active": now - 1,
            "completed": now - 1,
            "fresh": now + 30,
        }
    )
    controller._active_attempts.add("active")

    controller._prune_attempts(now)

    assert controller._used_attempts == {"active": now - 1, "fresh": now + 30}


@pytest.mark.asyncio
async def test_app_installer_is_idempotent_and_fails_closed_on_bad_config():
    from fastapi import FastAPI

    app = FastAPI()
    controller = install_backend_routing_controller(
        app,
        registry_id=str(uuid4()),
        environ={
            "AAP_ROUTE_CONTROL_TOKEN": "secret",
            "AAP_ROUTE_ALLOW_LEGACY_EXECUTION": "1",
        },
    )
    assert install_backend_routing_controller(app) is controller
    assert app.state.backend_routing_controller is controller

    with pytest.raises(RouteConfigurationError):
        install_backend_routing_controller(
            FastAPI(),
            environ={
                "AAP_ROUTE_CONTROL_TOKEN": "one",
                "AAP_ROUTE_CONTROL_TOKEN_FILE": "two",
            },
        )

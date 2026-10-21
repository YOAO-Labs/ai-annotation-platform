from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from aap_inference_contract.routing import (
    ROUTING_DATA_PROTOCOL,
    RoutingRejectedResult,
    RoutingUnavailable,
    make_routing_command,
    make_routing_runtime_request,
    make_routing_status_request,
    routing_command_sha256,
)

from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
)
from aap_inference.routing import RoutingManager

_POOL_ID = UUID("e57fd804-5e3c-448d-9aee-a4cb50c4e640")
_PROJECT_ID = UUID("f3deddb7-507b-4914-bbc4-8ac9e1e53b54")
_LEGACY_ID = UUID("d70c519e-97ec-4d08-b25f-14868f1588c2")
_STALE_ID = UUID("d70c519e-97ec-4d08-b25f-14868f1588c3")
_DRIFT_ID = UUID("d70c519e-97ec-4d08-b25f-14868f1588c4")
_SCOPE_ID = UUID("88d37335-71cb-4224-9c9e-832e9ee5dbb0")
_CONTROL_ID = UUID("61d8d46c-b3ee-48bd-adbe-1b33e96b013b")
_ATTEMPT_ID = UUID("690a011a-a54e-43b0-8294-a4aca0b61b8e")
_AUTHORITY_EPOCH = UUID("5f7661e7-49e9-4786-859c-1fbac9d5aad0")
_BACKEND_INCARNATION = UUID("e28e0e23-6a80-43a9-96a3-c64aad9c2f63")
_NEW_EPOCH = UUID("4c4d0ff3-6cb3-49e8-ac64-e1ac1dbfba61")
_NEW_INCARNATION = UUID("bdd5dff5-c92f-40b4-8258-a3f69e238d07")
_NOW = datetime(2026, 10, 22, 2, 30, tzinfo=UTC)
_CHALLENGE = "a" * 64
_CAPABILITIES = canonicalize_capability(
    {"models": [{"id": "detector", "task": "detection"}]}
)
_DRIFTED_CAPABILITIES = canonicalize_capability(
    {"models": [{"id": "segmenter", "task": "segmentation"}]}
)


def _success(**values):
    return {"schema": ROUTING_DATA_PROTOCOL, "status": "ok", **values}


def _rejected(reason):
    return {"schema": ROUTING_DATA_PROTOCOL, "status": "rejected", "reason": reason}


def _registry_row(registry_id, *, capabilities=None, checked_at=None):
    return {
        "id": str(registry_id),
        "name": f"backend-{registry_id.hex[:6]}",
        "url": f"http://{registry_id.hex[:6]}.example.test",
        "state": "connected",
        "is_interactive": False,
        "auth_method": "token",
        "auth_token": "private-synthetic-credential",
        "extra_params": {"max_concurrency": 2},
        "gpu_resource_id": None,
        "vram_budget_mb": None,
        "eviction_priority": 1,
        "health_meta": (
            {"capabilities": capabilities} if capabilities is not None else None
        ),
        "source": "manual",
        "error_message": None,
        "last_checked_at": (checked_at or _NOW - timedelta(seconds=1)).isoformat(),
        "created_at": (_NOW - timedelta(days=2)).isoformat(),
        "updated_at": (_NOW - timedelta(minutes=1)).isoformat(),
    }


def _pool_row(*, fingerprint=None, legacy_id=_LEGACY_ID):
    return {
        "id": str(_POOL_ID),
        "name": "fixture pool",
        "enabled": True,
        "routing_policy": "smooth_weighted_round_robin",
        "legacy_instance_id": str(legacy_id) if legacy_id is not None else None,
        "routing_generation": 7,
        "capability_fingerprint": fingerprint,
        "capability_snapshot": _CAPABILITIES if fingerprint is not None else None,
        "created_at": (_NOW - timedelta(days=3)).isoformat(),
        "updated_at": (_NOW - timedelta(minutes=1)).isoformat(),
    }


def _member_row(registry_id, *, weight=1, state="active"):
    return {
        "id": str(UUID(int=registry_id.int + 1000)),
        "pool_id": str(_POOL_ID),
        "registry_id": str(registry_id),
        "traffic_state": state,
        "weight": weight,
        "created_at": (_NOW - timedelta(days=3)).isoformat(),
        "updated_at": (_NOW - timedelta(minutes=1)).isoformat(),
    }


def _snapshot(*, pool=None, members=None):
    return {
        "snapshot": "opaque-snapshot-token",
        "pool": _pool_row(fingerprint=capability_fingerprint(_CAPABILITIES))
        if pool is None
        else pool,
        "members": [
            {
                "member": _member_row(_LEGACY_ID, weight=2),
                "registry": _registry_row(_LEGACY_ID, capabilities=_CAPABILITIES),
                "max_concurrency": 2,
            },
            {
                "member": _member_row(_STALE_ID, weight=5),
                "registry": _registry_row(
                    _STALE_ID,
                    capabilities=_CAPABILITIES,
                    checked_at=_NOW - timedelta(minutes=5),
                ),
                "max_concurrency": 3,
            },
            {
                "member": _member_row(_DRIFT_ID, weight=4),
                "registry": _registry_row(
                    _DRIFT_ID, capabilities=_DRIFTED_CAPABILITIES
                ),
                "max_concurrency": 4,
            },
        ]
        if members is None
        else members,
        "observed_at": _NOW.isoformat(),
    }


def _scope(registry_id, *, mode="enforce", pool_id=_POOL_ID, project_id=_PROJECT_ID):
    return {
        "id": str(_SCOPE_ID),
        "pool_id": str(pool_id) if pool_id is not None else None,
        "registry_id": str(registry_id),
        "project_id": str(project_id) if project_id is not None else None,
        "mode": mode,
        "owner": "api:test-request",
        "operation": "predict",
        "generation": 7 if pool_id is not None else None,
        "config_revision": 1,
        "eligibility_revision": 1,
        "authority_epoch": str(_AUTHORITY_EPOCH),
        "state": "reserved",
        "closed": False,
        "heartbeat_deadline_ms": 1_800_000_000_000,
        "capability": "private-scope-capability",
    }


class _Native:
    def __init__(self, *, snapshot=None, receipts=None, handlers=None):
        self.snapshot = _snapshot() if snapshot is None else snapshot
        self.receipts = {} if receipts is None else receipts
        self.handlers = {} if handlers is None else handlers
        self.calls = []
        self.active_transaction = False
        self.events = []
        self.last_admission = None

    async def step(self, action, arguments):
        self.calls.append((action, dict(arguments)))
        self.events.append(("step", action, self.active_transaction))
        if action == "command_status":
            receipt = self.receipts.get(
                (arguments["request_id"], arguments["command_sha256"])
            )
            return _success(receipt=receipt)
        if action == "record_command":
            receipt = {
                "request_id": arguments["request_id"],
                "command_sha256": arguments["command_sha256"],
                "operation": arguments["operation"],
                "state": "caller_pending",
                "result": arguments["result"],
            }
            key = (receipt["request_id"], receipt["command_sha256"])
            self.receipts[key] = receipt
            return _success(receipt=receipt)
        handler = self.handlers.get(action)
        if handler is not None:
            result = handler(arguments)
            if isinstance(result, Exception):
                raise result
            return result
        if action == "admission_snapshot":
            self.last_admission = dict(arguments)
            return _success(**self.snapshot)
        if action == "reserve_scope":
            selected = arguments["candidate_ids"][0]
            admission = self.last_admission
            return _success(
                scope=_scope(
                    UUID(selected),
                    mode=admission["mode"],
                    pool_id=(
                        UUID(admission["pool_id"]) if admission["pool_id"] else None
                    ),
                    project_id=(
                        UUID(admission["project_id"])
                        if admission["project_id"]
                        else None
                    ),
                )
            )
        raise AssertionError(f"unexpected Native action {action}")


class _DataContext:
    def __init__(self, native, *, fail_commit=False):
        self.native = native
        self.fail_commit = fail_commit

    async def __aenter__(self):
        assert self.native.active_transaction is False
        self.native.active_transaction = True
        self.native.events.append(("enter",))
        return self.native

    async def __aexit__(self, error_type, _error, _traceback):
        self.native.active_transaction = False
        self.native.events.append(("exit", error_type))
        if error_type is None:
            for receipt in self.native.receipts.values():
                if receipt["state"] == "caller_pending":
                    receipt["state"] = "committed"
            if self.fail_commit:
                raise RuntimeError("synthetic ambiguous commit")
        return False


class _DataFactory:
    def __init__(self, native, *, fail_next_commit=False):
        self.native = native
        self.fail_next_commit = fail_next_commit

    def __call__(self):
        fail = self.fail_next_commit
        self.fail_next_commit = False
        return _DataContext(self.native, fail_commit=fail)


def _acquire(*, mode="enforce", request_id=None, required=None, registry_id=None):
    return make_routing_command(
        "acquire",
        {
            "pool_id": str(_POOL_ID) if mode != "direct" else None,
            "registry_id": str(registry_id) if registry_id is not None else None,
            "project_id": str(_PROJECT_ID) if mode != "direct" else None,
            "mode": mode,
            "required_instance_id": str(required) if required is not None else None,
            "owner": "api:test-request",
            "operation": "predict",
        },
        request_id=request_id,
    )


def _manager(native, **kwargs):
    return RoutingManager(
        _DataFactory(native),
        health_max_age_seconds=60,
        lease_ttl_ms=120_000,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_enforce_eligibility_uses_full_rows_and_python_capability_fingerprint():
    native = _Native()
    result = await _manager(native).execute(_acquire())

    assert result.state == "committed"
    assert result.result.registry_id == _LEGACY_ID
    reserve = next(args for action, args in native.calls if action == "reserve_scope")
    assert reserve["candidate_ids"] == [str(_LEGACY_ID)]
    assert reserve["snapshot"] == "opaque-snapshot-token"
    assert "private-synthetic-credential" not in repr(result)


@pytest.mark.asyncio
async def test_off_mode_tracks_the_legacy_instance_without_health_gating():
    native = _Native()
    result = await _manager(native).execute(_acquire(mode="off"))

    assert result.result.registry_id == _LEGACY_ID
    reserve = next(args for action, args in native.calls if action == "reserve_scope")
    assert reserve["candidate_ids"] == [str(_LEGACY_ID)]


@pytest.mark.asyncio
async def test_direct_mode_tracks_exact_registry_without_a_pool_or_health_gate():
    snapshot = _snapshot(members=[])
    snapshot["pool"] = None
    native = _Native(snapshot=snapshot)
    result = await _manager(native).execute(
        _acquire(mode="direct", registry_id=_STALE_ID)
    )

    assert result.result.registry_id == _STALE_ID
    reserve = next(args for action, args in native.calls if action == "reserve_scope")
    assert reserve["candidate_ids"] == [str(_STALE_ID)]


@pytest.mark.asyncio
async def test_begin_opens_the_opaque_subject_to_a_digest_before_registering_attempt():
    identity_digest = "b" * 64
    active_scope = {**_scope(_LEGACY_ID), "state": "active"}
    native = _Native(
        handlers={
            "open_transport_subject": lambda args: _success(
                transport_identity={"identity_sha256": identity_digest}
            ),
            "begin_attempt": lambda args: _success(
                attempt_id=args["attempt_id"],
                scope=active_scope,
                headers={"X-AAP-Route-Admission": "synthetic-signed-token"},
            ),
        }
    )
    command = make_routing_command(
        "begin",
        {
            "scope_id": str(_SCOPE_ID),
            "capability": "private-scope-capability",
            "attempt_id": str(_ATTEMPT_ID),
            "method": "POST",
            "path": "/predict",
            "transport_subject": "opaque-v1.synthetic-subject",
        },
    )

    result = await _manager(native).execute(command)

    open_call = next(
        args for action, args in native.calls if action == "open_transport_subject"
    )
    begin_call = next(
        args for action, args in native.calls if action == "begin_attempt"
    )
    assert open_call["subject"] == "opaque-v1.synthetic-subject"
    assert begin_call["transport_identity"] == {"identity_sha256": identity_digest}
    assert "transport_subject" not in begin_call
    assert result.result.headers["X-AAP-Route-Admission"] == "synthetic-signed-token"
    assert "private-scope-capability" not in repr(result)


@pytest.mark.asyncio
async def test_control_begin_restricts_the_lifecycle_request_surface():
    native = _Native()
    command = make_routing_command(
        "begin_control",
        {
            "control_id": str(_CONTROL_ID),
            "capability": "private-control-capability",
            "attempt_id": str(_ATTEMPT_ID),
            "method": "GET",
            "path": "/health",
            "transport_subject": "opaque-v1.synthetic-subject",
        },
    )

    result = await _manager(native).execute(command)

    assert isinstance(result.result, RoutingRejectedResult)
    assert result.result.reason == "invalid_command"
    assert not any(action == "open_transport_subject" for action, _ in native.calls)


@pytest.mark.asyncio
async def test_settle_maps_private_attempt_id_to_the_closed_public_attempt_view():
    native = _Native(
        handlers={
            "settle_attempt": lambda _args: _success(
                attempt={
                    "id": str(_ATTEMPT_ID),
                    "state": "uncertain",
                    "status_code": None,
                    "reason": "transport_timeout",
                }
            )
        }
    )
    command = make_routing_command(
        "settle",
        {
            "scope_id": str(_SCOPE_ID),
            "capability": "private-scope-capability",
            "attempt_id": str(_ATTEMPT_ID),
            "kind": "uncertain",
            "status_code": None,
            "reason": "transport_timeout",
        },
    )

    result = await _manager(native).execute(command)

    assert result.result.attempt_id == _ATTEMPT_ID
    assert result.result.state == "uncertain"


@pytest.mark.asyncio
async def test_heartbeat_and_close_keep_uncertain_work_durable():
    uncertain_scope = {**_scope(_LEGACY_ID), "state": "uncertain", "closed": True}
    native = _Native(
        handlers={
            "heartbeat_scope": lambda _args: _success(scope=uncertain_scope),
            "close_scope": lambda _args: _success(scope=uncertain_scope),
        }
    )
    manager = _manager(native)
    heartbeat = make_routing_command(
        "heartbeat",
        {"scope_id": str(_SCOPE_ID), "capability": "private-scope-capability"},
    )
    close = make_routing_command(
        "close",
        {
            "scope_id": str(_SCOPE_ID),
            "capability": "private-scope-capability",
            "outcome": "error",
        },
    )

    heartbeat_result = await manager.execute(heartbeat)
    close_result = await manager.execute(close)

    assert heartbeat_result.result.state == "uncertain"
    assert close_result.result.state == "uncertain"
    heartbeat_args = next(
        args for action, args in native.calls if action == "heartbeat_scope"
    )
    assert heartbeat_args["ttl_ms"] == 120_000


@pytest.mark.asyncio
async def test_scope_status_translates_native_attempt_ids():
    native = _Native(
        handlers={
            "scope_status": lambda _args: _success(
                scope=_scope(_LEGACY_ID),
                attempts=[
                    {
                        "id": str(_ATTEMPT_ID),
                        "state": "active",
                        "status_code": None,
                        "reason": None,
                    }
                ],
            )
        }
    )
    command = make_routing_command(
        "scope_status",
        {"scope_id": str(_SCOPE_ID), "capability": "private-scope-capability"},
    )

    result = await _manager(native).execute(command)

    assert result.result.scope.id == _SCOPE_ID
    assert result.result.attempts[0].attempt_id == _ATTEMPT_ID
    assert result.result.attempts[0].state == "active"


@pytest.mark.asyncio
async def test_control_attempts_are_bound_to_the_persistent_control_capability():
    control = {
        "control_id": str(_CONTROL_ID),
        "registry_id": str(_LEGACY_ID),
        "kind": "reload",
        "phase": "prepared",
        "capability": "private-control-capability",
    }
    native = _Native(
        handlers={
            "open_transport_subject": lambda _args: _success(
                transport_identity={"identity_sha256": "c" * 64}
            ),
            "begin_control_attempt": lambda _args: _success(
                attempt_id=str(_ATTEMPT_ID),
                control=control,
                headers={"X-AAP-Route-Admission": "exclusive-control-token"},
            ),
            "settle_control_attempt": lambda _args: _success(
                attempt={
                    "id": str(_ATTEMPT_ID),
                    "state": "response",
                    "status_code": 200,
                    "reason": None,
                }
            ),
            "finish_control": lambda _args: _success(
                control_id=str(_CONTROL_ID),
                registry_id=str(_LEGACY_ID),
                kind="reload",
                phase="completed",
            ),
        }
    )
    manager = _manager(native)
    begin = make_routing_command(
        "begin_control",
        {
            "control_id": str(_CONTROL_ID),
            "capability": "private-control-capability",
            "attempt_id": str(_ATTEMPT_ID),
            "method": "POST",
            "path": "/reload",
            "transport_subject": "opaque-v1.control-subject",
        },
    )
    settle = make_routing_command(
        "settle_control",
        {
            "control_id": str(_CONTROL_ID),
            "capability": "private-control-capability",
            "attempt_id": str(_ATTEMPT_ID),
            "kind": "response",
            "status_code": 200,
            "reason": None,
        },
    )
    finish = make_routing_command(
        "finish_control",
        {"control_id": str(_CONTROL_ID), "capability": "private-control-capability"},
    )

    begin_result = await manager.execute(begin)
    settle_result = await manager.execute(settle)
    finish_result = await manager.execute(finish)

    assert begin_result.result.control.control_id == _CONTROL_ID
    assert (
        begin_result.result.headers["X-AAP-Route-Admission"]
        == "exclusive-control-token"
    )
    assert settle_result.result.attempt_id == _ATTEMPT_ID
    assert finish_result.result.phase == "completed"
    assert finish_result.result.capability == "private-control-capability"
    finish_args = next(
        args for action, args in native.calls if action == "finish_control"
    )
    assert set(finish_args) == {"control_id", "capability"}


@pytest.mark.asyncio
async def test_native_rejection_is_receipted_and_duplicate_request_replays_it():
    native = _Native(
        handlers={"admission_snapshot": lambda _args: _rejected("pool_not_enabled")}
    )
    command = _acquire(request_id=UUID("d5cae7a9-3e44-43e5-9453-25a8e6f06558"))
    manager = _manager(native)

    first = await manager.execute(command)
    second = await manager.execute(command)

    assert isinstance(first.result, RoutingRejectedResult)
    assert first.result.reason == "pool_not_enabled"
    assert second.result == first.result
    assert sum(action == "admission_snapshot" for action, _ in native.calls) == 1
    assert sum(action == "record_command" for action, _ in native.calls) == 1
    assert routing_command_sha256(command) == first.command_sha256


@pytest.mark.asyncio
async def test_ambiguous_commit_returns_unknown_then_status_resolves_receipt():
    native = _Native()
    factory = _DataFactory(native, fail_next_commit=True)
    manager = RoutingManager(
        factory,
        health_max_age_seconds=60,
        lease_ttl_ms=120_000,
    )

    command = _acquire()
    result = await manager.execute(command)

    assert result.state == "unknown"
    assert result.result is None
    assert sum(action == "reserve_scope" for action, _ in native.calls) == 1
    status = await manager.status(make_routing_status_request(command))
    assert status.state == "committed"
    assert sum(action == "command_status" for action, _ in native.calls) == 2


def _backend_reply(*, phase="bound", active_requests=0):
    return {
        "schema": "aap-backend-route-control-reply/v1",
        "command_id": str(_CONTROL_ID),
        "registry_id": str(_LEGACY_ID),
        "authority_epoch": str(_NEW_EPOCH),
        "backend_incarnation": str(_NEW_INCARNATION),
        "challenge": _CHALLENGE,
        "phase": phase,
        "active_requests": active_requests,
    }


@pytest.mark.asyncio
async def test_reconcile_rejected_before_control_io_has_a_committed_replayable_receipt():
    native = _Native(
        handlers={"prepare_epoch": lambda _args: _rejected("member_not_quiescent")}
    )

    async def backend_control(_context):
        pytest.fail("rejected epoch preparation must not call the backend")

    manager = _manager(native, backend_control=backend_control)
    command = make_routing_command(
        "reconcile", {"registry_id": str(_LEGACY_ID), "control_id": str(_CONTROL_ID)}
    )
    first = await manager.execute(command)
    second = await manager.execute(command)
    assert first.state == second.state == "committed"
    assert isinstance(first.result, RoutingRejectedResult)
    assert first.result.reason == "member_not_quiescent"
    assert first.result == second.result
    assert sum(action == "prepare_epoch" for action, _ in native.calls) == 1


@pytest.mark.asyncio
async def test_reconcile_commits_prepare_before_control_io_and_finishes_only_bound():
    prepared = {
        "control_id": str(_CONTROL_ID),
        "registry_id": str(_LEGACY_ID),
        "phase": "prepared",
        "new_epoch": str(_NEW_EPOCH),
        "challenge": _CHALLENGE,
        "retired_epoch": str(_AUTHORITY_EPOCH),
        "backend_incarnation": str(_BACKEND_INCARNATION),
        "endpoint": {
            "url": "http://backend.example.test",
            "auth_method": "token",
            "auth_token": "private-backend-token",
        },
        "signing_key": base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    }
    epoch = {
        "control_id": str(_CONTROL_ID),
        "registry_id": str(_LEGACY_ID),
        "phase": "completed",
        "authority_epoch": str(_NEW_EPOCH),
        "backend_incarnation": str(_NEW_INCARNATION),
    }
    native = _Native(
        handlers={
            "prepare_epoch": lambda _args: _success(**prepared),
            "finish_epoch": lambda _args: _success(**epoch),
        }
    )
    factory = _DataFactory(native)
    control_calls = []

    async def backend_control(private_context):
        assert factory.native.active_transaction is False
        control_calls.append(dict(private_context))
        return _backend_reply()

    manager = RoutingManager(
        factory,
        health_max_age_seconds=60,
        lease_ttl_ms=120_000,
        backend_control=backend_control,
    )
    command = make_routing_command(
        "reconcile",
        {"registry_id": str(_LEGACY_ID), "control_id": str(_CONTROL_ID)},
    )

    result = await manager.execute(command)

    assert result.result.phase == "completed"
    assert len(control_calls) == 1
    assert [event[0] for event in native.events if event[0] in {"enter", "exit"}] == [
        "enter",
        "exit",
        "enter",
        "exit",
    ]
    finish_args = next(
        args for action, args in native.calls if action == "finish_epoch"
    )
    assert finish_args["reply"]["phase"] == "bound"
    assert "private-backend-token" not in repr(result)


@pytest.mark.asyncio
async def test_reconcile_records_draining_progress_without_finishing_epoch():
    prepared = {
        "control_id": str(_CONTROL_ID),
        "registry_id": str(_LEGACY_ID),
        "phase": "draining",
        "new_epoch": str(_NEW_EPOCH),
        "challenge": _CHALLENGE,
        "retired_epoch": str(_AUTHORITY_EPOCH),
        "backend_incarnation": str(_BACKEND_INCARNATION),
        "endpoint": {
            "url": "http://backend.example.test",
            "auth_method": None,
            "auth_token": None,
        },
        "signing_key": base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    }
    native = _Native(handlers={"prepare_epoch": lambda _args: _success(**prepared)})
    calls = []

    async def backend_control(_context):
        calls.append(True)
        return _backend_reply(phase="draining", active_requests=3)

    result = await RoutingManager(
        _DataFactory(native),
        health_max_age_seconds=60,
        lease_ttl_ms=120_000,
        backend_control=backend_control,
    ).execute(
        make_routing_command(
            "reconcile",
            {"registry_id": str(_LEGACY_ID), "control_id": str(_CONTROL_ID)},
        )
    )

    assert result.state == "committed"
    assert result.result.phase == "draining"
    assert calls == [True]
    assert not any(action == "finish_epoch" for action, _ in native.calls)


def _runtime_row(registry_id, *, open_scopes=0, active=0, uncertain=0):
    return {
        "registry_id": str(registry_id),
        "open_scopes": open_scopes,
        "active_attempts": active,
        "uncertain_attempts": uncertain,
        "continuity": "ready",
        "authority_epoch": str(_AUTHORITY_EPOCH),
        "backend_incarnation": str(_BACKEND_INCARNATION),
        "control_id": None,
    }


@pytest.mark.asyncio
async def test_runtime_snapshot_calls_only_readonly_native_action_and_binds_ids():
    request = make_routing_runtime_request([_STALE_ID, _LEGACY_ID])

    def read_snapshot(arguments):
        assert arguments == {"registry_ids": [str(_STALE_ID), str(_LEGACY_ID)]}
        return _success(
            observed_at=_NOW.isoformat(),
            registries=[
                _runtime_row(_LEGACY_ID, open_scopes=2, active=1, uncertain=1),
                _runtime_row(_STALE_ID, open_scopes=0),
            ],
        )

    native = _Native(handlers={"runtime_snapshot": read_snapshot})

    result = await _manager(native).runtime(request)

    assert result.request_id == request.request_id
    assert [row.registry_id for row in result.registries] == [
        _LEGACY_ID,
        _STALE_ID,
    ]
    assert result.registries[0].open_scopes == 2
    assert [action for action, _args in native.calls] == ["runtime_snapshot"]
    assert native.receipts == {}


@pytest.mark.asyncio
async def test_runtime_snapshot_accepts_empty_request_and_rejects_unrequested_rows():
    def empty_snapshot(arguments):
        assert arguments == {"registry_ids": []}
        return _success(observed_at=_NOW.isoformat(), registries=[])

    empty_native = _Native(handlers={"runtime_snapshot": empty_snapshot})
    empty_result = await _manager(empty_native).runtime(
        make_routing_runtime_request([])
    )
    assert empty_result.registries == []
    assert [action for action, _args in empty_native.calls] == ["runtime_snapshot"]

    extra_native = _Native(
        handlers={
            "runtime_snapshot": lambda _arguments: _success(
                observed_at=_NOW.isoformat(),
                registries=[_runtime_row(_DRIFT_ID)],
            )
        }
    )
    with pytest.raises(RoutingUnavailable):
        await _manager(extra_native).runtime(make_routing_runtime_request([_LEGACY_ID]))

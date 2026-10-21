from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from aap_inference.scope_retirement_manager import ScopeRetirementManager
from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_DATA_PROTOCOL,
    SCOPE_RETIREMENT_NATIVE_FUNCTIONS,
    make_scope_retirement_request,
)

_RETIREMENT_ID = "e469fa57-ff59-42ae-9fe7-366f895931f7"
_SCOPE_ID = "c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf"
_REGISTRY_ID = "d4e6676d-9fe4-4c33-9ca5-fc1c31230052"
_EPOCH_ID = "6a7a2139-2428-4c28-b4c8-6b0c40034289"
_INCARNATION_ID = "be189eb2-2e7d-4c36-94b0-20ad78d68147"
_CHALLENGE = "a" * 64


def _context():
    return {
        "retirement_id": _RETIREMENT_ID,
        "scope_id": _SCOPE_ID,
        "registry_id": _REGISTRY_ID,
        "authority_epoch": _EPOCH_ID,
        "backend_incarnation": _INCARNATION_ID,
        "challenge": _CHALLENGE,
        "fence_until": 1_800_000_000,
        "endpoint": {
            "url": "http://backend.local",
            "auth_method": "token",
            "auth_token": "private-management-token",
        },
    }


def _reply(*, phase="retired", active_requests=0, **overrides):
    value = {
        "schema": "aap-backend-scope-retirement-reply/v1",
        "retirement_id": _RETIREMENT_ID,
        "registry_id": _REGISTRY_ID,
        "scope_id": _SCOPE_ID,
        "authority_epoch": _EPOCH_ID,
        "backend_incarnation": _INCARNATION_ID,
        "challenge": _CHALLENGE,
        "phase": phase,
        "active_requests": active_requests,
    }
    return {**value, **overrides}


class _Store:
    def __init__(self):
        self.retirement = None
        self.context = None
        self.actions = []
        self.transactions = []
        self.fail_after_prepare_commit = False
        self.fail_after_finish_commit = False
        self.expose_private_context_in_status = False

    def factory(self):
        @asynccontextmanager
        async def transaction():
            data = _Data(self)
            self.transactions.append(data)
            try:
                yield data
            except BaseException:
                raise
            else:
                data.committed = True
                if data.last_action == "prepare" and self.fail_after_prepare_commit:
                    self.fail_after_prepare_commit = False
                    raise RuntimeError("prepare commit acknowledgement lost")
                if data.last_action == "finish" and self.fail_after_finish_commit:
                    self.fail_after_finish_commit = False
                    raise RuntimeError("commit acknowledgement lost")

        return transaction()


class _Data:
    def __init__(self, store):
        self.store = store
        self.committed = False
        self.last_action = None

    async def retirement_step(self, action, arguments):
        self.last_action = action
        if action == "policy":
            self.store.actions.append((action, dict(arguments)))
            return {
                "schema": SCOPE_RETIREMENT_DATA_PROTOCOL,
                "status": "ok",
                "ready": True,
                "functions": list(SCOPE_RETIREMENT_NATIVE_FUNCTIONS),
            }
        self.store.actions.append((action, dict(arguments)))
        if action == "prepare":
            if self.store.retirement is None:
                self.store.retirement = "prepared"
                self.store.context = _context()
            return self._intent_reply()
        if action == "status":
            reply = self._intent_reply(expose_context=False)
            if self.store.expose_private_context_in_status:
                reply["context"] = self.store.context
            return reply
        if action == "finish":
            self.store.retirement = (
                "completed" if arguments["reply"]["phase"] == "retired" else "draining"
            )
            if self.store.retirement == "completed":
                self.store.context = None
            return self._intent_reply()
        raise AssertionError(action)

    def _intent_reply(self, *, expose_context=True):
        if self.store.retirement is None:
            retirement = None
            context = None
        else:
            phase = self.store.retirement
            retirement = {
                "retirement_id": _RETIREMENT_ID,
                "scope_id": _SCOPE_ID,
                "registry_id": _REGISTRY_ID,
                "authority_epoch": _EPOCH_ID,
                "backend_incarnation": _INCARNATION_ID,
                "phase": phase,
                "closed": True,
            }
            context = self.store.context if expose_context else None
        return {
            "schema": SCOPE_RETIREMENT_DATA_PROTOCOL,
            "status": "ok",
            "retirement": retirement,
            "context": context,
        }


def _request():
    return make_scope_retirement_request(
        _RETIREMENT_ID, _SCOPE_ID, "private-scope-capability"
    )


@pytest.mark.asyncio
async def test_prepare_commits_before_backend_and_finish_returns_bound_view(
    monkeypatch,
):
    store = _Store()
    backend_calls = []

    async def backend(context):
        backend_calls.append(context)
        assert store.transactions[-1].committed is True
        assert context["endpoint"]["auth_token"] == "private-management-token"
        return _reply()

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).execute(_request())

    assert result.state == "committed"
    assert result.result.phase == "completed"
    assert store.retirement == "completed"
    assert len(backend_calls) == 1
    assert [action for action, _ in store.actions] == [
        "policy",
        "prepare",
        "policy",
        "finish",
    ]
    assert "private-management-token" not in repr(result)


@pytest.mark.asyncio
async def test_status_is_read_only_and_does_not_call_backend():
    store = _Store()
    backend_calls = 0

    async def backend(_context):
        nonlocal backend_calls
        backend_calls += 1
        return _reply()

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).status(_request())

    assert result.state == "unknown"
    assert backend_calls == 0
    assert [action for action, _ in store.actions] == ["policy", "status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "draining", "completed"])
async def test_status_decodes_public_phase_without_private_context(phase):
    store = _Store()
    store.retirement = phase
    store.context = None if phase == "completed" else _context()
    backend_calls = 0

    async def backend(_context):
        nonlocal backend_calls
        backend_calls += 1
        return _reply()

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).status(_request())

    assert result.state == "committed"
    assert result.result.phase == phase
    assert backend_calls == 0
    status_call = next(
        arguments for action, arguments in store.actions if action == "status"
    )
    assert status_call["capability"] == "private-scope-capability"


@pytest.mark.asyncio
async def test_status_rejects_unexpected_private_context_without_calling_backend():
    store = _Store()
    store.retirement = "prepared"
    store.context = _context()
    store.expose_private_context_in_status = True
    backend_calls = 0

    async def backend(_context):
        nonlocal backend_calls
        backend_calls += 1
        return _reply()

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).status(_request())

    assert result.state == "unknown"
    assert backend_calls == 0


@pytest.mark.asyncio
async def test_lost_finish_commit_is_recovered_by_one_status_read(monkeypatch):
    store = _Store()
    store.fail_after_finish_commit = True
    backend_calls = 0

    async def backend(_context):
        nonlocal backend_calls
        backend_calls += 1
        return _reply()

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).execute(_request())

    assert result.state == "committed"
    assert result.result.phase == "completed"
    assert backend_calls == 1
    assert [action for action, _ in store.actions].count("status") == 1
    assert [action for action, _ in store.actions].count("finish") == 1


@pytest.mark.asyncio
async def test_lost_prepare_commit_returns_public_phase_without_backend_call():
    store = _Store()
    store.fail_after_prepare_commit = True
    backend_calls = 0

    async def backend(_context):
        nonlocal backend_calls
        backend_calls += 1
        return _reply()

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).execute(_request())

    assert result.state == "committed"
    assert result.result.phase == "prepared"
    assert backend_calls == 0
    assert [action for action, _ in store.actions].count("prepare") == 1
    assert [action for action, _ in store.actions].count("status") == 1


@pytest.mark.asyncio
async def test_lost_draining_finish_commit_recovers_phase_without_second_control(
    monkeypatch,
):
    store = _Store()
    store.fail_after_finish_commit = True
    backend_calls = 0

    async def backend(_context):
        nonlocal backend_calls
        backend_calls += 1
        return _reply(phase="draining", active_requests=2)

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).execute(_request())

    assert result.state == "committed"
    assert result.result.phase == "draining"
    assert backend_calls == 1
    assert [action for action, _ in store.actions].count("finish") == 1
    assert [action for action, _ in store.actions].count("status") == 1


@pytest.mark.asyncio
async def test_mismatched_backend_reply_never_reaches_finish(monkeypatch):
    store = _Store()

    async def backend(_context):
        return _reply(scope_id="22222222-2222-4222-8222-222222222222")

    result = await ScopeRetirementManager(
        store.factory, backend_retirement=backend
    ).execute(_request())

    assert result.state == "unknown"
    assert store.retirement == "prepared"
    assert [action for action, _ in store.actions].count("finish") == 0

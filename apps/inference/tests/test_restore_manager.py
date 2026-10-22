from __future__ import annotations

import base64
import copy

import pytest

from aap_inference.restore import VideoRestoreManager
from aap_inference.restore_contract import (
    RESTORE_DATA_PROTOCOL,
    RESTORE_PROTOCOL,
    RestoreCommand,
    RestoreUnavailable,
    canonical_command_json,
    command_sha256,
    decode_native_envelope,
    decode_restore_command,
)
from aap_protocol_v2.routing import ROUTE_CONTROL_REPLY_PROTOCOL

RESTORE_ID = "11111111-1111-4111-8111-111111111111"
CONTROL_ID = "22222222-2222-4222-8222-222222222222"
REGISTRY_ID = "33333333-3333-4333-8333-333333333333"
NEW_EPOCH = "44444444-4444-4444-8444-444444444444"
OLD_CONTROL_ID = "55555555-5555-4555-8555-555555555555"
OLD_EPOCH = "66666666-6666-4666-8666-666666666666"
INCARNATION = "77777777-7777-4777-8777-777777777777"
RESTARTED_INCARNATION = "88888888-8888-4888-8888-888888888888"
CHALLENGE = "a" * 64


def _command(**overrides):
    value = {
        "schema": RESTORE_PROTOCOL,
        "restore_id": RESTORE_ID,
        "backup_sha256": "1" * 64,
        "fence_sha256": "2" * 64,
        "source_jobs_realm": "16384:source_db:1234567890123456789",
        "target_jobs_realm": "16384:target_db:1234567890123456789",
        "source_routing_realm": "16385:routing_source",
        "target_routing_realm": "16385:routing_target",
        "source_jobs_secret_sha256": "3" * 64,
        "source_routing_secret_sha256": "4" * 64,
    }
    value.update(overrides)
    return value


def _context(*, phase="prepared", incarnation=INCARNATION):
    return {
        "control_id": CONTROL_ID,
        "registry_id": REGISTRY_ID,
        "phase": phase,
        "new_epoch": NEW_EPOCH,
        "challenge": CHALLENGE,
        "retired_epoch": None if phase == "prepared" else OLD_EPOCH,
        "backend_incarnation": incarnation,
        "endpoint": {
            "url": "https://backend.example.test/base/",
            "auth_method": "token",
            "auth_token": "backend-token",
        },
        "signing_key": base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
    }


def _record(command, *, phase="prepared"):
    return {
        "restore_id": command.restore_id,
        "phase": phase,
        "command_sha256": command_sha256(command),
        "backup_sha256": command.backup_sha256,
        "fence_sha256": command.fence_sha256,
        "source_jobs_realm": command.source_jobs_realm,
        "target_jobs_realm": command.target_jobs_realm,
        "source_routing_realm": command.source_routing_realm,
        "target_routing_realm": command.target_routing_realm,
        "started_at": "2026-10-22T10:11:12.123456Z",
        "completed_at": None if phase == "prepared" else "2026-10-22T10:12:13.123456Z",
        "counts": None
        if phase == "prepared"
        else {
            "executions_invalidated": 1,
            "route_intents_rewrapped": 1,
            "registries_rebound": 1,
            "scopes_retired": 1,
            "attempts_reconciled": 1,
        },
    }


def _envelope(command, context, *, phase="prepared"):
    return {
        "schema": RESTORE_DATA_PROTOCOL,
        "status": "ok",
        "restore": _record(command, phase=phase),
        "contexts": [] if phase == "completed" else [copy.deepcopy(context)],
    }


def _reply(
    *,
    command_id=OLD_CONTROL_ID,
    authority_epoch=OLD_EPOCH,
    incarnation=INCARNATION,
    challenge="b" * 64,
    phase="bound",
    active_requests=0,
):
    return {
        "schema": ROUTE_CONTROL_REPLY_PROTOCOL,
        "command_id": command_id,
        "registry_id": REGISTRY_ID,
        "authority_epoch": authority_epoch,
        "backend_incarnation": incarnation,
        "challenge": challenge,
        "phase": phase,
        "active_requests": active_requests,
    }


class _Data:
    def __init__(self, command, context, *, phase="prepared"):
        self.command = command
        self.context = context
        self.phase = phase
        self.actions = []

    async def step(self, action, arguments):
        self.actions.append(action)
        if action == "prepare":
            if self.phase == "completed":
                return _envelope(self.command, self.context, phase="completed")
            return _envelope(self.command, self.context)
        if action == "status":
            return _envelope(
                self.command,
                self.context,
                phase=self.phase,
            )
        if action == "observe":
            assert arguments["restore_id"] == RESTORE_ID
            assert arguments["registry_id"] == REGISTRY_ID
            if self.context["retired_epoch"] is None:
                self.context["retired_epoch"] = arguments["reply"]["authority_epoch"]
            return _envelope(self.command, self.context)
        if action == "record_bound":
            assert arguments["reply"]["backend_incarnation"] == INCARNATION
            self.context["phase"] = "bound"
            return _envelope(self.command, self.context)
        if action == "finish":
            assert self.context["phase"] == "bound"
            self.phase = "completed"
            return _envelope(self.command, self.context, phase="completed")
        raise AssertionError(action)


class _Backend:
    def __init__(self, *, status_reply, control_reply=None):
        self.status_replies = (
            status_reply if type(status_reply) is list else [status_reply]
        )
        self.control_replies = (
            control_reply if type(control_reply) is list else [control_reply]
        )
        self.status_calls = []
        self.control_calls = []

    async def status(self, context):
        self.status_calls.append(copy.deepcopy(context))
        index = min(len(self.status_calls) - 1, len(self.status_replies) - 1)
        return self.status_replies[index]

    async def __call__(self, context):
        self.control_calls.append(copy.deepcopy(context))
        index = min(len(self.control_calls) - 1, len(self.control_replies) - 1)
        return self.control_replies[index]


def test_command_is_canonical_and_rejects_extra_or_duplicate_fields():
    command_value = _command(source_jobs_realm="16384:来源数据库:1234567890123456789")
    command = decode_restore_command(command_value)

    assert command.as_dict() == command_value
    assert (
        canonical_command_json(command).encode("utf-8").find("来源数据库".encode()) >= 0
    )
    assert len(command_sha256(command)) == 64
    with pytest.raises(RestoreUnavailable):
        decode_restore_command({**command_value, "unexpected": "value"})
    duplicate = b'{"schema":"aap-video-restore/v1","schema":"aap-video-restore/v1"}'
    with pytest.raises(RestoreUnavailable):
        decode_restore_command(duplicate)


def test_native_envelope_rejects_secret_or_unexpected_result_fields():
    command = RestoreCommand(**_command())
    envelope = _envelope(command, _context())
    with pytest.raises(RestoreUnavailable):
        decode_native_envelope(
            {**envelope, "dsn": "postgresql://secret"}, action="status"
        )


@pytest.mark.asyncio
async def test_completed_replay_does_not_contact_backend():
    command = RestoreCommand(**_command())
    context = _context(phase="bound")
    data = _Data(command, context, phase="completed")
    backend = _Backend(
        status_reply=AssertionError("completed replay must not request backend status"),
        control_reply=AssertionError("completed replay must not control a backend"),
    )
    manager = VideoRestoreManager(data, backend_control=backend)

    result = await manager.execute(command)

    assert result.state == "completed"
    assert backend.status_calls == []
    assert backend.control_calls == []


@pytest.mark.asyncio
async def test_pre_finish_status_rejects_backend_cold_restart_after_bound_record():
    command = RestoreCommand(**_command())
    context = _context(phase="bound")
    data = _Data(command, context)
    backend = _Backend(status_reply=_reply(incarnation=RESTARTED_INCARNATION))
    manager = VideoRestoreManager(data, backend_control=backend)

    result = await manager.execute(command)

    assert result.state == "pending"
    assert data.phase == "prepared"
    assert "finish" not in data.actions
    assert len(backend.status_calls) == 1
    assert backend.control_calls == []


@pytest.mark.asyncio
async def test_active_drain_returns_pending_without_finishing():
    command = RestoreCommand(**_command())
    context = _context()
    data = _Data(command, context)
    backend = _Backend(
        status_reply=_reply(),
        control_reply=_reply(
            command_id=CONTROL_ID,
            authority_epoch=NEW_EPOCH,
            challenge=CHALLENGE,
            phase="draining",
            active_requests=3,
        ),
    )
    manager = VideoRestoreManager(data, backend_control=backend)

    result = await manager.execute(command)

    assert result.state == "pending"
    assert result.record["phase"] == "prepared"
    assert "finish" not in data.actions
    assert backend.control_calls[0]["control_id"] == CONTROL_ID
    assert data.context["retired_epoch"] == OLD_EPOCH


@pytest.mark.asyncio
async def test_retry_resumes_same_control_ids_and_finishes_after_live_bound_status():
    command = RestoreCommand(**_command())
    context = _context()
    data = _Data(command, context)
    own_draining = _reply(
        command_id=CONTROL_ID,
        authority_epoch=NEW_EPOCH,
        challenge=CHALLENGE,
        phase="draining",
        active_requests=2,
    )
    own_bound = _reply(
        command_id=CONTROL_ID,
        authority_epoch=NEW_EPOCH,
        challenge=CHALLENGE,
        phase="bound",
        active_requests=0,
    )
    backend = _Backend(
        status_reply=[_reply(), own_draining, own_bound],
        control_reply=[own_draining, own_bound],
    )
    manager = VideoRestoreManager(data, backend_control=backend)

    first = await manager.execute(command)
    second = await manager.execute(command)

    assert first.state == "pending"
    assert second.state == "completed"
    assert data.context["retired_epoch"] == OLD_EPOCH
    assert [call["control_id"] for call in backend.control_calls] == [
        CONTROL_ID,
        CONTROL_ID,
    ]
    assert len(backend.status_calls) == 3

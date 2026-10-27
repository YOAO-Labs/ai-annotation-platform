from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import bcrypt
import pytest

from aap_identity.account_contract import (
    NATIVE_ACCOUNT_PROTOCOL,
    AccountFailure,
    AccountResultData,
    NativeAccountReply,
    NativePrepareData,
    NativePrepareReply,
    decode_native_apply_command,
    decode_native_prepare_command,
)
from aap_identity.account_manager import AccountManager, account_request_fingerprint
from aap_identity.authenticator import VerifiedCredential

from account_helpers import account_record, account_request
from session_helpers import SECRET, principal


class Authenticator:
    def __init__(self, credential):
        self.credential = credential
        self.calls = []

    async def authenticate_bound(self, token):
        self.calls.append(token)
        return self.credential


class Transaction:
    def __init__(self, data):
        self.data = data

    async def prepare_account_command(self, payload):
        assert self.data.in_transaction
        command = decode_native_prepare_command(payload)
        self.data.prepare_command = command
        self.data.events.append("prepare")
        current = command.actor.model_copy(update={"name": "Fresh account name"})
        account = self.data.account
        reply = NativePrepareReply(
            schema=NATIVE_ACCOUNT_PROTOCOL,
            ok=True,
            operation=command.operation,
            operation_id=command.operation_id,
            principal=current,
            data=NativePrepareData(
                account=account,
                password_hash=self.data.password_hash
                if command.operation == "change_password" and self.data.replay is None
                else None,
                replay=self.data.replay,
            ),
        )
        return reply

    async def apply_account_command(self, payload):
        assert self.data.in_transaction
        command = decode_native_apply_command(payload)
        self.data.apply_command = command
        self.data.events.append("apply")
        if self.data.apply_failure is not None:
            return {
                "schema": NATIVE_ACCOUNT_PROTOCOL,
                "ok": False,
                "error": {
                    "code": self.data.apply_failure,
                    "status_code": None,
                    "detail": None,
                },
            }
        op = command.operation
        if op == "update_name":
            data = AccountResultData(
                account=account_record(name=command.new_value),
                preferences=None,
                previous_avatar_ref=None,
                changed=command.base_value != command.new_value,
            )
        elif op == "patch_preferences":
            data = AccountResultData(
                account=None,
                preferences=command.response_preferences,
                previous_avatar_ref=None,
                changed=None,
            )
        elif op == "set_avatar_ref":
            changed = command.base_value != command.new_value.avatar_ref
            data = AccountResultData(
                account=account_record(avatar_ref=command.new_value.avatar_ref),
                preferences=None,
                previous_avatar_ref=command.base_value if changed else None,
                changed=changed,
            )
        else:
            data = AccountResultData(
                account=None,
                preferences=None,
                previous_avatar_ref=None,
                changed=None,
            )
        reply = NativeAccountReply(
            schema=NATIVE_ACCOUNT_PROTOCOL,
            ok=True,
            operation=op,
            operation_id=command.operation_id,
            principal=command.actor,
            data=data,
        )
        return reply


class Data:
    def __init__(
        self, *, account=None, password_hash=None, replay=None, apply_failure=None
    ):
        self.account = account or account_record()
        self.password_hash = password_hash
        self.replay = replay
        self.apply_failure = apply_failure
        self.events = []
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False
        self.prepare_command = None
        self.apply_command = None

    @asynccontextmanager
    async def transaction(self):
        assert not self.in_transaction
        self.in_transaction = True
        try:
            yield Transaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False


def manager(data, *, actor=None, key_hash=None):
    actor = actor or principal()
    auth = Authenticator(VerifiedCredential(actor, key_hash))
    return AccountManager(auth, data, SECRET)


@pytest.mark.asyncio
async def test_update_name_is_trimmed_and_native_transaction_commits_after_validation():
    request = account_request("update_name", payload={"name": "  Updated name  "})
    data = Data()
    reply = await manager(data).execute(request)
    assert reply.data.account.name == "Updated name"
    assert reply.data.changed is True
    assert data.events == ["prepare", "apply"]
    assert data.committed and not data.rolled_back
    assert data.apply_command.base_value == "Current account"
    assert data.apply_command.new_value == "Updated name"
    assert data.prepare_command.request_fingerprint == account_request_fingerprint(
        SECRET, request
    )
    assert request.token not in json.dumps(
        data.prepare_command.model_dump(mode="json", by_alias=True)
    )


@pytest.mark.asyncio
async def test_update_name_replay_keeps_frozen_account_and_fresh_principal():
    request = account_request("update_name", payload={"name": "Historical name"})
    frozen = AccountResultData(
        account=account_record(name="Historical name"),
        preferences=None,
        previous_avatar_ref=None,
        changed=True,
    )
    data = Data(replay=frozen)
    reply = await manager(data).execute(request)
    assert reply.data.account.name == "Historical name"
    assert reply.principal.name == "Fresh account name"
    assert data.events == ["prepare"] and data.committed and data.apply_command is None


@pytest.mark.asyncio
async def test_blank_name_returns_safe_policy_failure_and_rolls_back():
    request = account_request("update_name", payload={"name": "  "})
    data = Data()
    with pytest.raises(AccountFailure) as failure:
        await manager(data).execute(request)
    assert failure.value.code == "policy_rejected"
    assert failure.value.status_code == 400 and failure.value.detail == "姓名不能为空"
    assert data.events == ["prepare"] and data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_preferences_merge_runs_on_locked_snapshot_and_preserves_large_values():
    existing = {
        "workbench": {"image": {"smoothImage": False}},
        "ai": {"model_by_backend": {"existing": "model-a"}},
    }
    large = "x" * 80_000
    request = account_request(
        "patch_preferences",
        payload={
            "patch": {
                "ai": {"model_by_backend": {"new": large}},
                "workbench": {"image": {"cssImageFilter": "contrast(1.1)"}},
            }
        },
    )
    data = Data(account=account_record(preferences=existing))
    reply = await manager(data).execute(request)
    assert len(reply.data.preferences["ai"]["model_by_backend"]["new"]) == 80_000
    assert data.apply_command.new_value["ai"]["model_by_backend"] == {
        "existing": "model-a",
        "new": large,
    }
    assert data.apply_command.new_value["workbench"]["image"] == {
        "smoothImage": False,
        "cssImageFilter": "contrast(1.1)",
    }
    assert data.committed


@pytest.mark.asyncio
async def test_invalid_preferences_are_safe_and_rollback_before_native_apply():
    request = account_request(
        "patch_preferences",
        payload={"patch": {"workbench": {"image": {"unknownField": "private"}}}},
    )
    data = Data()
    with pytest.raises(AccountFailure) as failure:
        await manager(data).execute(request)
    assert failure.value.code == "policy_rejected" and failure.value.status_code == 422
    assert isinstance(failure.value.detail, list)
    assert all(set(item) == {"type", "loc", "msg"} for item in failure.value.detail)
    assert "private" not in repr(failure.value)
    assert data.events == ["prepare"] and data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_password_change_checks_strength_old_password_and_hashes_inside_owner():
    password = "Current1Password"
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    request = account_request(
        "change_password",
        payload={"old_password": password, "new_password": "NewPassword!2026"},
    )
    data = Data(password_hash=hashed)
    reply = await manager(data).execute(request)
    assert reply.data.account is reply.data.preferences is None
    assert data.apply_command.base_value == hashed
    assert data.apply_command.new_value.startswith("$2")
    serialized = data.apply_command.model_dump_json()
    assert password not in serialized and request.payload.new_password not in serialized
    assert data.committed


@pytest.mark.asyncio
async def test_password_replay_skips_old_password_recheck_and_rehash(monkeypatch):
    request = account_request(
        "change_password",
        payload={"old_password": "wrong-old", "new_password": "weakpass"},
    )
    frozen = AccountResultData(
        account=None,
        preferences=None,
        previous_avatar_ref=None,
        changed=None,
    )
    data = Data(replay=frozen)

    def fail(*args, **kwargs):
        raise AssertionError("replay must not verify or rehash a password")

    monkeypatch.setattr("aap_identity.account_manager.verify_password", fail)
    monkeypatch.setattr("aap_identity.account_manager.hash_password", fail)
    reply = await manager(data).execute(request)
    assert reply.operation == "change_password" and reply.data.account is None
    assert data.events == ["prepare"] and data.committed and data.apply_command is None


@pytest.mark.asyncio
async def test_password_failures_return_only_safe_policy_diagnostics():
    current = "Current1Password"
    hashed = bcrypt.hashpw(current.encode(), bcrypt.gensalt(rounds=4)).decode()
    weak = account_request(
        "change_password",
        payload={"old_password": "wrong-old", "new_password": "lowercaseonly"},
    )
    with pytest.raises(AccountFailure) as weak_failure:
        await manager(Data(password_hash=hashed)).execute(weak)
    assert weak_failure.value.status_code == 422
    assert "wrong-old" not in repr(weak_failure.value)

    wrong = account_request(
        "change_password",
        payload={"old_password": "wrong-old", "new_password": "NewPassword!2026"},
    )
    data = Data(password_hash=hashed)
    with pytest.raises(AccountFailure) as wrong_failure:
        await manager(data).execute(wrong)
    assert wrong_failure.value.detail == "原密码不正确"
    assert wrong_failure.value.status_code == 400
    assert data.rolled_back and data.apply_command is None


@pytest.mark.asyncio
async def test_avatar_noop_returns_no_previous_ref_and_api_key_hash_is_fenced():
    actor = principal(
        "api_key",
        credential_id=uuid.uuid4(),
        scopes=["datasets:read"],
        generation=None,
        expires_at=None,
    )
    request = account_request(
        "set_avatar_ref",
        payload={"avatar_ref": "preset:blue", "kind": "preset_or_clear"},
    )
    data = Data(account=account_record(avatar_ref="preset:blue"))
    key_hash = "$2b$12$" + "a" * 53
    reply = await manager(data, actor=actor, key_hash=key_hash).execute(request)
    assert reply.data.changed is False
    assert reply.data.previous_avatar_ref is None
    assert data.prepare_command.expected_key_hash == key_hash


@pytest.mark.asyncio
async def test_upload_uses_the_frozen_expected_previous_reference_as_base():
    token = "a" * 32
    request = account_request(
        "set_avatar_ref",
        payload={
            "avatar_ref": "upload:" + token,
            "kind": "upload",
            "bytes": 9,
            "expected_previous_avatar_ref": "preset:blue",
            "receipt": {
                "token": token,
                "sha256": "b" * 64,
                "size_bytes": 9,
                "object_version_id": "version-1",
                "maintenance_revision": 1,
                "storage_ref": {
                    "schema": 1,
                    "purpose": "avatars",
                    "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
                    "config_revision": 1,
                    "binding_generation": 1,
                    "endpoint_url": "http://minio:9000",
                    "bucket": "avatars",
                    "object_key": token + ".webp",
                    "storage_identity": "c" * 64,
                },
            },
        },
    )
    data = Data(account=account_record(avatar_ref="preset:other"))
    reply = await manager(data).execute(request)
    assert data.apply_command.base_value == "preset:blue"
    assert data.apply_command.new_value.avatar_ref == "upload:" + token
    assert reply.data.account.avatar_ref == "upload:" + token


@pytest.mark.asyncio
async def test_heartbeat_uses_native_apply_without_receipt_payload():
    data = Data()
    reply = await manager(data).execute(account_request("heartbeat"))
    assert reply.data.model_dump(mode="json") == {
        "account": None,
        "preferences": None,
        "previous_avatar_ref": None,
        "changed": None,
    }
    assert data.apply_command.base_value is None
    assert data.apply_command.new_value is None
    assert data.committed


@pytest.mark.asyncio
async def test_account_busy_from_native_apply_remains_a_business_failure():
    data = Data(apply_failure="account_busy")
    with pytest.raises(AccountFailure) as failure:
        await manager(data).execute(account_request("update_name"))
    assert failure.value.code == "account_busy"
    assert failure.value.status_code is None and failure.value.detail is None
    assert data.events == ["prepare", "apply"]
    assert data.rolled_back and not data.committed

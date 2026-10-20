from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
import json

import pytest

from aap_identity.admin_password_contract import (
    ADMIN_PASSWORD_PROTOCOL,
    NATIVE_ADMIN_PASSWORD_PROTOCOL,
    AdminPasswordFailure,
    admin_password_failure_reply,
    decode_admin_password_reply,
    decode_native_admin_password_reply,
)
from aap_identity.contract import IdentityUnavailable
from admin_password_helpers import (
    admin_password_data,
    admin_password_request,
    native_admin_password_command,
    native_admin_password_success,
    wire_admin_password_success,
)
from aap_identity.admin_password_manager import (
    admin_password_derivation_id,
    admin_password_request_fingerprint,
    derive_admin_password,
)
from session_helpers import principal


def test_cross_language_derivation_vector_is_stable():
    vector = json.loads(
        (Path(__file__).parent / "fixtures/admin_password_vectors.json").read_text()
    )
    actor_id = uuid.UUID(vector["actor_id"])
    request = admin_password_request(
        operation_id=uuid.UUID(vector["operation_id"]),
        target_user_id=uuid.UUID(vector["target_user_id"]),
    )
    fingerprint = admin_password_request_fingerprint(vector["signing_secret"], request)
    assert (
        json.dumps(
            {
                "operation": request.operation,
                "payload": request.payload.model_dump(mode="json", by_alias=True),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        == vector["canonical_request"]
    )
    assert fingerprint == vector["request_fingerprint"]
    assert (
        admin_password_derivation_id(vector["signing_secret"])
        == vector["derivation_id"]
    )
    assert (
        derive_admin_password(
            vector["signing_secret"],
            actor_id,
            request.operation_id,
            fingerprint,
        )
        == vector["temporary_password"]
    )


def test_wire_and_native_authentication_failures_decode_with_null_details():
    request = admin_password_request()
    command = native_admin_password_command(
        request, actor=principal(role="super_admin"), key_hash=None
    )
    for code in ("credential_invalid", "credential_revoked", "generation_outdated"):
        wire = admin_password_failure_reply(code)
        with pytest.raises(AdminPasswordFailure) as wire_error:
            decode_admin_password_reply(
                request,
                wire,
                expected_principal=principal(role="super_admin"),
                expected_temp_password="GLwLvE4ri9v0V!#y",
            )
        assert wire_error.value.code == code
        assert wire_error.value.status_code is None
        assert wire_error.value.detail is None

        native = {
            **wire,
            "schema": NATIVE_ADMIN_PASSWORD_PROTOCOL,
        }
        with pytest.raises(AdminPasswordFailure) as native_error:
            decode_native_admin_password_reply(command, native)
        assert native_error.value.code == code
        assert native_error.value.status_code is None
        assert native_error.value.detail is None


def test_manager_required_failure_is_valid_after_admission_role_goes_stale():
    request = admin_password_request()
    admitted = principal(role="super_admin")
    command = native_admin_password_command(request, actor=admitted, key_hash=None)
    failure = admin_password_failure_reply("manager_required")
    with pytest.raises(AdminPasswordFailure) as native_error:
        decode_native_admin_password_reply(
            command, {**failure, "schema": NATIVE_ADMIN_PASSWORD_PROTOCOL}
        )
    assert native_error.value.code == "manager_required"
    with pytest.raises(AdminPasswordFailure) as wire_error:
        decode_admin_password_reply(request, failure, expected_principal=admitted)
    assert wire_error.value.code == "manager_required"


def test_success_is_bound_to_actor_target_operation_and_derived_password():
    request = admin_password_request()
    actor = principal(role="super_admin")
    command = native_admin_password_command(request, actor=actor, key_hash=None)
    native_data = {
        key: value
        for key, value in admin_password_data().items()
        if key != "temp_password"
    }
    native_data["password_admin_reset_at"] = datetime(
        2026, 10, 20, tzinfo=UTC
    ).isoformat()
    native = native_admin_password_success(command, native_data)
    assert decode_native_admin_password_reply(command, native).data.target_user_id == (
        request.payload.target_user_id
    )
    current_manager = actor.model_copy(update={"role": "project_admin"})
    current = native_admin_password_success(
        command, native_data, current_principal=current_manager
    )
    assert decode_native_admin_password_reply(command, current).principal.role == (
        "project_admin"
    )

    wire = wire_admin_password_success(
        request, admin_password_data(), current_principal=actor
    )
    assert (
        decode_admin_password_reply(
            request,
            wire,
            expected_principal=actor,
        ).protocol
        == ADMIN_PASSWORD_PROTOCOL
    )

    wire["data"]["temp_password"] = "wrong-password-1!"
    with pytest.raises(IdentityUnavailable):
        decode_admin_password_reply(
            request,
            wire,
            expected_principal=actor,
            expected_temp_password="GLwLvE4ri9v0V!#y",
        )


def test_request_and_command_reprs_hide_bearer_and_password_material():
    token = "private-request-bearer-material"
    request = admin_password_request(token=token)
    command = native_admin_password_command(
        request, actor=principal(role="super_admin"), key_hash=None
    )
    assert token not in repr(request)
    assert token not in repr(command)
    assert "new_password_hash" not in repr(command)


def test_native_password_hash_requires_cost_twelve_and_exact_command_shape():
    request = admin_password_request()
    actor = principal(role="super_admin")
    command = native_admin_password_command(request, actor=actor, key_hash=None)
    raw = command.model_dump(mode="json", by_alias=True)
    assert set(raw) == {
        "schema",
        "operation",
        "operation_id",
        "actor",
        "expected_key_hash",
        "request_fingerprint",
        "derivation_id",
        "audit_context",
        "payload",
        "new_password_hash",
    }
    raw["new_password_hash"] = "$2b$10$" + ("." * 53)
    with pytest.raises(IdentityUnavailable):
        from aap_identity.admin_password_contract import (
            decode_native_admin_password_command,
        )

        decode_native_admin_password_command(raw)


def test_api_key_hash_is_opaque_and_bound_only_to_api_key_principals():
    request = admin_password_request()
    actor = principal(
        credential_type="api_key",
        role="project_admin",
        generation=None,
        scopes=["users:reset"],
    )
    command = native_admin_password_command(
        request, actor=actor, key_hash="legacy opaque stored hash"
    )
    assert command.expected_key_hash == "legacy opaque stored hash"
    with pytest.raises(IdentityUnavailable):
        native_admin_password_command(request, actor=actor, key_hash=None)


def test_failure_status_and_detail_are_closed():
    request = admin_password_request()
    reply = admin_password_failure_reply("manager_required")
    reply["error"]["detail"] = "database refused"
    with pytest.raises(IdentityUnavailable):
        decode_admin_password_reply(
            request,
            reply,
            expected_principal=principal(role="super_admin"),
            expected_temp_password="GLwLvE4ri9v0V!#y",
        )

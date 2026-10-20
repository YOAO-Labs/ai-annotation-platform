from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.directory_contract import (
    DIRECTORY_PROTOCOL,
    DirectoryFailure,
    decode_directory_reply,
    decode_directory_request,
    decode_native_directory_command,
    decode_native_directory_reply,
    directory_failure_reply,
)
from aap_identity.directory_manager import directory_request_fingerprint
from directory_helpers import (
    DIRECTORY_SECRET,
    directory_export_user,
    directory_request,
    directory_user,
    native_command,
    native_success,
    wire_success,
)
from session_helpers import audit_context, principal


def test_request_defaults_page_bounds_and_unrestricted_role_values():
    unusual_role = "legacy-role-" + ("x" * 4096)
    request = directory_request("list_users", payload={"role": unusual_role})
    assert request.payload.role == unusual_role
    assert request.payload.status == "active"
    assert request.payload.project_id is None

    query = directory_request(
        "query_users",
        payload={"page": 2**63 - 1, "page_size": 500, "search": "\u00a0x\u2003"},
    )
    assert query.payload.page == 2**63 - 1
    assert query.payload.status == "active"
    assert query.payload.search == "\u00a0x\u2003"

    with pytest.raises(ValidationError):
        directory_request("query_users", payload={"page": 2**63})
    with pytest.raises(ValidationError):
        directory_request("query_users", payload={"page": True})
    with pytest.raises(ValidationError):
        directory_request("query_users", payload={"page_size": 501})


def test_wire_is_closed_duplicate_safe_and_hides_bearer_from_native_command():
    token = "private-directory-bearer"
    request = directory_request("list_users", token=token)
    assert token not in repr(request)
    actor = principal(role="project_admin")
    command = native_command(request, actor=actor)
    encoded = json.dumps(command.model_dump(mode="json", by_alias=True))
    assert token not in encoded
    assert set(command.model_dump(mode="json", by_alias=True)) == {
        "schema",
        "operation",
        "operation_id",
        "actor",
        "expected_key_hash",
        "request_fingerprint",
        "derivation_id",
        "audit_context",
        "payload",
    }
    assert command.request_fingerprint == directory_request_fingerprint(
        DIRECTORY_SECRET, request
    )
    assert (
        decode_native_directory_command(command.model_dump(mode="json", by_alias=True))
        == command
    )

    body = json.dumps(
        {
            "schema": DIRECTORY_PROTOCOL,
            "operation": "list_users",
            "operation_id": str(uuid.uuid4()),
            "token": token,
            "audit_context": audit_context(),
            "payload": {},
        }
    ).replace('"schema":', '"schema":"bad", "schema":', 1)
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        decode_directory_request(body.encode())
    with pytest.raises(ValueError, match="declared wire field schema"):
        decode_directory_request(
            {
                "schema": DIRECTORY_PROTOCOL,
                "protocol": DIRECTORY_PROTOCOL,
                "operation": "list_users",
                "operation_id": str(uuid.uuid4()),
                "token": token,
                "audit_context": audit_context(),
                "payload": {},
            }
        )


def test_full_unicode_search_normalization_is_fingerprinted_semantically():
    operation_id = uuid.uuid4()
    first = directory_request(
        "query_users",
        operation_id=operation_id,
        payload={"search": "\u00a0 Name_% \u2003"},
        audit=audit_context(path="/first"),
    )
    second = directory_request(
        "query_users",
        operation_id=operation_id,
        payload={"search": "Name_%"},
        audit=audit_context(path="/second"),
    )
    assert directory_request_fingerprint(DIRECTORY_SECRET, first) == (
        directory_request_fingerprint(DIRECTORY_SECRET, second)
    )


def test_list_and_export_preserve_historical_strings_and_opaque_preferences():
    request = directory_request("list_users")
    preferences = {
        "new_client_field": {
            "token": "opaque-token-label",
            "password": "opaque-password-label",
            "large_int": 9_007_199_254_740_993,
            "nested": [True, None, 0.125],
        }
    }
    user = directory_user(preferences=preferences)
    actor = principal(role="super_admin")
    reply = decode_directory_reply(
        request,
        wire_success(request, {"users": [user]}, current_principal=actor),
        expected_principal=actor,
    )
    returned = reply.data.users[0]
    assert returned.role == "retired_global_role"
    assert returned.status == "historical_status"
    assert returned.preferences == preferences
    assert (
        returned.preferences["new_client_field"]["large_int"] == 9_007_199_254_740_993
    )

    export_request = directory_request("export_users")
    export = decode_directory_reply(
        export_request,
        wire_success(
            export_request,
            {
                "users": [directory_export_user()],
                "exported_by": "person@example.test",
                "exported_at": "2026-10-20T00:00:00Z",
                "request_id": export_request.audit_context.request_id,
            },
            current_principal=principal(role="super_admin"),
        ),
    )
    assert export.data.users[0].role == "retired_global_role"
    assert export.data.users[0].status == "historical_status"


def test_query_pagination_and_scope_flags_are_closed_and_bound():
    actor = principal(role="project_admin")
    request = directory_request(
        "query_users", payload={"page": 1, "page_size": 1, "status": "active"}
    )
    item = directory_user(role="employee", is_managed=True, is_lifecycle_managed=True)
    data = {"items": [item], "total": 7, "page": 1, "page_size": 1, "pages": 7}
    reply = decode_directory_reply(
        request,
        wire_success(request, data, current_principal=actor),
        expected_principal=actor,
    )
    assert reply.data.pages == 7

    wrong_echo = {**data, "page": 2}
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            request, wire_success(request, wrong_echo, current_principal=actor)
        )
    underfilled = {**data, "items": []}
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            request, wire_success(request, underfilled, current_principal=actor)
        )
    impossible_flags = {
        **data,
        "items": [
            directory_user(
                role="employee",
                is_managed=False,
                is_lifecycle_managed=True,
            )
        ],
    }
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            request, wire_success(request, impossible_flags, current_principal=actor)
        )
    malformed_count = {**data, "total": True}
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            request, wire_success(request, malformed_count, current_principal=actor)
        )


def test_query_and_stats_reject_inconsistent_counts_and_unordered_rows():
    actor = principal(role="super_admin")
    request = directory_request("user_stats")
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            request,
            wire_success(
                request,
                {"total": 1, "online": 2, "weekly_active": 0},
                current_principal=actor,
            ),
        )

    query = directory_request("query_users", payload={"page_size": 2})
    rows = [
        directory_user(
            id=uuid.UUID(int=1), created_at=datetime(2026, 10, 20, tzinfo=UTC)
        ),
        directory_user(
            id=uuid.UUID(int=2), created_at=datetime(2026, 10, 20, tzinfo=UTC)
        ),
    ]
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            query,
            wire_success(
                query,
                {"items": rows, "total": 2, "page": 1, "page_size": 2, "pages": 1},
                current_principal=actor,
            ),
        )


def test_failure_shapes_and_invalid_role_details_are_request_bound():
    request = directory_request("list_users", payload={"role": "legacy-role"})
    failure = directory_failure_reply(
        DirectoryFailure("invalid_platform_role", 400, "非法平台角色: legacy-role")
    )
    with pytest.raises(DirectoryFailure) as raised:
        decode_directory_reply(request, failure)
    assert raised.value.code == "invalid_platform_role"
    assert raised.value.status_code == 400

    wrong_detail = {
        "schema": DIRECTORY_PROTOCOL,
        "ok": False,
        "error": {
            "code": "invalid_platform_role",
            "status_code": 400,
            "detail": "非法平台角色: other",
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(request, wrong_detail)

    private_role = "private-directory-bearer"
    secret_request = directory_request(
        "list_users", token=private_role, payload={"role": private_role}
    )
    secret_error = directory_failure_reply(
        DirectoryFailure("invalid_platform_role", 400, f"非法平台角色: {private_role}")
    )
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(secret_request, secret_error)

    wrong_manager_detail = {
        "schema": DIRECTORY_PROTOCOL,
        "ok": False,
        "error": {
            "code": "manager_required",
            "status_code": 403,
            "detail": "wrong detail",
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(directory_request("query_users"), wrong_manager_detail)

    manager_required = {
        "schema": DIRECTORY_PROTOCOL,
        "ok": False,
        "error": {
            "code": "manager_required",
            "status_code": 403,
            "detail": "需要角色权限: super_admin或project_admin",
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(
            directory_request("query_users"),
            manager_required,
            expected_principal=principal(role="super_admin"),
        )


def test_export_receipt_replay_allows_frozen_selection_and_metadata_but_fresh_does_not():
    group_id = uuid.uuid4()
    audit_id = "original-request-id"
    request = directory_request(
        "export_users",
        payload={
            "status": "active",
            "role": "employee",
            "group_id": str(group_id),
            "search": "Original",
            "format": "json",
        },
        audit=audit_context(request_id="retry-request-id"),
    )
    command = native_command(request)
    old_row = directory_export_user(
        role="viewer",
        status="offline",
        group_id=str(uuid.uuid4()),
        name="Changed profile",
    )
    frozen = {
        "users": [old_row],
        "exported_by": "original-actor@example.test",
        "exported_at": "2026-10-19T10:00:00Z",
        "request_id": audit_id,
    }
    current_principal = command.actor.model_copy(
        update={"email": "renamed@example.test", "name": "Renamed"}
    )
    replay = native_success(
        command, frozen, replayed=True, current_principal=current_principal
    )
    native = decode_native_directory_reply(command, replay)
    wire = wire_success(
        request,
        native.data.model_dump(mode="json"),
        current_principal=current_principal,
    )
    decoded = decode_directory_reply(
        request,
        wire,
        expected_principal=command.actor,
        replayed=native.replayed,
    )
    assert decoded.data.exported_by == "original-actor@example.test"
    assert decoded.data.request_id == audit_id

    remote_wire = decode_directory_reply(
        request, wire, expected_principal=command.actor
    )
    assert remote_wire.data.users[0].role == "viewer"

    fresh_data = {
        **frozen,
        "users": [
            directory_export_user(
                role="employee",
                status="online",
                group_id=str(group_id),
                name="Original profile",
            )
        ],
    }
    fresh = native_success(
        command, fresh_data, replayed=False, current_principal=current_principal
    )
    with pytest.raises(IdentityUnavailable):
        decode_native_directory_reply(command, fresh)


def test_export_operation_conflict_is_only_valid_for_export():
    conflict = directory_failure_reply(DirectoryFailure("operation_conflict", 409))
    with pytest.raises(DirectoryFailure):
        decode_directory_reply(directory_request("export_users"), conflict)
    with pytest.raises(IdentityUnavailable):
        decode_directory_reply(directory_request("list_users"), conflict)

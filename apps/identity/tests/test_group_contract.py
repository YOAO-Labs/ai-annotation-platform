from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.group_contract import (
    GROUP_PROTOCOL,
    NATIVE_GROUP_PROTOCOL,
    GroupFailure,
    decode_group_reply,
    decode_group_request,
    decode_native_group_command,
    decode_native_group_reply,
    native_group_command,
)
from aap_identity.group_manager import group_request_fingerprint
from group_helpers import (
    assigned_account,
    group_record,
    group_request,
    group_success,
    native_success,
)
from session_helpers import ACTOR_ID, audit_context, principal


def native_command(request, *, actor=None, key_hash=None):
    return native_group_command(
        request,
        actor=actor or principal(role="super_admin"),
        expected_key_hash=key_hash,
        request_fingerprint=group_request_fingerprint("secret", request),
        derivation_id="c" * 64,
    )


def test_wire_request_is_closed_and_keeps_legacy_search_unbounded():
    long_search = "组" * 100_000
    request = group_request(
        "query_groups",
        payload={"page": 1, "page_size": 500, "search": long_search},
    )
    assert request.payload.search == long_search

    raw = {
        "schema": GROUP_PROTOCOL,
        "operation": "list_groups",
        "operation_id": str(uuid.uuid4()),
        "token": "private-bearer-value",
        "audit_context": audit_context(),
        "payload": {},
        "unexpected": True,
    }
    with pytest.raises(ValidationError):
        decode_group_request(raw)
    duplicated = json.dumps(raw).replace('"schema":', '"schema":"bad", "schema":', 1)
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        decode_group_request(duplicated.encode())


def test_wire_token_is_private_and_never_crosses_native_boundary_or_fingerprint():
    token = "private-group-bearer"
    operation_id = uuid.uuid4()
    first = group_request(
        "create_group",
        operation_id=operation_id,
        token=token,
        audit=audit_context(path="/one"),
    )
    second = group_request(
        "create_group",
        operation_id=operation_id,
        token=token,
        audit=audit_context(path="/two"),
    )
    assert token not in repr(first)
    assert group_request_fingerprint("secret", first) == group_request_fingerprint(
        "secret", second
    )
    command = native_command(first)
    encoded = json.dumps(command.model_dump(mode="json", by_alias=True))
    assert token not in encoded
    assert "audit_context" in encoded
    assert command.request_fingerprint == group_request_fingerprint("secret", first)


def test_native_command_requires_key_hash_only_for_api_key_principals():
    request = group_request("list_groups")
    command = native_command(
        request,
        actor=principal(credential_type="api_key", role="super_admin"),
        key_hash="key-hash-private",
    )
    decoded = decode_native_group_command(
        command.model_dump(mode="json", by_alias=True)
    )
    assert decoded.expected_key_hash == "key-hash-private"

    with pytest.raises(IdentityUnavailable):
        decode_native_group_command(
            {
                **command.model_dump(mode="json", by_alias=True),
                "expected_key_hash": None,
            }
        )


def test_create_reply_binds_group_to_operation_and_allows_fresh_actor_metadata():
    request = group_request(
        "create_group",
        operation_id=uuid.uuid4(),
        payload={"name": " 研发 ", "description": ""},
    )
    expected = principal(role="super_admin")
    current = expected.model_copy(update={"role": "project_admin", "name": "Renamed"})
    reply = decode_group_reply(
        request,
        group_success(
            request,
            {
                "group": group_record(
                    id=request.operation_id, name="研发", description=None
                ).model_dump(mode="json")
            },
            current_principal=current,
        ),
        expected_principal=expected,
    )
    assert reply.data.group.id == request.operation_id
    assert reply.principal.name == "Renamed"

    wrong_id = group_success(
        request,
        {"group": group_record(name="研发", description=None).model_dump(mode="json")},
        current_principal=current,
    )
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, wrong_id, expected_principal=expected)


def test_native_reply_is_checked_before_commit_and_binds_operation_principal():
    request = group_request("delete_group")
    command = native_command(request)
    payload = native_success(
        command,
        {
            "group_id": str(request.payload.group_id),
            "deleted": True,
        },
        current_principal=command.actor.model_copy(
            update={"role": "project_admin", "name": "Changed metadata"}
        ),
    )
    assert decode_native_group_reply(command, payload).data.deleted is True

    payload["operation_id"] = str(uuid.uuid4())
    with pytest.raises(IdentityUnavailable):
        decode_native_group_reply(command, payload)


def test_group_deletion_flag_rejects_integer_one():
    request = group_request("delete_group")
    payload = group_success(
        request,
        {"group_id": str(request.payload.group_id), "deleted": 1},
    )
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, payload)


def test_success_principal_may_refresh_manager_role_but_not_lose_manager_authority():
    request = group_request("list_groups")
    expected = principal(role="super_admin")
    demoted = expected.model_copy(update={"role": "employee"})
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(
            request,
            group_success(request, {"items": []}, current_principal=demoted),
            expected_principal=expected,
        )

    command = native_command(request, actor=expected)
    native = native_success(command, {"items": []}, current_principal=demoted)
    with pytest.raises(IdentityUnavailable):
        decode_native_group_reply(command, native)


def test_single_assignment_reply_targets_requested_account_not_actor():
    target_id = uuid.uuid4()
    request = group_request(
        "assign_user_group",
        payload={"user_id": str(target_id), "group_id": None},
    )
    data = {"account": assigned_account(id=target_id).model_dump(mode="json")}
    reply = decode_group_reply(request, group_success(request, data))
    assert reply.data.account.id == target_id
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(
            request,
            group_success(
                request,
                {"account": assigned_account(id=ACTOR_ID).model_dump(mode="json")},
            ),
        )


def test_preview_preserves_duplicate_order_but_never_exposes_out_of_scope_profile():
    first = uuid.uuid4()
    request = group_request(
        "preview_group_assignments",
        payload={"user_ids": [str(first), str(first)], "group_id": None},
    )
    items = [
        {
            "user_id": str(first),
            "email": None,
            "name": None,
            "ok": False,
            "current_group_id": None,
            "current_group_name": None,
            "next_group_id": None,
            "next_group_name": None,
            "error": "用户不存在或不在管理范围内",
        },
        {
            "user_id": str(first),
            "email": None,
            "name": None,
            "ok": False,
            "current_group_id": None,
            "current_group_name": None,
            "next_group_id": None,
            "next_group_name": None,
            "error": "用户不存在或不在管理范围内",
        },
    ]
    data = {
        "group_id": None,
        "group_name": None,
        "items": items,
        "applicable": 0,
        "blocked": 2,
    }
    assert decode_group_reply(request, group_success(request, data)).data.blocked == 2

    items[0]["email"] = "private@example.test"
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, group_success(request, data))


def test_preview_missing_group_preserves_legacy_null_group_projection():
    target_id = uuid.uuid4()
    group_id = uuid.uuid4()
    request = group_request(
        "preview_group_assignments",
        payload={"user_ids": [str(target_id)], "group_id": str(group_id)},
    )
    item = {
        "user_id": str(target_id),
        "email": "target@example.test",
        "name": "Target user",
        "ok": False,
        "current_group_id": None,
        "current_group_name": None,
        "next_group_id": None,
        "next_group_name": None,
        "error": "数据组不存在",
    }
    data = {
        "group_id": str(group_id),
        "group_name": None,
        "items": [item],
        "applicable": 0,
        "blocked": 1,
    }
    assert decode_group_reply(request, group_success(request, data)).data.blocked == 1

    item["next_group_id"] = str(group_id)
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, group_success(request, data))


def test_apply_reply_preserves_duplicates_and_only_safe_per_item_errors():
    target_id = uuid.uuid4()
    request = group_request(
        "apply_group_assignments",
        payload={"user_ids": [str(target_id), str(target_id)], "group_id": None},
    )
    data = {
        "items": [
            {"user_id": str(target_id), "ok": True, "retryable": False, "error": None},
            {
                "user_id": str(target_id),
                "ok": False,
                "retryable": True,
                "error": "account_busy",
            },
        ],
        "succeeded": 1,
        "failed": 1,
    }
    assert decode_group_reply(request, group_success(request, data)).data.failed == 1

    data["items"][1]["error"] = "database password leaked"
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, group_success(request, data))


def test_policy_errors_are_operation_bound_and_cannot_echo_bearer():
    token = "group-name-is-the-bearer"
    request = group_request(
        "create_group",
        token=token,
        payload={"name": token, "description": None},
    )
    error = {
        "schema": GROUP_PROTOCOL,
        "ok": False,
        "error": {
            "code": "policy_rejected",
            "status_code": 409,
            "detail": f"已存在同名数据组: {token}",
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, error)

    safe = {
        "schema": GROUP_PROTOCOL,
        "ok": False,
        "error": {
            "code": "policy_rejected",
            "status_code": 409,
            "detail": "已存在同名数据组: Existing",
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, safe)

    valid_request = group_request(
        "create_group", payload={"name": "Existing", "description": None}
    )
    with pytest.raises(GroupFailure) as failure:
        decode_group_reply(valid_request, safe)
    assert failure.value.status_code == 409


def test_query_pages_and_group_timestamps_are_strict():
    request = group_request(
        "query_groups", payload={"page": 2, "page_size": 2, "search": None}
    )
    item = group_record().model_dump(mode="json")
    data = {
        "items": [item],
        "total": 3,
        "page": 2,
        "page_size": 2,
        "pages": 2,
    }
    assert decode_group_reply(request, group_success(request, data)).data.pages == 2
    data["pages"] = 3
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, group_success(request, data))

    item["member_count"] = True
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, group_success(request, {**data, "pages": 2}))

    item["member_count"] = 0
    item["created_at"] = datetime.now().isoformat()
    with pytest.raises(IdentityUnavailable):
        decode_group_reply(request, group_success(request, {**data, "pages": 2}))


def test_native_failure_requires_exact_closed_fields_and_known_operation():
    request = group_request("delete_group")
    command = native_command(request)
    failure = {
        "schema": NATIVE_GROUP_PROTOCOL,
        "ok": False,
        "error": {"code": "group_busy", "status_code": None, "detail": None},
    }
    with pytest.raises(GroupFailure) as caught:
        decode_native_group_reply(command, failure)
    assert caught.value.code == "group_busy"

    failure["error"]["detail"] = "SQL exception details"
    with pytest.raises(IdentityUnavailable):
        decode_native_group_reply(command, failure)


@pytest.mark.parametrize(
    "operation",
    [
        "create_group",
        "update_group",
        "delete_group",
        "assign_user_group",
        "apply_group_assignments",
    ],
)
def test_group_busy_is_valid_for_write_scope_and_advisory_locks(operation):
    request = group_request(operation)
    command = native_command(request)
    failure = {
        "schema": NATIVE_GROUP_PROTOCOL,
        "ok": False,
        "error": {"code": "group_busy", "status_code": None, "detail": None},
    }
    with pytest.raises(GroupFailure) as caught:
        decode_native_group_reply(command, failure)
    assert caught.value.code == "group_busy"


def test_group_busy_is_not_valid_for_read_or_preview_commands():
    request = group_request("preview_group_assignments")
    command = native_command(request)
    failure = {
        "schema": NATIVE_GROUP_PROTOCOL,
        "ok": False,
        "error": {"code": "group_busy", "status_code": None, "detail": None},
    }
    with pytest.raises(IdentityUnavailable):
        decode_native_group_reply(command, failure)

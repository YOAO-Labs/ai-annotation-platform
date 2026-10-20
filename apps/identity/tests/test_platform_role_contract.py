from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.platform_role_contract import (
    PlatformRoleFailure,
    PlatformRolePreviewData,
    decode_labeling_platform_role_facts,
    decode_native_platform_role_command,
    decode_native_platform_role_reply,
    decode_platform_role_reply,
    decode_platform_role_request,
    decode_workspace_platform_role_facts,
    platform_role_failure_reply,
    validate_platform_role_fact_coverage,
)
from aap_identity.platform_role_manager import (
    platform_role_derivation_id,
    platform_role_request_fingerprint,
)
from platform_role_helpers import (
    PLATFORM_ROLE_SECRET,
    PROJECT_ID,
    TARGET_ID,
    labeling_facts,
    native_command,
    native_success,
    platform_role_request,
    role_preview_data,
    role_project,
    role_user,
    wire_success,
    workspace_facts,
)
from session_helpers import ACTOR_ID, principal


def test_request_fingerprint_uses_exact_role_and_ignores_credential_and_audit():
    request = platform_role_request("change_platform_role", role="employee")
    other = platform_role_request(
        "change_platform_role",
        role="employee",
        token="different-bearer",
        audit={
            "method": "PATCH",
            "path": "/different/audit/path",
            "ip": None,
            "request_id": "different-request",
        },
    )
    assert platform_role_request_fingerprint(PLATFORM_ROLE_SECRET, request) == (
        platform_role_request_fingerprint(PLATFORM_ROLE_SECRET, other)
    )
    changed_role = platform_role_request("change_platform_role", role=" employee")
    assert platform_role_request_fingerprint(PLATFORM_ROLE_SECRET, request) != (
        platform_role_request_fingerprint(PLATFORM_ROLE_SECRET, changed_role)
    )
    assert len(platform_role_derivation_id(PLATFORM_ROLE_SECRET)) == 64

    vector = json.loads(
        (Path(__file__).parent / "fixtures/platform_role_vector.json").read_text()
    )
    vector_request = platform_role_request(
        "change_platform_role",
        role=vector["role"],
        operation_id=uuid.UUID(vector["operation_id"]),
        target_user_id=uuid.UUID(vector["target_user_id"]),
    )
    vector_canonical = json.dumps(
        {
            "operation": vector_request.operation,
            "payload": vector_request.payload.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    assert vector_canonical == vector["canonical_request"]
    assert (
        platform_role_request_fingerprint(vector["signing_secret"], vector_request)
        == vector["request_fingerprint"]
    )
    assert (
        platform_role_derivation_id(vector["signing_secret"]) == vector["derivation_id"]
    )


def test_wire_requests_accept_arbitrary_role_text_without_short_length_cap():
    role = "x" * 10000
    request = platform_role_request("preview_role_change", role=role)
    assert request.payload.role == role
    assert "platform-role-test-bearer" not in repr(request)
    with pytest.raises(Exception):
        decode_platform_role_request(
            {
                "schema": "aap-identity-platform-roles/v1",
                "operation": "preview_role_change",
                "operation_id": str(uuid.uuid4()),
                "token": "t",
                "audit_context": {
                    "method": "GET",
                    "path": "/api/v1/users/role/preview",
                    "ip": None,
                    "request_id": None,
                },
                "payload": {"target_user_id": str(TARGET_ID), "role": 3},
            }
        )


def test_native_command_has_exact_nine_fields_and_hides_credentials():
    request = platform_role_request(
        "change_platform_role", token="sensitive-platform-role-bearer"
    )
    command = native_command(request)
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
    }
    assert "sensitive-platform-role-bearer" not in repr(command)
    assert "sensitive-platform-role-bearer" not in json.dumps(raw)
    assert decode_native_platform_role_command(raw) == command


def test_preview_dto_aggregates_only_project_rows_and_preserves_warnings():
    request = platform_role_request("preview_role_change")
    actor = principal(role="super_admin")
    first = role_project(project_name="A project", project_id=PROJECT_ID)
    second_id = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    second = role_project(
        project_id=second_id,
        project_name="Z project",
        annotator_batch_count=0,
        reviewer_batch_count=0,
        assigned_task_count=2,
        review_task_count=0,
    )
    data = role_preview_data(
        projects=[first, second],
        assigned_batch_count=3,
        assigned_task_count=5,
        review_task_count=4,
    )
    reply = decode_platform_role_reply(
        request,
        wire_success(request, data, current_principal=actor),
        expected_principal=actor,
    )
    assert isinstance(reply.data, PlatformRolePreviewData)
    assert reply.data.assigned_batch_count == 3
    assert reply.data.assigned_task_count == 5
    assert len(reply.data.warnings) == 2

    for bad in (
        {**data, "other_project_count": 1},
        {**data, "warnings": ["unfrozen warning"]},
        {**data, "assigned_batch_count": 4},
        {**data, "can_change": 1},
        {**data, "projects": [first, first]},
    ):
        with pytest.raises(IdentityUnavailable):
            decode_platform_role_reply(
                request,
                wire_success(request, bad, current_principal=actor),
                expected_principal=actor,
            )


def test_preview_blocker_order_binds_self_same_role_last_sa_then_work():
    actor = principal(role="super_admin")
    self_request = platform_role_request(
        "preview_role_change", role="super_admin", target_user_id=ACTOR_ID
    )
    preview = role_preview_data(
        user_id=ACTOR_ID,
        current_role="super_admin",
        requested_role="super_admin",
        can_change=False,
        blockers=["不能修改自己的角色", "目标角色与当前角色相同"],
    )
    decode_platform_role_reply(
        self_request,
        wire_success(self_request, preview, current_principal=actor),
        expected_principal=actor,
    )

    changed = platform_role_request("preview_role_change", role="viewer")
    blocked = role_preview_data(
        current_role="super_admin",
        requested_role="viewer",
        can_change=False,
        blockers=[
            "不能降级最后一名超级管理员",
            "账号仍持有标注/质检项目职责，请先在项目中变更职责",
            "账号仍是项目负责人，请先转移项目后变更平台角色",
            "账号仍有未完成的标注/质检工作，请先交接",
        ],
    )
    decode_platform_role_reply(
        changed,
        wire_success(changed, blocked, current_principal=actor),
        expected_principal=actor,
    )
    blocked["blockers"] = list(reversed(blocked["blockers"]))
    with pytest.raises(IdentityUnavailable):
        decode_platform_role_reply(
            changed,
            wire_success(changed, blocked, current_principal=actor),
            expected_principal=actor,
        )


def test_mutation_user_projection_has_exact_21_fields_and_request_role():
    request = platform_role_request("change_platform_role", role="employee")
    actor = principal(role="super_admin")
    data = {"user": role_user()}
    reply = decode_platform_role_reply(
        request,
        wire_success(request, data, current_principal=actor),
        expected_principal=actor,
    )
    assert reply.data.user.status == "historical_status"
    assert reply.data.user.preferences == {"opaque": {"numeric": 9007199254740993}}
    assert set(reply.data.user.model_dump()) == {
        "id",
        "email",
        "name",
        "role",
        "group_name",
        "group_id",
        "status",
        "is_active",
        "disabled_kind",
        "disabled_at",
        "disabled_by",
        "disabled_reason",
        "email_verified_at",
        "last_login_at",
        "last_seen_at",
        "password_admin_reset_at",
        "deactivation_requested_at",
        "deactivation_scheduled_at",
        "preferences",
        "avatar_ref",
        "created_at",
    }
    with pytest.raises(IdentityUnavailable):
        decode_platform_role_reply(
            request,
            wire_success(
                request,
                {"user": role_user(role="viewer")},
                current_principal=actor,
            ),
            expected_principal=actor,
        )


def test_native_replay_returns_original_accepted_role_snapshot():
    request = platform_role_request("change_platform_role", role="employee")
    actor = principal(role="super_admin")
    command = native_command(request, actor=actor)
    reply = native_success(
        command,
        {"user": role_user(role="employee")},
        replayed=True,
        current_principal=actor,
    )
    accepted = decode_native_platform_role_reply(command, reply)
    assert accepted.replayed is True
    assert accepted.data.user.role == "employee"


def test_native_preview_cannot_claim_receipt_replay_and_stale_actor_fails_closed():
    request = platform_role_request("preview_role_change")
    actor = principal(role="super_admin")
    command = native_command(request, actor=actor)
    raw = native_success(command, role_preview_data(), replayed=True)
    with pytest.raises(IdentityUnavailable):
        decode_native_platform_role_reply(command, raw)
    raw = native_success(
        command,
        role_preview_data(),
        replayed=False,
        current_principal=actor.model_copy(update={"role": "employee"}),
    )
    with pytest.raises(IdentityUnavailable):
        decode_native_platform_role_reply(command, raw)


def test_closed_request_bound_failures_and_stale_super_admin_admission():
    admitted = principal(role="super_admin")
    invalid = platform_role_request("change_platform_role", role=" employee")
    invalid_failure = PlatformRoleFailure(
        "invalid_platform_role", 400, "非法平台角色:  employee"
    )
    with pytest.raises(PlatformRoleFailure) as raised:
        decode_platform_role_reply(
            invalid,
            platform_role_failure_reply(invalid_failure),
            expected_principal=admitted,
        )
    assert raised.value.code == "invalid_platform_role"

    valid = platform_role_request("change_platform_role")
    lost_manager = PlatformRoleFailure(
        "super_admin_required", 403, "当前账号已无管理权限"
    )
    with pytest.raises(PlatformRoleFailure) as stale:
        decode_platform_role_reply(
            valid,
            platform_role_failure_reply(lost_manager),
            expected_principal=admitted,
        )
    assert stale.value.code == "super_admin_required"

    self_request = platform_role_request(
        "change_platform_role", target_user_id=ACTOR_ID
    )
    with pytest.raises(PlatformRoleFailure):
        decode_platform_role_reply(
            self_request,
            platform_role_failure_reply(
                PlatformRoleFailure("self_change", 400, "不能修改自己的角色")
            ),
            expected_principal=admitted,
        )


def test_invalid_role_error_cannot_echo_the_input_bearer():
    token = "private-token-embedded-in-role"
    request = platform_role_request("preview_role_change", role=token, token=token)
    response = platform_role_failure_reply(
        PlatformRoleFailure("invalid_platform_role", 400, f"非法平台角色: {token}")
    )
    with pytest.raises(IdentityUnavailable):
        decode_platform_role_reply(
            request, response, expected_principal=principal(role="super_admin")
        )


def test_structured_project_busy_and_work_blocker_failures_are_closed():
    actor = principal(role="super_admin")
    request = platform_role_request("change_platform_role", role="viewer")
    project_busy = PlatformRoleFailure("project_busy", 409, {"reason": "project_busy"})
    assert platform_role_failure_reply(project_busy)["error"]["detail"] == {
        "reason": "project_busy"
    }
    with pytest.raises(PlatformRoleFailure) as busy:
        decode_platform_role_reply(
            request,
            platform_role_failure_reply(project_busy),
            expected_principal=actor,
        )
    assert busy.value.detail == {"reason": "project_busy"}

    blockers = [
        "账号仍持有标注/质检项目职责，请先在项目中变更职责",
        "账号仍有未完成的标注/质检工作，请先交接",
    ]
    blocked = PlatformRoleFailure(
        "platform_role_change_blocked",
        409,
        {"reason": "platform_role_change_blocked", "blockers": blockers},
    )
    with pytest.raises(PlatformRoleFailure) as change_error:
        decode_platform_role_reply(
            request,
            platform_role_failure_reply(blocked),
            expected_principal=actor,
        )
    assert change_error.value.detail == {
        "reason": "platform_role_change_blocked",
        "blockers": blockers,
    }

    preview = platform_role_request("preview_role_change", role="viewer")
    with pytest.raises(IdentityUnavailable):
        decode_platform_role_reply(
            preview,
            platform_role_failure_reply(blocked),
            expected_principal=actor,
        )


def test_workspace_and_labeling_support_facts_are_closed_and_project_aligned():
    workspace = decode_workspace_platform_role_facts(
        workspace_facts(), target_user_id=TARGET_ID
    )
    labeling = decode_labeling_platform_role_facts(
        labeling_facts(), target_user_id=TARGET_ID
    )
    validate_platform_role_fact_coverage(workspace, labeling)
    assert workspace.non_viewer_membership_count == 1
    assert labeling.unfinished_effective_work_count == 5

    wrong_target = workspace_facts(target_user_id=str(uuid.uuid4()))
    with pytest.raises(IdentityUnavailable):
        decode_workspace_platform_role_facts(wrong_target, target_user_id=TARGET_ID)
    wrong_project = labeling_facts(
        membership_projects=[
            {
                "project_id": str(uuid.uuid4()),
                "annotator_batch_count": 0,
                "reviewer_batch_count": 0,
                "assigned_task_count": 0,
                "review_task_count": 0,
            }
        ]
    )
    with pytest.raises(IdentityUnavailable):
        validate_platform_role_fact_coverage(
            workspace,
            decode_labeling_platform_role_facts(
                wrong_project, target_user_id=TARGET_ID
            ),
        )
    other_id = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    second_workspace = workspace_facts(
        membership_projects=[
            workspace_facts()["membership_projects"][0],
            {
                "project_id": str(other_id),
                "project_name": "Second project",
                "membership_role": "reviewer",
            },
        ],
        non_viewer_membership_count=2,
    )
    reordered_labeling = labeling_facts(
        membership_projects=[
            {
                "project_id": str(other_id),
                "annotator_batch_count": 0,
                "reviewer_batch_count": 0,
                "assigned_task_count": 0,
                "review_task_count": 0,
            },
            *labeling_facts()["membership_projects"],
        ]
    )
    with pytest.raises(IdentityUnavailable):
        validate_platform_role_fact_coverage(
            decode_workspace_platform_role_facts(
                second_workspace, target_user_id=TARGET_ID
            ),
            decode_labeling_platform_role_facts(
                reordered_labeling, target_user_id=TARGET_ID
            ),
        )
    with pytest.raises(IdentityUnavailable):
        decode_workspace_platform_role_facts(
            workspace_facts(owned_project_count=True), target_user_id=TARGET_ID
        )

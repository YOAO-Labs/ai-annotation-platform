from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_identity.contract import IdentityUnavailable
from aap_identity.offboarding_contract import (
    NativeOffboardingApplyCommand,
    OffboardingFailure,
    OffboardingSnapshotFacts,
    decode_native_offboarding_command,
    decode_native_offboarding_reply,
    decode_offboarding_request,
    offboarding_failure_reply,
)
from aap_identity.offboarding_manager import (
    legacy_offboarding_preview_version,
    legacy_offboarding_resource_state,
)
from offboarding_helpers import (
    NATIVE_OFFBOARDING_PROTOCOL,
    offboarding_facts,
    offboarding_request,
)
from session_helpers import principal

FIXTURES = Path(__file__).parent / "fixtures"


def _from_iso(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _facts_from_legacy_sample(sample) -> OffboardingSnapshotFacts:
    state = json.loads(sample["canonical"])
    preview = sample["preview"]
    preview_projects = {str(item["project_id"]): item for item in preview["projects"]}
    project_names = {
        key: item["project_name"] for key, item in preview_projects.items()
    }
    batch_names = {}
    for project in preview["projects"]:
        for role in ("annotator", "reviewer"):
            for batch in project["roles"][role]["batches"]:
                batch_names[str(batch["batch_id"])] = batch["batch_name"]

    candidate_users = {}
    candidate_memberships = {}
    for project_id, roles in state["receivers"].items():
        for role in ("owner", "annotator", "reviewer"):
            for option in roles[role]:
                candidate_users[option["id"]] = {
                    "id": option["id"],
                    "name": option["name"],
                    "email": option["email"],
                    "role": option["role"],
                }
                if role in {"annotator", "reviewer"}:
                    candidate_memberships[(project_id, option["id"], role)] = {
                        "project_id": project_id,
                        "user_id": option["id"],
                        "role": role,
                    }

    key_previews = {str(item["id"]): item for item in preview["api_keys"]}
    values = {
        "target": preview["user"],
        "projects": [
            {
                "id": item["id"],
                "name": project_names[item["id"]],
                "owner_id": item["owner_id"],
            }
            for item in state["projects"]
        ],
        "members": state["members"],
        "batches": [
            {**item, "name": batch_names[item["id"]]} for item in state["batches"]
        ],
        "tasks": state["tasks"],
        "locks": [
            {**item, "expire_at": _from_iso(item["expire_at"]).isoformat()}
            for item in state["locks"]
        ],
        "api_keys": [
            {
                "id": item["id"],
                "name": item["name"],
                "key_prefix": item["prefix"],
                "last_used_at": (
                    _from_iso(key_previews[item["id"]]["last_used_at"]).isoformat()
                    if key_previews[item["id"]]["last_used_at"]
                    else None
                ),
                "revoked_at": (
                    _from_iso(item["revoked_at"]).isoformat()
                    if item["revoked_at"]
                    else None
                ),
                "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            }
            for item in state["api_keys"]
        ],
        "candidate_users": list(candidate_users.values()),
        "candidate_memberships": list(candidate_memberships.values()),
        "other_unreserved": 1,
    }
    return OffboardingSnapshotFacts.model_validate_json(
        json.dumps(values, ensure_ascii=True, allow_nan=False)
    )


def test_actual_legacy_samples_keep_exact_canonical_sha256_bytes():
    samples = json.loads((FIXTURES / "offboarding_legacy_vectors.json").read_text())[
        "samples"
    ]
    for sample in samples:
        facts = _facts_from_legacy_sample(sample)
        assert (
            json.dumps(
                legacy_offboarding_resource_state(facts), sort_keys=True, default=str
            )
            == sample["canonical"]
        )
        assert legacy_offboarding_preview_version(facts) == sample["preview_version"]


def test_request_is_closed_and_preserves_unbounded_raw_preview_version():
    raw = offboarding_request(
        "commit_offboarding", preview_version="\x00" + "raw" * 30_000
    ).model_dump(mode="json", by_alias=True)
    assert len(raw["payload"]["preview_version"]) > 65_536
    assert raw["payload"]["preview_version"].startswith("\x00")
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        decode_offboarding_request(raw)


def test_successful_native_result_requires_positive_audit_id():
    actor = principal(role="super_admin")
    request = offboarding_request("commit_offboarding")
    command_payload = {
        "schema": NATIVE_OFFBOARDING_PROTOCOL,
        "phase": "apply",
        "operation": "commit_offboarding",
        "operation_id": str(request.operation_id),
        "actor": actor.model_dump(mode="json"),
        "expected_key_hash": None,
        "request_fingerprint": "a" * 64,
        "derivation_id": "b" * 64,
        "audit_context": request.audit_context.model_dump(mode="json"),
        "payload": {
            "transaction_id": "tx-1",
            "target_user_id": str(request.payload.target_user_id),
            "expected_target_generation": 7,
            "expected_target_is_active": True,
            "mode": "handoff",
            "normalized_reason": "End of assignment",
            "reason_present": True,
            "owner_plan": [],
            "labeling_plan": [],
            "transfers": [],
            "unresolved": [],
            "scope_basis": {
                "target_role": "employee",
                "target_was_active": True,
                "project_ids": [],
            },
        },
    }
    command = decode_native_offboarding_command(command_payload)
    assert isinstance(command, NativeOffboardingApplyCommand)
    invalid = {
        "schema": NATIVE_OFFBOARDING_PROTOCOL,
        "phase": "apply",
        "ok": True,
        "operation": "commit_offboarding",
        "operation_id": str(request.operation_id),
        "principal": actor.model_dump(mode="json"),
        "data": {
            "result": {
                "user": offboarding_facts().target.model_dump(mode="json"),
                "status": "suspended",
                "mode": "handoff",
                "transfers": [],
                "unresolved": [],
                "revoked_api_key_ids": [],
                "audit_id": None,
            }
        },
        "replayed": False,
    }
    with pytest.raises(IdentityUnavailable):
        decode_native_offboarding_reply(command, invalid)


def test_native_prepare_cannot_return_facts_for_another_target():
    actor = principal(role="super_admin")
    request = offboarding_request()
    command = decode_native_offboarding_command(
        {
            "schema": NATIVE_OFFBOARDING_PROTOCOL,
            "phase": "prepare",
            "operation": request.operation,
            "operation_id": str(request.operation_id),
            "actor": actor.model_dump(mode="json"),
            "expected_key_hash": None,
            "request_fingerprint": "a" * 64,
            "derivation_id": "b" * 64,
            "audit_context": request.audit_context.model_dump(mode="json"),
            "payload": {
                "target_user_id": str(request.payload.target_user_id),
                "receiver_ids": [],
                "payload_project_ids": [],
            },
        }
    )
    wrong_facts = offboarding_facts(
        target=offboarding_facts().target.model_copy(
            update={"id": uuid.UUID("99999999-9999-4999-8999-999999999999")}
        )
    )
    reply = {
        "schema": NATIVE_OFFBOARDING_PROTOCOL,
        "phase": "prepare",
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": actor.model_dump(mode="json"),
        "data": {
            "kind": "snapshot",
            "transaction_id": "tx-1",
            "target_generation": 7,
            "target_is_active": True,
            "facts": wrong_facts.model_dump(mode="json"),
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_native_offboarding_reply(command, reply)


def test_failure_builder_keeps_closed_stale_detail_without_secret_fields():
    from aap_identity.offboarding_contract import PreviewStaleDetail

    failure = OffboardingFailure(
        "offboarding_preview_stale",
        409,
        PreviewStaleDetail(
            code="offboarding_preview_stale",
            expected_preview_version="\x00" + "x" * 70_000,
            current_preview_version="a" * 64,
            blockers=[],
        ),
    )
    payload = offboarding_failure_reply(failure)
    assert payload["error"]["status_code"] == 409
    assert payload["error"]["detail"]["expected_preview_version"].startswith("\x00")

from __future__ import annotations

import json
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aap_workspace.contract import (
    MEMBERSHIP_PROTOCOL,
    LABELING_MEMBERSHIP_WORK_PROTOCOL,
    MembershipAccountFacts,
    MembershipActorFacts,
    MembershipAuditContext,
    MembershipBatchFacts,
    MembershipFactsData,
    MembershipLabelingFacts,
    MembershipRecord,
    MembershipReceiverFacts,
    MembershipProjectFacts,
    MembershipRequest,
    MembershipTaskFacts,
    PreviewRoleChangePayload,
)
from aap_workspace.policy import (
    build_member_resource_snapshot,
    evaluate_role_change,
    snapshot_token,
)

_VECTORS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "legacy_membership_digest_vectors.json"
    ).read_text()
)
_TIMESTAMP = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _id(value):
    return uuid.UUID(str(value)) if value is not None else None


def _member(snapshot: dict) -> MembershipRecord:
    member = snapshot["member"]
    return MembershipRecord(
        id=_id(member["id"]),
        project_id=_id(snapshot["project"]["id"]),
        user_id=_id(member["user_id"]),
        role=member["role"],
        assigned_by=None,
        version=member["version"],
        assigned_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        weekly_target=None,
    )


def _request_facts(name: str, vector: dict):
    snapshot = vector["snapshot"]
    member = _member(snapshot)
    owner_id = _id(snapshot["project"]["owner_id"])
    operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"membership-vector:{name}")
    replacements = snapshot["receivers"]
    replacement_annotator_id = _id(
        replacements["annotator"]["user_id"]
        if replacements["annotator"] is not None
        else None
    )
    replacement_reviewer_id = _id(
        replacements["reviewer"]["user_id"]
        if replacements["reviewer"] is not None
        else None
    )
    request = MembershipRequest(
        schema=MEMBERSHIP_PROTOCOL,
        operation_id=operation_id,
        operation="preview_role_change",
        actor_id=owner_id,
        project_id=member.project_id,
        payload=PreviewRoleChangePayload(
            member_id=member.id,
            target_role=vector["target_role"],
            replacement_annotator_id=replacement_annotator_id,
            replacement_reviewer_id=replacement_reviewer_id,
        ),
        audit_context=MembershipAuditContext(
            method="POST",
            path="/projects/project/members/member/role/preview",
            ip=None,
            request_id=None,
        ),
    )

    account_by_id = {}
    member_account = snapshot["member_account"]
    if member_account["exists"]:
        account_by_id[member.user_id] = MembershipAccountFacts(
            id=member.user_id,
            role=member_account["platform_role"],
            is_active=member_account["is_active"],
        )
    receiver_memberships = []
    for receiver in replacements.values():
        if receiver is None or not receiver["exists"]:
            continue
        receiver_id = _id(receiver["user_id"])
        account_by_id[receiver_id] = MembershipAccountFacts(
            id=receiver_id,
            role=receiver["platform_role"],
            is_active=receiver["is_active"],
        )
        if receiver["membership_id"] is not None:
            receiver_memberships.append(
                MembershipReceiverFacts(
                    id=_id(receiver["membership_id"]),
                    project_id=member.project_id,
                    user_id=receiver_id,
                    role=receiver["membership_role"],
                    version=receiver["membership_version"],
                )
            )

    batch_facts = [
        MembershipBatchFacts(
            id=_id(batch_id),
            status=batch["status"],
            annotator_id=_id(batch["annotator_id"]),
            reviewer_id=_id(batch["reviewer_id"]),
            assigned_user_ids=batch["assigned_user_ids"],
            admin_locked=batch["admin_locked"],
        )
        for batch_id, batch in snapshot["batches"].items()
    ]
    batches_by_id = {str(batch.id): batch for batch in batch_facts}
    task_facts = []
    for task_id, task in snapshot["tasks"].items():
        batch = batches_by_id.get(task["batch_id"])
        task_facts.append(
            MembershipTaskFacts(
                id=_id(task_id),
                status=task["status"],
                version=task["version"],
                batch_id=_id(task["batch_id"]),
                assignee_id=_id(task["assignee_id"]),
                assignee_is_override=task["assignee_is_override"],
                reviewer_id=_id(task["reviewer_id"]),
                reviewer_is_override=task["reviewer_is_override"],
                reviewer_claimed=task["reviewer_claimed"],
                review_round_id=_id(task["review_round_id"]),
                review_submitter_id=_id(task["review_submitter_id"]),
                annotation_contributor_ids=task["annotation_contributors"],
                review_contributor_ids=task["review_contributors"],
                batch_annotator_id=batch.annotator_id if batch else None,
                batch_reviewer_id=batch.reviewer_id if batch else None,
            )
        )

    details = vector["blocker_details"]
    by_code = {entry["code"]: entry.get("task_ids", []) for entry in details}
    labeling = MembershipLabelingFacts(
        schema=LABELING_MEMBERSHIP_WORK_PROTOCOL,
        project_id=member.project_id,
        user_id=member.user_id,
        annotation_task_ids=[_id(value) for value in snapshot["annotation_task_ids"]],
        review_task_ids=[_id(value) for value in snapshot["review_task_ids"]],
        unassigned_review_task_ids=[
            _id(value) for value in snapshot["unassigned_review_task_ids"]
        ],
        lock_task_ids=[_id(value) for value in snapshot["lock_task_ids"]],
        review_claim_task_ids=[
            _id(value) for value in snapshot["review_claim_task_ids"]
        ],
        materialization_annotation_task_ids=[
            _id(value) for value in snapshot["materialization_annotation_task_ids"]
        ],
        materialization_review_task_ids=[
            _id(value) for value in snapshot["materialization_review_task_ids"]
        ],
        batch_annotator_ids=[_id(value) for value in snapshot["batch_annotator_ids"]],
        batch_reviewer_ids=[_id(value) for value in snapshot["batch_reviewer_ids"]],
        tasks=task_facts,
        batches=batch_facts,
        annotation_receiver_conflict_task_ids=[
            _id(value)
            for value in by_code.get("replacement_annotator_reviewer_conflict", [])
        ],
        review_receiver_unknown_task_ids=[
            _id(value) for value in by_code.get("review_contributors_unknown", [])
        ],
        review_receiver_annotator_task_ids=[
            _id(value) for value in by_code.get("replacement_reviewer_is_annotator", [])
        ],
        review_receiver_contributor_task_ids=[
            _id(value) for value in by_code.get("replacement_reviewer_contributor", [])
        ],
    )
    facts = MembershipFactsData(
        kind="facts",
        operation_id=operation_id,
        operation=request.operation,
        actor_id=owner_id,
        project_id=member.project_id,
        transaction_id="membership-test-transaction",
        actor=MembershipActorFacts(
            id=owner_id,
            name="owner",
            email="owner@example.test",
            role="project_admin",
            is_active=True,
        ),
        project=MembershipProjectFacts(
            id=member.project_id,
            owner_id=owner_id,
        ),
        member=member,
        accounts=list(account_by_id.values()),
        receiver_memberships=receiver_memberships,
        eligible_reviewer_ids=[
            _id(value) for value in snapshot["eligible_reviewer_ids"]
        ],
        labeling=labeling,
    )
    return request, facts


def test_snapshot_and_digest_match_all_captured_legacy_vectors():
    for name, vector in _VECTORS.items():
        request, facts = _request_facts(name, vector)
        snapshot = build_member_resource_snapshot(request, facts)
        assert snapshot == vector["snapshot"], name
        assert snapshot_token(snapshot) == vector["preview_token"], name


def test_blocker_order_and_details_match_captured_legacy_vectors():
    for name, vector in _VECTORS.items():
        request, facts = _request_facts(name, vector)
        snapshot, blockers = evaluate_role_change(
            request, facts, request.payload.target_role
        )
        assert snapshot == vector["snapshot"], name
        assert [item["code"] for item in blockers] == vector["blockers"], name
        assert blockers == vector["blocker_details"], name


def test_digest_keeps_python_ascii_escaping_spaces_and_none_behavior():
    snapshot = {"unicode": "质检", "empty": None, "number": 7}
    legacy_json = json.dumps(snapshot, sort_keys=True, default=str)
    assert (
        snapshot_token(snapshot)
        == hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()
    )

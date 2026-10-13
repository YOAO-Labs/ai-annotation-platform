import json
import uuid
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.errors import ConflictError, PermissionDeniedError
from ai_annotation.models import (
    Batch,
    BatchDistributeResult,
    BulkBatchActionResult,
    Me,
    Member,
    ProjectMemberRolePreview,
)

from .conftest import API

PID = str(uuid4())
BID = str(uuid4())


def _batch(**extra) -> dict:
    return {
        "id": BID,
        "project_id": PID,
        "display_id": "B-1",
        "name": "batch-alpha",
        "status": "active",
        "total_tasks": 20,
        "completed_tasks": 12,
        "review_tasks": 3,
        "approved_tasks": 8,
        "rejected_tasks": 1,
        "progress_pct": 60.0,
        "annotator": {
            "id": str(uuid4()),
            "name": "标注员甲",
            "email": "a@x.io",
            "role": "annotator",
            "avatar_initial": "甲",
        },
        "reviewer": None,
        "created_at": "2026-06-11T00:00:00Z",
        **extra,
    }


def test_list_batches(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PID}/batches").mock(
        return_value=httpx.Response(200, json=[_batch()])
    )
    batches = client.batches.list(PID)
    assert route.called
    assert len(batches) == 1
    b = batches[0]
    assert isinstance(b, Batch)
    assert b.progress_pct == 60.0
    assert b.rejected_tasks == 1
    assert b.annotator.name == "标注员甲"
    assert b.reviewer is None


def test_list_batches_status_filter(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PID}/batches").mock(
        return_value=httpx.Response(200, json=[])
    )
    client.batches.list(PID, status="reviewing")
    assert route.calls.last.request.url.params["status"] == "reviewing"


def test_get_batch(client, respx_mock):
    respx_mock.get(f"{API}/projects/{PID}/batches/{BID}").mock(
        return_value=httpx.Response(200, json=_batch(status="approved"))
    )
    b = client.batches.get(PID, BID)
    assert b.status == "approved"


def test_create_update_delete_batch(client, respx_mock):
    create_route = respx_mock.post(f"{API}/projects/{PID}/batches").mock(
        return_value=httpx.Response(201, json=_batch())
    )
    batch = client.batches.create(PID, "batch-alpha", priority=70)
    assert json.loads(create_route.calls.last.request.content) == {
        "name": "batch-alpha",
        "priority": 70,
    }
    assert isinstance(batch, Batch)

    update_route = respx_mock.patch(f"{API}/projects/{PID}/batches/{BID}").mock(
        return_value=httpx.Response(200, json=_batch(name="renamed"))
    )
    batch = client.batches.update(PID, BID, name="renamed")
    assert json.loads(update_route.calls.last.request.content) == {"name": "renamed"}
    assert batch.name == "renamed"

    delete_route = respx_mock.delete(f"{API}/projects/{PID}/batches/{BID}").mock(
        return_value=httpx.Response(204)
    )
    assert client.batches.delete(PID, BID, force=True) is None
    assert delete_route.calls.last.request.url.params["force"] == "true"


@pytest.mark.parametrize("method", ["create", "update"])
def test_batch_writes_map_permission_error(client, respx_mock, method):
    path = f"{API}/projects/{PID}/batches"
    if method == "update":
        path += f"/{BID}"
    getattr(respx_mock, "post" if method == "create" else "patch")(path).mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        if method == "create":
            client.batches.create(PID, "blocked")
        else:
            client.batches.update(PID, BID, name="blocked")


def test_delete_batch_maps_conflict(client, respx_mock):
    respx_mock.delete(f"{API}/projects/{PID}/batches/{BID}").mock(
        return_value=httpx.Response(409, json={"detail": "requires_force"})
    )
    with pytest.raises(ConflictError):
        client.batches.delete(PID, BID)


def test_batch_workflow_actions(client, respx_mock):
    transition = respx_mock.post(f"{API}/projects/{PID}/batches/{BID}/transition").mock(
        return_value=httpx.Response(200, json=_batch(status="reviewing"))
    )
    assert client.batches.transition(PID, BID, "reviewing").status == "reviewing"
    assert json.loads(transition.calls.last.request.content) == {
        "target_status": "reviewing"
    }

    reject = respx_mock.post(f"{API}/projects/{PID}/batches/{BID}/reject").mock(
        return_value=httpx.Response(200, json=_batch(status="rejected"))
    )
    assert client.batches.reject(PID, BID, "fix labels").status == "rejected"
    assert json.loads(reject.calls.last.request.content) == {"feedback": "fix labels"}

    reset = respx_mock.post(f"{API}/projects/{PID}/batches/{BID}/reset").mock(
        return_value=httpx.Response(200, json=_batch(status="draft"))
    )
    assert client.batches.reset(PID, BID, "reset for correction").status == "draft"
    assert json.loads(reset.calls.last.request.content) == {
        "reason": "reset for correction"
    }


def test_distribute_batches(client, respx_mock):
    annotator_id, reviewer_id = uuid4(), uuid4()
    route = respx_mock.post(f"{API}/projects/{PID}/batches/distribute-batches").mock(
        return_value=httpx.Response(
            200,
            json={
                "distributed_batches": 2,
                "annotator_per_batch": {BID: str(annotator_id)},
                "reviewer_per_batch": {BID: str(reviewer_id)},
            },
        )
    )
    result = client.batches.distribute(
        PID,
        annotator_ids=[annotator_id],
        reviewer_ids=[reviewer_id],
        only_unassigned=True,
    )
    assert json.loads(route.calls.last.request.content) == {
        "annotator_ids": [str(annotator_id)],
        "reviewer_ids": [str(reviewer_id)],
        "only_unassigned": True,
    }
    assert isinstance(result, BatchDistributeResult)
    assert result.distributed_batches == 2


@pytest.mark.parametrize(
    ("action", "call", "extra"),
    [
        ("bulk-activate", lambda c, ids: c.batches.bulk_activate(PID, ids), {}),
        ("bulk-approve", lambda c, ids: c.batches.bulk_approve(PID, ids), {}),
        (
            "bulk-reject",
            lambda c, ids: c.batches.bulk_reject(PID, ids, "needs work"),
            {"feedback": "needs work"},
        ),
        (
            "bulk-reassign",
            lambda c, ids: c.batches.bulk_reassign(
                PID, ids, annotator_id=None, reviewer_id=uuid4()
            ),
            None,
        ),
    ],
)
def test_bulk_batch_actions(client, respx_mock, action, call, extra):
    ids = [uuid4(), uuid4()]
    route = respx_mock.post(f"{API}/projects/{PID}/batches/{action}").mock(
        return_value=httpx.Response(
            200,
            json={
                "succeeded": [str(ids[0])],
                "skipped": [{"batch_id": str(ids[1]), "reason": "wrong_status"}],
                "failed": [],
            },
        )
    )
    result = call(client, ids)
    body = json.loads(route.calls.last.request.content)
    assert body["batch_ids"] == [str(value) for value in ids]
    if extra:
        assert body | extra == body
    assert isinstance(result, BulkBatchActionResult)
    assert result.succeeded == [ids[0]]
    assert result.skipped[0].reason == "wrong_status"


def test_bulk_reassign_validates_assignment(client):
    with pytest.raises(ValueError, match="at least one"):
        client.batches.bulk_reassign(PID, [BID])
    with pytest.raises(ValueError, match="unsupported"):
        client.batches.bulk_reassign(PID, [BID], assignee_id=uuid4())


def test_export_batch(client, respx_mock):
    job_id = str(uuid4())
    route = respx_mock.post(f"{API}/projects/{PID}/batches/{BID}/export").mock(
        return_value=httpx.Response(202, json={"job_id": job_id})
    )
    assert (
        client.batches.export(PID, BID, targets=["coco", "aap_json"], axis_frame="iso")
        == job_id
    )
    params = route.calls.last.request.url.params
    assert params.get_list("targets") == ["coco", "aap_json"]
    assert params["axis_frame"] == "iso"


def test_export_batch_rejects_synchronous_voc(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PID}/batches/{BID}/export")

    with pytest.raises(ValueError, match="VOC batch export returns ZIP bytes"):
        client.batches.export(PID, BID, targets=["voc"])

    assert not route.called


@pytest.mark.parametrize(
    ("path", "call"),
    [
        (f"{BID}/transition", lambda c: c.batches.transition(PID, BID, "active")),
        (f"{BID}/reject", lambda c: c.batches.reject(PID, BID, "bad")),
        (f"{BID}/reset", lambda c: c.batches.reset(PID, BID, "long reason")),
        ("distribute-batches", lambda c: c.batches.distribute(PID)),
        ("bulk-activate", lambda c: c.batches.bulk_activate(PID, [BID])),
        ("bulk-approve", lambda c: c.batches.bulk_approve(PID, [BID])),
        ("bulk-reject", lambda c: c.batches.bulk_reject(PID, [BID], "bad")),
        (
            "bulk-reassign",
            lambda c: c.batches.bulk_reassign(PID, [BID], annotator_id=None),
        ),
        (f"{BID}/export", lambda c: c.batches.export(PID, BID)),
    ],
)
def test_batch_workflow_maps_permission_error(client, respx_mock, path, call):
    respx_mock.post(f"{API}/projects/{PID}/batches/{path}").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        call(client)


def test_list_members(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PID}/members").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "user_id": str(uuid4()),
                    "user_name": "张三",
                    "user_email": "zhang@x.io",
                    "role": "annotator",
                    "platform_role": "employee",
                    "version": 1,
                    "assigned_at": "2026-06-10T00:00:00Z",
                }
            ],
        )
    )
    members = client.members.list(PID)
    assert route.called
    assert len(members) == 1
    assert isinstance(members[0], Member)
    assert members[0].user_name == "张三"
    assert members[0].role == "annotator"
    assert members[0].platform_role == "employee"


def test_add_and_remove_member(client, respx_mock):
    member_id = str(uuid4())
    user_id = str(uuid4())
    payload = {
        "id": member_id,
        "user_id": user_id,
        "user_name": "李四",
        "user_email": "li@example.com",
        "role": "reviewer",
        "platform_role": "employee",
        "version": 1,
        "assigned_at": "2026-06-10T00:00:00Z",
    }
    add_route = respx_mock.post(f"{API}/projects/{PID}/members").mock(
        return_value=httpx.Response(201, json=payload)
    )
    member = client.members.add(PID, user_id, "reviewer")
    assert json.loads(add_route.calls.last.request.content) == {
        "user_id": user_id,
        "role": "reviewer",
    }
    assert member.id == uuid.UUID(member_id)

    remove_route = respx_mock.delete(f"{API}/projects/{PID}/members/{member_id}").mock(
        return_value=httpx.Response(204)
    )
    assert client.members.remove(PID, member_id) is None
    assert remove_route.called


@pytest.mark.parametrize("method", ["add", "remove"])
def test_member_writes_map_permission_error(client, respx_mock, method):
    member_id = str(uuid4())
    path = f"{API}/projects/{PID}/members"
    if method == "remove":
        path += f"/{member_id}"
    getattr(respx_mock, "post" if method == "add" else "delete")(path).mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        if method == "add":
            client.members.add(PID, uuid4(), "annotator")
        else:
            client.members.remove(PID, member_id)


def test_me(client, respx_mock):
    respx_mock.get(f"{API}/auth/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "email": "me@x.io",
                "name": "Me",
                "role": "project_admin",
                "status": "active",
            },
        )
    )
    me = client.me()
    assert isinstance(me, Me)
    assert me.role == "project_admin"
    assert me.email == "me@x.io"


def test_me_role_is_platform_role_not_legacy_staff(client, respx_mock):
    # /auth/me 是实时账号契约: 平台角色, 不是历史全局 annotator/reviewer。
    respx_mock.get(f"{API}/auth/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "email": "me@x.io",
                "name": "Me",
                "role": "annotator",
                "status": "active",
            },
        )
    )
    with pytest.raises(ValidationError):
        client.me()


def test_members_permission_denied(client, respx_mock):
    respx_mock.get(f"{API}/projects/{PID}/members").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        client.members.list(PID)


def _member(**extra) -> dict:
    return {
        "id": str(uuid4()),
        "user_id": str(uuid4()),
        "user_name": "王五",
        "user_email": "wang@x.io",
        "role": "reviewer",
        "platform_role": "employee",
        "version": 3,
        "assigned_at": "2026-06-10T00:00:00Z",
        "updated_at": "2026-06-20T00:00:00Z",
        **extra,
    }


def test_preview_member_role_change(client, respx_mock):
    member_id = str(uuid4())
    user_id = str(uuid4())
    replacement = str(uuid4())
    route = respx_mock.post(
        f"{API}/projects/{PID}/members/{member_id}/role/preview"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "member_id": member_id,
                "user_id": user_id,
                "current_role": "reviewer",
                "current_version": 3,
                "target_role": "annotator",
                "requires_handoff": True,
                "blockers": ["active_review_claims"],
                "blocker_details": [{"kind": "active_review_claims", "count": 2}],
                "resource_snapshot": {"active_task_ids": [str(uuid4())]},
                "preview_token": "tok-1",
            },
        )
    )
    preview = client.members.preview_role_change(
        PID, member_id, "annotator", replacement_annotator_id=replacement
    )
    assert route.called
    assert json.loads(route.calls.last.request.content) == {
        "project_role": "annotator",
        "replacement_annotator_id": replacement,
    }
    assert isinstance(preview, ProjectMemberRolePreview)
    assert preview.requires_handoff is True
    assert preview.preview_token == "tok-1"
    assert preview.blocker_details[0]["kind"] == "active_review_claims"


def test_change_member_role(client, respx_mock):
    member_id = str(uuid4())
    replacement_reviewer = str(uuid4())
    route = respx_mock.patch(f"{API}/projects/{PID}/members/{member_id}/role").mock(
        return_value=httpx.Response(200, json=_member(role="annotator", version=4))
    )
    member = client.members.change_role(
        PID,
        member_id,
        "annotator",
        expected_version=3,
        preview_token="tok-1",
        reason="rebalance workload",
        replacement_reviewer_id=replacement_reviewer,
    )
    assert isinstance(member, Member)
    assert member.role == "annotator"
    assert member.version == 4
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "project_role": "annotator",
        "expected_version": 3,
        "preview_token": "tok-1",
        "reason": "rebalance workload",
        "replacement_reviewer_id": replacement_reviewer,
    }


def test_change_member_role_maps_conflict(client, respx_mock):
    member_id = str(uuid4())
    respx_mock.patch(f"{API}/projects/{PID}/members/{member_id}/role").mock(
        return_value=httpx.Response(409, json={"detail": "stale_membership_version"})
    )
    with pytest.raises(ConflictError):
        client.members.change_role(
            PID,
            member_id,
            "viewer",
            expected_version=1,
            preview_token="tok-1",
            reason="demote",
        )


@pytest.mark.parametrize("operation", ["preview", "change"])
def test_member_role_maps_permission_error(client, respx_mock, operation):
    member_id = str(uuid4())
    path = f"{API}/projects/{PID}/members/{member_id}/role"
    if operation == "preview":
        route = respx_mock.post(f"{path}/preview")
    else:
        route = respx_mock.patch(path)
    route.mock(return_value=httpx.Response(403, json={"detail": "forbidden"}))
    with pytest.raises(PermissionDeniedError):
        if operation == "preview":
            client.members.preview_role_change(PID, member_id, "viewer")
        else:
            client.members.change_role(
                PID,
                member_id,
                "viewer",
                expected_version=1,
                preview_token="tok-1",
                reason="demote",
            )

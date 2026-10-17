"""Project operation request/response and authorization-scope contracts."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.client import Projects
from ai_annotation.project_operation_models import (
    LidarExportPreflightRequest,
    ProjectClassRenameRequest,
    ProjectTransferRequest,
)

from .conftest import API


def _project(project_id: str) -> dict:
    return {
        "id": project_id,
        "display_id": "P-12",
        "name": "project",
        "type_key": "object_detection",
        "data_type": "image",
        "status": "active",
    }


def _readiness(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "guide_ready": True,
        "classes_ready": True,
        "linked_dataset_count": 1,
        "dataset_item_count": 4,
        "task_count": 4,
        "linked_task_count": 4,
        "task_creation_active_jobs": 0,
        "task_creation_failed_jobs": 1,
        "latest_task_creation_status": "failed",
        "batch_count": 2,
        "nonempty_batch_count": 2,
        "executable_batch_count": 1,
        "assigned_batch_count": 1,
        "active_annotator_count": 1,
        "active_reviewer_count": 1,
        "activated_batch_count": 1,
    }


def test_project_query_and_operations_use_literal_routes_and_typed_payloads(
    client, respx_mock
):
    project_id = str(uuid4())
    member_id = str(uuid4())
    owner_id = str(uuid4())
    route_query = respx_mock.get(f"{API}/projects/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [_project(project_id)],
                "total": 21,
                "page": 2,
                "page_size": 10,
                "pages": 3,
            },
        )
    )
    route_readiness = respx_mock.get(f"{API}/projects/{project_id}/readiness").mock(
        return_value=httpx.Response(200, json=_readiness(project_id))
    )
    route_class_usage = respx_mock.get(f"{API}/projects/{project_id}/class-usage").mock(
        return_value=httpx.Response(
            200,
            json={"classes": {"car": 8}, "attributes": {"color": 5}},
        )
    )
    route_rename = respx_mock.post(f"{API}/projects/{project_id}/classes/rename").mock(
        return_value=httpx.Response(200, json=_project(project_id))
    )
    route_cleanup_annotations = respx_mock.post(
        f"{API}/projects/{project_id}/cleanup-orphans"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"orphan_annotations": 2, "orphan_attribute_keys": {"legacy": 3}},
            ),
            httpx.Response(
                200,
                json={"orphan_annotations": 2, "orphan_attribute_keys": {"legacy": 3}},
            ),
        ]
    )
    route_datasets = respx_mock.get(f"{API}/projects/{project_id}/datasets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "display_id": "D-1",
                    "name": "source",
                    "data_type": "image",
                    "linked_at": "2026-10-17T00:00:00Z",
                    "items_count": 4,
                    "tasks_in_project": 4,
                }
            ],
        )
    )
    route_mentions = respx_mock.get(
        f"{API}/projects/{project_id}/mention-candidates"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "user_id": member_id,
                    "user_name": "Member",
                    "user_email": "member@example.test",
                    "kind": "member",
                }
            ],
        )
    )
    route_lidar = respx_mock.post(
        f"{API}/projects/{project_id}/exports/lidar:preflight"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "ready": False,
                "camera_roles": ["camera_front"],
                "selected_camera_role": "camera_front",
                "checked_tasks": 4,
                "issue_count": 1,
                "issues_truncated": False,
                "issues": [{"code": "missing_frame", "message": "frame unavailable"}],
            },
        )
    )
    route_orphan_preview = respx_mock.get(
        f"{API}/projects/{project_id}/orphan-tasks/preview"
    ).mock(
        return_value=httpx.Response(
            200, json={"orphan_tasks": 3, "orphan_annotations": 7}
        )
    )
    route_orphan_cleanup = respx_mock.post(
        f"{API}/projects/{project_id}/orphan-tasks/cleanup"
    ).mock(
        return_value=httpx.Response(
            200, json={"deleted_tasks": 3, "deleted_annotations": 7}
        )
    )
    route_transfer = respx_mock.post(f"{API}/projects/{project_id}/transfer").mock(
        return_value=httpx.Response(200, json=_project(project_id))
    )

    projects = Projects(client._http)
    page = projects.query(
        page=2,
        page_size=10,
        status="active",
        search="object",
        type_key=["object_detection", "segmentation"],
        data_type=["image", "lidar"],
        member_id=member_id,
        project_role="reviewer",
        created_from="2026-10-01",
        created_to="2026-10-17",
    )
    assert page.items[0].display_id == "P-12"
    assert route_query.calls.last.request.url.params.get_list("type_key") == [
        "object_detection",
        "segmentation",
    ]
    assert route_query.calls.last.request.url.params.get_list("data_type") == [
        "image",
        "lidar",
    ]
    assert route_query.calls.last.request.url.params["page_size"] == "10"

    readiness = projects.readiness(project_id)
    assert readiness.task_creation_failed_jobs == 1
    assert route_readiness.called
    assert projects.class_usage(project_id).attributes == {"color": 5}
    assert route_class_usage.called

    rename = ProjectClassRenameRequest(
        old_name=" car ", new_name=" vehicle ", tool_unit_id="unit-1"
    )
    projects.rename_class(project_id, rename)
    assert json.loads(route_rename.calls.last.request.content) == {
        "old_name": "car",
        "new_name": "vehicle",
        "tool_unit_id": "unit-1",
    }
    with pytest.raises(ValidationError, match="must not be blank|blank"):
        ProjectClassRenameRequest(old_name=" ", new_name="vehicle")

    projects.cleanup_annotation_orphans(project_id)
    projects.cleanup_annotation_orphans(project_id, dry_run=False)
    assert json.loads(route_cleanup_annotations.calls[0].request.content) == {
        "dry_run": True
    }
    assert json.loads(route_cleanup_annotations.calls[1].request.content) == {
        "dry_run": False
    }
    assert projects.list_datasets(project_id)[0].tasks_in_project == 4
    assert projects.mention_candidates(project_id)[0].kind == "member"
    assert route_datasets.called and route_mentions.called

    preflight = projects.lidar_export_preflight(
        project_id,
        LidarExportPreflightRequest(
            targets=["kitti"], lidar={"kitti_camera_role": "camera_front"}
        ),
    )
    assert preflight.ready is False
    assert json.loads(route_lidar.calls.last.request.content) == {
        "targets": ["kitti"],
        "lidar": {"kitti_camera_role": "camera_front"},
    }

    assert projects.preview_orphan_tasks(project_id).orphan_annotations == 7
    assert route_orphan_preview.called
    assert projects.cleanup_orphan_tasks(project_id).deleted_tasks == 3
    assert route_orphan_cleanup.calls.last.request.content == b""

    projects.transfer(project_id, ProjectTransferRequest(new_owner_id=owner_id))
    assert json.loads(route_transfer.calls.last.request.content) == {
        "new_owner_id": owner_id
    }

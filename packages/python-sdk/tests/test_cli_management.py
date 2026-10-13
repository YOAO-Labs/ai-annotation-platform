"""Phase 2 资源管理 CLI 命令。"""

import json
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from ai_annotation.cli.main import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}

PROJECT_ID = str(uuid4())
PROJECT = {
    "id": PROJECT_ID,
    "display_id": "P-1",
    "name": "demo",
    "type_key": "object_detection",
    "data_type": "image",
    "status": "active",
}
DATASET_ID = str(uuid4())
DATASET = {
    "id": DATASET_ID,
    "display_id": "D-1",
    "name": "dataset",
    "data_type": "image",
    "file_count": 1,
    "total_size": 10,
}
BATCH_ID = str(uuid4())


def _batch(**extra) -> dict:
    return {
        "id": BATCH_ID,
        "project_id": PROJECT_ID,
        "display_id": "B-1",
        "name": "batch",
        "status": "active",
        "total_tasks": 2,
        "completed_tasks": 1,
        "review_tasks": 0,
        "approved_tasks": 0,
        "rejected_tasks": 0,
        **extra,
    }


def test_projects_update_and_delete_confirmation(respx_mock):
    update_route = respx_mock.patch(f"{API}/projects/{PROJECT_ID}").mock(
        return_value=httpx.Response(200, json={**PROJECT, "name": "renamed"})
    )
    result = runner.invoke(
        app, ["projects", "update", PROJECT_ID, "--name", "renamed", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert json.loads(update_route.calls.last.request.content) == {"name": "renamed"}

    result = runner.invoke(app, ["projects", "delete", PROJECT_ID, "--json"], env=ENV)
    assert result.exit_code == 2
    assert "--yes" in result.output

    delete_route = respx_mock.delete(f"{API}/projects/{PROJECT_ID}").mock(
        return_value=httpx.Response(204)
    )
    result = runner.invoke(
        app, ["projects", "delete", PROJECT_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert delete_route.called
    assert json.loads(result.stdout)["deleted"] is True


def test_members_add_and_remove(respx_mock):
    member_id = str(uuid4())
    user_id = str(uuid4())
    member = {
        "id": member_id,
        "user_id": user_id,
        "user_name": "李四",
        "user_email": "li@example.com",
        "role": "reviewer",
        "platform_role": "employee",
        "version": 1,
        "assigned_at": "2026-08-14T00:00:00Z",
    }
    add_route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/members").mock(
        return_value=httpx.Response(201, json=member)
    )
    result = runner.invoke(
        app,
        [
            "members",
            "add",
            PROJECT_ID,
            "--user-id",
            user_id,
            "--role",
            "reviewer",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(add_route.calls.last.request.content)["role"] == "reviewer"

    remove_route = respx_mock.delete(
        f"{API}/projects/{PROJECT_ID}/members/{member_id}"
    ).mock(return_value=httpx.Response(204))
    result = runner.invoke(
        app,
        ["members", "remove", PROJECT_ID, member_id, "--yes", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert remove_route.called


def test_members_add_viewer_role(respx_mock):
    user_id = str(uuid4())
    member = {
        "id": str(uuid4()),
        "user_id": user_id,
        "user_name": "观察者",
        "user_email": "viewer@example.com",
        "role": "viewer",
        "platform_role": "viewer",
        "version": 1,
        "assigned_at": "2026-08-14T00:00:00Z",
    }
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/members").mock(
        return_value=httpx.Response(201, json=member)
    )
    result = runner.invoke(
        app,
        [
            "members",
            "add",
            PROJECT_ID,
            "--user-id",
            user_id,
            "--role",
            "viewer",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content)["role"] == "viewer"


def test_members_preview_and_change_role(respx_mock):
    member_id = str(uuid4())
    user_id = str(uuid4())
    preview_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/members/{member_id}/role/preview"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "member_id": member_id,
                "user_id": user_id,
                "current_role": "annotator",
                "current_version": 2,
                "target_role": "reviewer",
                "requires_handoff": False,
                "blockers": [],
                "blocker_details": [],
                "resource_snapshot": {},
                "preview_token": "tok-9",
            },
        )
    )
    preview = runner.invoke(
        app,
        [
            "members",
            "preview-role",
            PROJECT_ID,
            member_id,
            "--role",
            "reviewer",
            "--json",
        ],
        env=ENV,
    )
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["preview_token"] == "tok-9"
    assert json.loads(preview_route.calls.last.request.content) == {
        "project_role": "reviewer"
    }

    change_route = respx_mock.patch(
        f"{API}/projects/{PROJECT_ID}/members/{member_id}/role"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": member_id,
                "user_id": user_id,
                "user_name": "李四",
                "user_email": "li@example.com",
                "role": "reviewer",
                "platform_role": "employee",
                "version": 3,
                "assigned_at": "2026-08-14T00:00:00Z",
            },
        )
    )
    args = [
        "members",
        "change-role",
        PROJECT_ID,
        member_id,
        "--role",
        "reviewer",
        "--expected-version",
        "2",
        "--preview-token",
        "tok-9",
        "--reason",
        "rebalance workload",
        "--json",
    ]
    guarded = runner.invoke(app, args, env=ENV)
    assert guarded.exit_code == 2
    assert "--yes" in guarded.output
    assert not change_route.called

    result = runner.invoke(app, [*args, "--yes"], env=ENV)
    assert result.exit_code == 0
    body = json.loads(change_route.calls.last.request.content)
    assert body["expected_version"] == 2
    assert body["preview_token"] == "tok-9"
    assert body["reason"] == "rebalance workload"
    assert json.loads(result.stdout)["role"] == "reviewer"


def test_datasets_update_items_projects_and_preview(respx_mock):
    update_route = respx_mock.put(f"{API}/datasets/{DATASET_ID}").mock(
        return_value=httpx.Response(200, json=DATASET)
    )
    result = runner.invoke(
        app,
        ["datasets", "update", DATASET_ID, "--clear-axis-convention", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(update_route.calls.last.request.content) == {
        "axis_convention": None
    }

    item_id = str(uuid4())
    respx_mock.get(f"{API}/datasets/{DATASET_ID}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": item_id,
                        "dataset_id": DATASET_ID,
                        "file_name": "a.jpg",
                        "file_path": "datasets/a.jpg",
                        "file_type": "image/jpeg",
                    }
                ],
                "total": 1,
                "limit": 5,
                "offset": 0,
            },
        )
    )
    result = runner.invoke(
        app,
        ["datasets", "items", DATASET_ID, "--limit", "5", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["items"][0]["id"] == item_id

    respx_mock.get(f"{API}/datasets/{DATASET_ID}/projects").mock(
        return_value=httpx.Response(200, json=[PROJECT])
    )
    result = runner.invoke(app, ["datasets", "projects", DATASET_ID, "--json"], env=ENV)
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["id"] == PROJECT_ID

    respx_mock.get(
        f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}/preview-unlink"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "will_delete_tasks": 2,
                "will_delete_annotations": 3,
                "will_delete_batches": 1,
            },
        )
    )
    result = runner.invoke(
        app,
        ["datasets", "preview-unlink", DATASET_ID, PROJECT_ID, "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["will_delete_annotations"] == 3


def test_dataset_destructive_commands(respx_mock):
    item_id = str(uuid4())
    delete_item_route = respx_mock.delete(
        f"{API}/datasets/{DATASET_ID}/items/{item_id}"
    ).mock(return_value=httpx.Response(204))
    result = runner.invoke(
        app,
        ["datasets", "delete-item", DATASET_ID, item_id, "--yes", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0
    assert delete_item_route.called

    delete_route = respx_mock.delete(f"{API}/datasets/{DATASET_ID}").mock(
        return_value=httpx.Response(204)
    )
    result = runner.invoke(
        app, ["datasets", "delete", DATASET_ID, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert delete_route.called


def test_dataset_unlink_previews_before_interactive_confirmation(respx_mock):
    preview_route = respx_mock.get(
        f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}/preview-unlink"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "will_delete_tasks": 2,
                "will_delete_annotations": 3,
                "will_delete_batches": 1,
            },
        )
    )
    unlink_route = respx_mock.delete(
        f"{API}/datasets/{DATASET_ID}/link/{PROJECT_ID}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "deleted_tasks": 2,
                "deleted_annotations": 3,
                "deleted_batches": 1,
                "deleted_batch_ids": [],
            },
        )
    )
    result = runner.invoke(
        app, ["datasets", "unlink", DATASET_ID, PROJECT_ID], input="y\n", env=ENV
    )
    assert result.exit_code == 0
    assert preview_route.called and unlink_route.called
    assert result.output.index("将删除") < result.output.index("确认取消关联")


def test_dataset_unlink_json_requires_yes():
    result = runner.invoke(
        app, ["datasets", "unlink", DATASET_ID, PROJECT_ID, "--json"], env=ENV
    )
    assert result.exit_code == 2
    assert "--yes" in result.output


def test_batches_create_update_and_force_delete(respx_mock):
    create_route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/batches").mock(
        return_value=httpx.Response(201, json=_batch())
    )
    result = runner.invoke(
        app,
        [
            "batches",
            "create",
            PROJECT_ID,
            "--name",
            "batch",
            "--priority",
            "70",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(create_route.calls.last.request.content) == {
        "name": "batch",
        "priority": 70,
    }

    update_route = respx_mock.patch(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}"
    ).mock(return_value=httpx.Response(200, json=_batch(name="renamed")))
    result = runner.invoke(
        app,
        [
            "batches",
            "update",
            PROJECT_ID,
            BATCH_ID,
            "--name",
            "renamed",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(update_route.calls.last.request.content) == {"name": "renamed"}

    get_route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}").mock(
        return_value=httpx.Response(200, json=_batch())
    )
    result = runner.invoke(
        app,
        ["batches", "delete", PROJECT_ID, BATCH_ID, "--yes", "--json"],
        env=ENV,
    )
    assert result.exit_code == 2
    assert "--force" in result.output
    assert get_route.called

    delete_route = respx_mock.delete(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}"
    ).mock(return_value=httpx.Response(204))
    result = runner.invoke(
        app,
        [
            "batches",
            "delete",
            PROJECT_ID,
            BATCH_ID,
            "--force",
            "--yes",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert delete_route.calls.last.request.url.params["force"] == "true"


def test_batch_reset_requires_confirmation(respx_mock):
    reset_route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/reset"
    ).mock(return_value=httpx.Response(200, json=_batch(status="draft")))

    guarded = runner.invoke(
        app,
        [
            "batches",
            "reset",
            PROJECT_ID,
            BATCH_ID,
            "--reason",
            "重新分配任务",
            "--json",
        ],
        env=ENV,
    )
    assert guarded.exit_code == 2
    assert "--yes" in guarded.output
    assert not reset_route.called

    result = runner.invoke(
        app,
        [
            "batches",
            "reset",
            PROJECT_ID,
            BATCH_ID,
            "--reason",
            "重新分配任务",
            "--yes",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert reset_route.called

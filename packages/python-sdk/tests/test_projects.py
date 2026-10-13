import json
from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import PermissionDeniedError
from ai_annotation.models import Project, ProjectAccess

from .conftest import API

PROJECT = {
    "id": str(uuid4()),
    "display_id": "P-1",
    "name": "demo",
    "type_label": "目标检测",
    "type_key": "object_detection",
    "data_type": "image",
    "status": "active",
    "created_at": "2026-06-11T00:00:00Z",
}


def test_list_projects(client, respx_mock):
    route = respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=[PROJECT])
    )
    projects = client.projects.list(status="active", search="de")
    assert route.called
    req = route.calls.last.request
    assert req.url.params["status"] == "active"
    assert req.url.params["search"] == "de"
    assert req.headers["Authorization"] == "Bearer ak_test"
    assert isinstance(projects[0], Project)
    assert projects[0].display_id == "P-1"


def test_create_project_fills_type_label(client, respx_mock):
    route = respx_mock.post(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )
    p = client.projects.create(
        name="demo", type_key="object_detection", data_type="image"
    )
    body = json.loads(route.calls.last.request.content)
    assert body["name"] == "demo"
    assert body["type_key"] == "object_detection"
    assert body["data_type"] == "image"
    # 后端 ProjectCreate.type_label 必填, SDK 按 type_key 兜底
    assert body["type_label"] == "object_detection"
    assert p.name == "demo"


def test_get_project(client, respx_mock):
    pid = PROJECT["id"]
    respx_mock.get(f"{API}/projects/{pid}").mock(
        return_value=httpx.Response(200, json=PROJECT)
    )
    p = client.projects.get(pid)
    assert str(p.id) == pid


def test_update_project_sends_only_explicit_fields(client, respx_mock):
    route = respx_mock.patch(f"{API}/projects/{PROJECT['id']}").mock(
        return_value=httpx.Response(200, json={**PROJECT, "name": "renamed"})
    )
    project = client.projects.update(PROJECT["id"], name="renamed")
    assert json.loads(route.calls.last.request.content) == {"name": "renamed"}
    assert project.name == "renamed"


def test_delete_project(client, respx_mock):
    route = respx_mock.delete(f"{API}/projects/{PROJECT['id']}").mock(
        return_value=httpx.Response(204)
    )
    assert client.projects.delete(PROJECT["id"]) is None
    assert route.called


@pytest.mark.parametrize("method", ["update", "delete"])
def test_project_writes_map_permission_error(client, respx_mock, method):
    route = getattr(respx_mock, "patch" if method == "update" else "delete")(
        f"{API}/projects/{PROJECT['id']}"
    )
    route.mock(return_value=httpx.Response(403, json={"detail": "forbidden"}))
    with pytest.raises(PermissionDeniedError):
        if method == "update":
            client.projects.update(PROJECT["id"], name="blocked")
        else:
            client.projects.delete(PROJECT["id"])


def test_project_list_exposes_my_project_role(client, respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            200, json=[{**PROJECT, "my_project_role": "reviewer"}]
        )
    )
    projects = client.projects.list()
    assert projects[0].my_project_role == "reviewer"


def test_project_access_for_member(client, respx_mock):
    pid = PROJECT["id"]
    user_id = str(uuid4())
    member_id = str(uuid4())
    respx_mock.get(f"{API}/projects/{pid}/access").mock(
        return_value=httpx.Response(
            200,
            json={
                "project_id": pid,
                "user_id": user_id,
                "platform_role": "employee",
                "project_role": "reviewer",
                "membership_id": member_id,
                "membership_version": 2,
                "access_kind": "member",
                "is_manager": False,
                "capabilities": [
                    "project.read",
                    "task.read",
                    "review.write",
                    "export.annotations",
                ],
            },
        )
    )
    access = client.projects.access(pid)
    assert isinstance(access, ProjectAccess)
    assert access.project_role == "reviewer"
    assert access.platform_role == "employee"
    assert access.access_kind == "member"
    assert access.membership_version == 2
    assert "review.write" in access.capabilities


def test_project_access_for_manager_has_no_membership(client, respx_mock):
    pid = PROJECT["id"]
    respx_mock.get(f"{API}/projects/{pid}/access").mock(
        return_value=httpx.Response(
            200,
            json={
                "project_id": pid,
                "user_id": str(uuid4()),
                "platform_role": "project_admin",
                "access_kind": "owner",
                "is_manager": True,
            },
        )
    )
    access = client.projects.access(pid)
    assert access.access_kind == "owner"
    assert access.is_manager is True
    assert access.project_role is None
    assert access.membership_id is None
    assert access.capabilities == []


def test_project_access_maps_permission_error(client, respx_mock):
    pid = PROJECT["id"]
    respx_mock.get(f"{API}/projects/{pid}/access").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        client.projects.access(pid)


def test_extra_fields_tolerated(client, respx_mock):
    # 服务端新增字段不应破坏解析 (extra="allow")
    payload = {**PROJECT, "brand_new_field": {"x": 1}}
    respx_mock.get(f"{API}/projects/{PROJECT['id']}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    p = client.projects.get(PROJECT["id"])
    assert p.brand_new_field == {"x": 1}

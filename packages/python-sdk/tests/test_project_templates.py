"""Project template SDK request and response contracts."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.project_template_models import (
    ProjectTemplate,
    ProjectTemplateCreate,
    ProjectTemplateUpdate,
)
from ai_annotation.project_templates import ProjectTemplates

from .conftest import API

TEMPLATE_ID = str(uuid4())
SOURCE_ID = str(uuid4())
CREATED_BY = str(uuid4())

TEMPLATE = {
    "id": TEMPLATE_ID,
    "display_id": "PT-1",
    "name": "Vehicles",
    "type_label": "Object detection",
    "type_key": "image-det",
    "data_type": "image",
    "created_by": CREATED_BY,
    "created_at": "2026-10-17T00:00:00Z",
    "updated_at": "2026-10-17T00:00:00Z",
}


def test_list_uses_repeated_type_key_and_returns_array(client, respx_mock):
    route = respx_mock.get(f"{API}/project-templates").mock(
        return_value=httpx.Response(200, json=[TEMPLATE])
    )

    templates = ProjectTemplates(client._http).list(
        scope="public", type_key=["image-det", "video-track"], search="vehicle"
    )

    assert route.called
    assert route.calls.last.request.url.params.get_list("type_key") == [
        "image-det",
        "video-track",
    ]
    assert route.calls.last.request.url.params["scope"] == "public"
    assert route.calls.last.request.url.params["search"] == "vehicle"
    assert len(templates) == 1
    assert isinstance(templates[0], ProjectTemplate)
    assert templates[0].name == "Vehicles"


def test_create_source_only_preserves_dynamic_config_without_sdk_merge(
    client, respx_mock
):
    tool_bindings = {"bbox": {"enabled": True, "classes": [{"name": "car"}]}}
    route = respx_mock.post(f"{API}/project-templates").mock(
        return_value=httpx.Response(201, json=TEMPLATE)
    )

    ProjectTemplates(client._http).create(
        ProjectTemplateCreate(
            source_project_id=SOURCE_ID,
            tool_bindings=tool_bindings,
        )
    )

    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "source_project_id": SOURCE_ID,
        "tool_bindings": tool_bindings,
    }


def test_create_requires_identity_or_source_and_rejects_explicit_null():
    with pytest.raises(ValidationError, match="required without source_project_id"):
        ProjectTemplateCreate()
    with pytest.raises(ValidationError, match="name cannot be null"):
        ProjectTemplateCreate(
            name=None, type_label="Type", type_key="type", source_project_id=SOURCE_ID
        )


def test_create_requires_organization_id_and_forbids_unknown_fields():
    with pytest.raises(ValidationError, match="requires organization_id"):
        ProjectTemplateCreate(
            name="Team", type_label="Type", type_key="type", scope="organization"
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProjectTemplateCreate(
            name="Template", type_label="Type", type_key="type", unexpected=True
        )


def test_update_keeps_omitted_fields_distinct_from_explicit_null(client, respx_mock):
    route = respx_mock.patch(f"{API}/project-templates/{TEMPLATE_ID}").mock(
        return_value=httpx.Response(200, json=TEMPLATE)
    )

    ProjectTemplates(client._http).update(
        TEMPLATE_ID, ProjectTemplateUpdate(name="Renamed", description=None)
    )

    assert json.loads(route.calls.last.request.content) == {
        "name": "Renamed",
        "description": None,
    }


def test_duplicate_posts_without_body_once(client, respx_mock):
    route = respx_mock.post(f"{API}/project-templates/{TEMPLATE_ID}/duplicate").mock(
        return_value=httpx.Response(201, json={**TEMPLATE, "scope": "private"})
    )

    duplicate = ProjectTemplates(client._http).duplicate(TEMPLATE_ID)

    assert route.call_count == 1
    assert route.calls.last.request.content == b""
    assert duplicate.scope == "private"


def test_delete_accepts_empty_204_response(client, respx_mock):
    route = respx_mock.delete(f"{API}/project-templates/{TEMPLATE_ID}").mock(
        return_value=httpx.Response(204)
    )

    assert ProjectTemplates(client._http).delete(TEMPLATE_ID) is None
    assert route.call_count == 1


def test_response_tolerates_new_fields():
    template = ProjectTemplate.model_validate(
        {**TEMPLATE, "future_field": {"ok": True}}
    )

    assert template.future_field == {"ok": True}

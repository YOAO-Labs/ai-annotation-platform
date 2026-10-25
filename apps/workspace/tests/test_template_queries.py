from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest

from aap_workspace.template_query_contract import (
    TEMPLATE_QUERY_PROTOCOL,
    TEMPLATE_ROW_COLUMNS,
    TemplateQueryUnavailable,
    template_query_request,
)
from aap_workspace.template_queries import TemplateQueryManager

_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_TARGET_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_CREATOR_ID = uuid.UUID("be846de2-ab3d-426e-b118-0f82d3affd06")
_ORG_ID = uuid.UUID("2aa1a157-2d42-4e45-8af9-65bcf5b509df")


def _row(*, template_id=_TARGET_ID, creator_id=_CREATOR_ID, **overrides):
    values = {
        "id": str(template_id),
        "display_id": "TPL-101",
        "name": "Detection",
        "description": None,
        "type_label": "Detection",
        "type_key": "image-det",
        "data_type": "image",
        "tool_bindings": {"bbox": {"classes": []}},
        "label_config": {},
        "ai_enabled": False,
        "sampling": "sequence",
        "maximum_annotations": 1,
        "show_overlap_first": False,
        "iou_dedup_threshold": "0.7",
        "box_threshold": "0.35",
        "text_threshold": "0.25",
        "rendering_config": {},
        "annotation_guide": None,
        "scope": "private",
        "organization_id": None,
        "created_by": str(creator_id),
        "source_project_id": None,
        "usage_count": 0,
        "created_at": "2026-10-21T12:00:00+00:00",
        "updated_at": "2026-10-21T12:00:00+00:00",
    }
    return {**values, **overrides}


def _request(operation="list_templates", *, role="annotator", filters=None):
    if operation == "list_templates" and filters is None:
        filters = {"scope": None, "type_key": None, "search": None}
    return template_query_request(
        operation,
        _ACTOR_ID,
        role,
        target_id=(None if operation == "list_templates" else _TARGET_ID),
        configuration_receipt=(
            {"operation_id": uuid.uuid4(), "operation": "create_template"}
            if operation == "get_template_projection"
            else None
        ),
        filters=filters,
        request_id=uuid.uuid4(),
    )


class FakeTransaction:
    def __init__(
        self,
        *,
        rows=None,
        row=None,
        projection_row="use_row",
        organization_ids=None,
        creator_names=None,
        wrong_echo_action=None,
    ):
        self.rows = list(rows or [])
        self.row = row
        self.projection_row = (
            self.row if projection_row == "use_row" else projection_row
        )
        self.organization_ids = list(organization_ids or [])
        self.creator_names = list(creator_names or [])
        self.creator_calls = []
        self.wrong_echo_action = wrong_echo_action
        self.calls = []

    async def step(self, request, action, parameters=None, filters=None):
        self.calls.append((action, parameters, filters))
        facts = {
            "organization_ids": {
                "organization_ids": [str(item) for item in self.organization_ids]
            },
            "list_rows": {
                "rows_json": json.dumps(
                    self.rows,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            },
            "get_row": {
                "row_json": (
                    None
                    if self.row is None
                    else json.dumps(self.row, ensure_ascii=True, allow_nan=False)
                )
            },
            "projection_row": {
                "row_json": (
                    None
                    if self.projection_row is None
                    else json.dumps(
                        self.projection_row, ensure_ascii=True, allow_nan=False
                    )
                )
            },
        }
        if action == "creator_name":
            creator_id = parameters["creator_id"]
            self.creator_calls.append(creator_id)
            index = len(self.creator_calls) - 1
            name = (
                self.creator_names[index] if index < len(self.creator_names) else None
            )
            action_facts = {"name": name}
        else:
            action_facts = facts[action]

        wire_request = request.model_dump(mode="json", by_alias=True)
        if action == self.wrong_echo_action:
            wire_request = {**wire_request, "request_id": str(uuid.uuid4())}
        return {
            "schema": TEMPLATE_QUERY_PROTOCOL,
            "request": wire_request,
            "action": action,
            "facts": action_facts,
        }


class FakeData:
    def __init__(self, transaction):
        self.tx = transaction
        self.error = None

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self.tx
        except BaseException as error:
            self.error = error
            raise


@pytest.mark.asyncio
async def test_super_admin_list_skips_org_read_and_looks_up_each_creator_in_row_order():
    rows = [
        _row(template_id=uuid.UUID(int=101), creator_id=_CREATOR_ID),
        _row(template_id=uuid.UUID(int=102), creator_id=_CREATOR_ID),
    ]
    transaction = FakeTransaction(
        rows=rows, creator_names=["first lookup", "second lookup"]
    )
    request = _request(
        role="super_admin",
        filters={"scope": "public", "type_key": None, "search": None},
    )

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is True
    assert [call[0] for call in transaction.calls] == [
        "list_rows",
        "creator_name",
        "creator_name",
    ]
    assert transaction.calls[0][1] == {"organization_ids": []}
    assert transaction.calls[0][2] == {
        "scope": "public",
        "type_key": None,
        "search": None,
    }
    data = json.loads(reply.data_json)
    assert [row["created_by_name"] for row in data] == [
        "first lookup",
        "second lookup",
    ]
    assert transaction.creator_calls == [str(_CREATOR_ID), str(_CREATOR_ID)]


@pytest.mark.asyncio
async def test_every_non_super_admin_list_reads_orgs_before_public_filter_even_for_unknown_role():
    transaction = FakeTransaction(
        rows=[_row()], organization_ids=[_ORG_ID], creator_names=["Creator"]
    )
    request = _request(
        role="historical_unknown_role",
        filters={"scope": "public", "type_key": ["image-det"], "search": ""},
    )

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is True
    assert [call[0] for call in transaction.calls] == [
        "organization_ids",
        "list_rows",
        "creator_name",
    ]
    assert transaction.calls[1][1] == {"organization_ids": [str(_ORG_ID)]}
    assert transaction.calls[1][2] == {
        "scope": "public",
        "type_key": ["image-det"],
        "search": "",
    }


@pytest.mark.asyncio
async def test_empty_list_does_no_creator_lookup_and_filter_nul_survives_to_list_step():
    transaction = FakeTransaction(rows=[], organization_ids=[])
    request = _request(
        role="reviewer",
        filters={"scope": None, "type_key": [], "search": "percent_%_雪\x00"},
    )

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is True
    assert reply.data_json == "[]"
    assert [call[0] for call in transaction.calls] == ["organization_ids", "list_rows"]
    assert transaction.calls[1][2]["search"] == "percent_%_雪\x00"
    assert transaction.creator_calls == []


@pytest.mark.asyncio
async def test_list_response_has_no_row_cap_and_creator_lookup_is_not_batched_or_cached():
    rows = [
        _row(template_id=uuid.UUID(int=1000 + index), creator_id=_CREATOR_ID)
        for index in range(501)
    ]
    names = [f"creator {index}" for index in range(501)]
    transaction = FakeTransaction(rows=rows, creator_names=names)
    request = _request(role="super_admin")

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert len(json.loads(reply.data_json)) == 501
    assert len(transaction.creator_calls) == 501
    assert [row["created_by_name"] for row in json.loads(reply.data_json)][
        -1
    ] == "creator 500"


@pytest.mark.asyncio
async def test_detail_missing_returns_404_before_org_or_creator_reads():
    transaction = FakeTransaction(row=None)
    data = FakeData(transaction)
    request = _request("get_template", role="annotator")

    reply = await TemplateQueryManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 404
    assert reply.error.detail == "模板不存在"
    assert data.error is None
    assert [call[0] for call in transaction.calls] == ["get_row"]


@pytest.mark.asyncio
async def test_hidden_organization_detail_checks_orgs_after_row_and_before_creator():
    row = _row(
        scope="organization",
        organization_id=str(_ORG_ID),
        created_by=str(uuid.uuid4()),
    )
    transaction = FakeTransaction(row=row, organization_ids=[])
    data = FakeData(transaction)
    request = _request("get_template", role="annotator")

    reply = await TemplateQueryManager(data).execute(request)

    assert reply.ok is False
    assert data.error is None
    assert [call[0] for call in transaction.calls] == ["get_row", "organization_ids"]
    assert transaction.creator_calls == []


@pytest.mark.asyncio
async def test_organization_null_detail_skips_org_lookup_and_uses_existing_policy_visibility():
    row = _row(scope="organization", organization_id=None)
    transaction = FakeTransaction(row=row, creator_names=["Creator"])
    request = _request("get_template", role="historical_unknown_role")

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is True
    assert [call[0] for call in transaction.calls] == ["get_row", "creator_name"]
    assert json.loads(reply.data_json)["created_by_name"] == "Creator"


@pytest.mark.asyncio
async def test_private_detail_checks_visibility_before_creator_name():
    row = _row(scope="private", created_by=str(uuid.uuid4()))
    transaction = FakeTransaction(row=row)
    request = _request("get_template", role="annotator")

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is False
    assert [call[0] for call in transaction.calls] == ["get_row"]


@pytest.mark.asyncio
async def test_postcommit_projection_uses_receipt_capability_without_visibility_reads():
    row = _row(scope="organization", organization_id=str(_ORG_ID))
    transaction = FakeTransaction(row=row, creator_names=[None])
    request = _request("get_template_projection", role="disabled_user")

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is True
    assert json.loads(reply.data_json)["created_by_name"] is None
    assert [call[0] for call in transaction.calls] == [
        "projection_row",
        "creator_name",
    ]


@pytest.mark.asyncio
async def test_missing_postcommit_projection_is_successful_json_null():
    transaction = FakeTransaction(projection_row=None)
    request = _request("get_template_projection", role="super_admin")

    reply = await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert reply.ok is True
    assert reply.data_json == "null"
    assert [call[0] for call in transaction.calls] == ["projection_row"]


@pytest.mark.asyncio
async def test_malformed_filters_are_decoded_after_org_lookup_and_before_list_rows():
    transaction = FakeTransaction()
    request = _request(role="annotator").model_copy(
        update={
            "filters_ascii": '{"scope":null,"scope":null,"type_key":null,"search":null}'
        }
    )

    with pytest.raises(TemplateQueryUnavailable):
        await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert [call[0] for call in transaction.calls] == ["organization_ids"]


@pytest.mark.asyncio
async def test_mismatched_native_echo_is_unavailable_before_creator_name():
    transaction = FakeTransaction(rows=[_row()], wrong_echo_action="list_rows")
    request = _request(role="super_admin")

    with pytest.raises(TemplateQueryUnavailable):
        await TemplateQueryManager(FakeData(transaction)).execute(request)

    assert [call[0] for call in transaction.calls] == ["list_rows"]
    assert transaction.creator_calls == []


def test_workspace_row_column_constant_is_the_fixed_owner_projection():
    assert len(TEMPLATE_ROW_COLUMNS) == 25

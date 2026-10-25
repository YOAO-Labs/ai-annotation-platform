from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
import json
from importlib.resources import files
from types import SimpleNamespace
import uuid

import pytest

from aap_workspace.project_queries import (
    ProjectQueryManager,
    ProjectQueryProfilesUnavailable,
)
from aap_workspace_contract.project_query_contract import (
    PROJECT_JSON_COLUMNS,
    PROJECT_QUERY_PROTOCOL,
    PROJECT_ROW_COLUMNS,
    ProjectQueryInvalidRequest,
    ProjectQueryProjectionMissing,
    ProjectQueryUnavailable,
    decode_project_query_request,
    decode_project_query_step,
    project_query_request,
    project_query_schema,
)
from aap_workspace_contract.project_query_values import (
    decode_project_query_reply,
    decode_project_row,
    materialize_project_query_data,
)

ACTOR = uuid.UUID(int=1)
PROJECT = uuid.UUID(int=2)
POOL = uuid.UUID(int=3)
DATASET = uuid.UUID(int=4)


def row(**changes):
    value = dict.fromkeys(PROJECT_ROW_COLUMNS)
    value.update({key: "{}" for key in PROJECT_JSON_COLUMNS})
    value.update(
        id=str(PROJECT),
        owner_id=str(ACTOR),
        display_id="P-LOCAL",
        name="Project",
        type_label="Image",
        type_key="image-det",
        data_type="image",
        status="in_progress",
        sampling="sequence",
        maximum_annotations=1,
        total_tasks=3,
        completed_tasks=1,
        review_tasks=1,
        in_progress_tasks=1,
        task_lock_ttl_seconds=300,
        iou_dedup_threshold="0.7000000000000001",
        box_threshold="0.35",
        text_threshold="0.25",
        guide_assets="[]",
        preannotate_pipeline=None,
        annotation_guide="Guide",
        tool_bindings='{"bbox":{"enabled":true,"classes":[{"name":"car"}]}}',
        ai_enabled=False,
        show_overlap_first=False,
        ai_interactive_enabled=True,
        raster_mask_native_editing_enabled=True,
        scene_mode=False,
        prefer_same_scene_continuation=False,
        scene_continuation_window_min=30,
        due_date="2026-01-02",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    return {**value, **changes}


def request(operation="list_projects", *, role="project_admin", filters=None, **kwargs):
    if operation in ("list_projects", "query_projects"):
        filters = {
            **dict.fromkeys(
                (
                    "status",
                    "search",
                    "type_key",
                    "data_type",
                    "member_id",
                    "project_role",
                    "created_from",
                    "created_to",
                )
            ),
            **(filters or {}),
        }
    if operation in (
        "get_project",
        "project_readiness",
        "get_project_projection",
        "get_project_access",
        "require_project_manager",
    ):
        kwargs.setdefault("target_id", PROJECT)
    return project_query_request(operation, ACTOR, role, filters=filters, **kwargs)


class Data:
    def __init__(self, facts, events=None):
        self.facts = facts
        self.events = events if events is not None else []
        self.filters = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        yield self

    async def step(self, request, action, parameters=None, filters=None):
        self.events.append((action, parameters))
        if filters is not None:
            self.filters.append(filters)
        facts = self.facts[action]
        if callable(facts):
            facts = facts(parameters)
        elif isinstance(facts, list):
            facts = facts.pop(0)
        return {
            "schema": PROJECT_QUERY_PROTOCOL,
            "request": request.model_dump(mode="json", by_alias=True),
            "action": action,
            "parameters": parameters,
            "facts": facts,
        }


class Profiles:
    def __init__(self, events, *, fail=False):
        self.events, self.fail = events, fail

    async def resolve(self, ids):
        self.events.append(("profiles", set(ids)))
        if self.fail:
            raise ProjectQueryProfilesUnavailable()
        return {
            str(ACTOR): SimpleNamespace(
                id=ACTOR,
                name="Owner",
                email="owner@example.test",
                role="project_admin",
                avatar_initial="O",
                avatar_ref=None,
            )
        }


def serialization_facts(value):
    return {
        "list_rows": {"rows_json": json.dumps([value])},
        "projection_row": {"row_json": json.dumps(value)},
        "get_row": {"row_json": json.dumps(value)},
        "ai_counts": {"counts": [{"project_id": str(PROJECT), "count": "1"}]},
        "member_count": {"count": "9007199254740993"},
        "pool": {"legacy_instance_id": None},
        "my_roles": {"memberships": []},
        "actor_status": {
            "actor": {
                "actor_id": str(ACTOR),
                "platform_role": "project_admin",
                "is_active": True,
            }
        },
    }


def test_request_preserves_opaque_nul_filters_and_closes_projection_context():
    value = request(filters={"member_id": ACTOR, "search": "a\0b"})
    assert "\\u0000" in value.filters_ascii
    with pytest.raises(ProjectQueryInvalidRequest):
        decode_project_query_request(
            {**value.model_dump(mode="json", by_alias=True), "is_manager": True}
        )
    with pytest.raises(ProjectQueryInvalidRequest):
        request(
            "get_project_projection",
            projection_evidence={
                "source": "class_rename",
                "operation_id": str(uuid.UUID(int=8)),
                "source_operation": "rename_class",
                "actor_id": str(uuid.UUID(int=9)),
                "project_id": str(PROJECT),
                "owner_id": str(ACTOR),
                "platform_role": "project_admin",
                "is_active": True,
            },
        )


def test_step_binds_repeated_arguments_and_keeps_bigint_as_decimal_text():
    value = request()
    payload = {
        "schema": PROJECT_QUERY_PROTOCOL,
        "request": value.model_dump(mode="json", by_alias=True),
        "action": "member_count",
        "parameters": {"project_id": str(PROJECT)},
        "facts": {"count": "9007199254740993"},
    }
    assert (
        decode_project_query_step(
            value, "member_count", payload["parameters"], payload
        ).facts.count
        == "9007199254740993"
    )
    for changed in (
        {"parameters": {"project_id": str(ACTOR)}},
        {"facts": {"count": 9007199254740993}},
        {"facts": {"count": "01"}},
        {"action": "task_count", "parameters": None},
    ):
        with pytest.raises(ProjectQueryUnavailable):
            decode_project_query_step(
                value, "member_count", payload["parameters"], {**payload, **changed}
            )


def test_published_step_schema_constrains_action_fact_pairs_and_operation_scope():
    schema = project_query_schema()
    assert (
        json.loads(
            files("aap_workspace_contract")
            .joinpath("project_query_protocol.schema.json")
            .read_text()
        )
        == schema
    )
    assert schema["$defs"]["ProjectQueryStep"]["additionalProperties"] is False
    rules = schema["$defs"]["ProjectQueryStep"]["allOf"]
    member = next(
        item
        for item in rules
        if item["if"]["properties"].get("action") == {"const": "member_count"}
    )
    assert member["then"]["properties"]["facts"] == {
        "$ref": "#/$defs/ProjectQueryCountFacts"
    }
    assert schema["x-json-text-columns"] == list(PROJECT_JSON_COLUMNS)


@pytest.mark.parametrize(
    "column,value,error",
    [
        ("due_date", "0001-01-01 BC", ValueError),
        ("due_date", "10000-01-01", ValueError),
        ("created_at", "10000-01-01T00:00:00+00:00", OverflowError),
        ("updated_at", "0001-01-01T00:00:00+00:00 BC", OverflowError),
    ],
)
@pytest.mark.asyncio
async def test_temporal_decode_failure_precedes_current_authority_and_profiles(
    column, value, error
):
    data = Data({"get_row": {"row_json": json.dumps(row(**{column: value}))}})
    with pytest.raises(error):
        await ProjectQueryManager(data, Profiles(data.events)).execute(
            request("get_project")
        )
    assert data.events == ["transaction", ("get_row", None)]


@pytest.mark.asyncio
async def test_infinity_and_json_text_preserve_legacy_scalars_and_late_business_validation():
    stored_json = '{"integer":9007199254740993,"overflow":1e1000}'
    value = row(
        due_date="infinity",
        updated_at="infinity",
        label_config="null",
        default_variants=stored_json,
    )
    data = Data(serialization_facts(value))
    reply = await ProjectQueryManager(data, Profiles(data.events)).execute(request())
    raw = json.loads(reply.data_json)[0]
    assert raw["default_variants"] == stored_json and raw["label_config"] == "null"
    public = materialize_project_query_data(reply)[0]
    assert public["due_date"] == date.max and public["updated_at"] == datetime.max
    assert public["updated_at"].tzinfo is None and public["created_at"].tzinfo == UTC
    assert public["label_config"] is None
    assert public["default_variants"]["integer"] == 9007199254740993
    assert public["default_variants"]["overflow"] == float("inf")
    assert public["iou_dedup_threshold"] == 0.7000000000000001
    assert public["member_count"] == 9007199254740993


def test_malformed_row_wire_is_unavailable_and_deep_stored_json_keeps_driver_failure():
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_row(json.dumps(row(label_config="not-json")))
    with pytest.raises(RecursionError):
        depth = 20_000
        decode_project_row(
            json.dumps(row(label_config="[" * depth + "0" + "]" * depth))
        )


@pytest.mark.asyncio
async def test_list_reads_batched_ai_then_one_profile_lookup_then_row_facts_and_my_roles():
    data = Data(serialization_facts(row(ml_backend_pool_id=str(POOL))))
    reply = await ProjectQueryManager(data, Profiles(data.events)).execute(request())
    assert (
        reply.ok and materialize_project_query_data(reply)[0]["owner_name"] == "Owner"
    )
    assert data.events == [
        "transaction",
        ("list_rows", {"created_from_us": None, "created_to_us": None}),
        ("ai_counts", {"project_ids": [str(PROJECT)]}),
        ("profiles", {ACTOR}),
        ("member_count", {"project_id": str(PROJECT)}),
        ("pool", {"pool_id": str(POOL)}),
        ("my_roles", {"project_ids": [str(PROJECT)]}),
    ]


@pytest.mark.asyncio
async def test_empty_results_and_stats_never_construct_profile_dependency():
    for operation, facts in (
        ("list_projects", {"list_rows": {"rows_json": "[]"}}),
        ("project_stats", {"visible_rows": {"rows_json": "[]"}}),
    ):
        data = Data(facts)
        reply = await ProjectQueryManager(data, object()).execute(request(operation))
        assert reply.ok and len(data.events) == 2


@pytest.mark.asyncio
async def test_filter_role_error_precedes_date_parsing_and_all_data_io():
    data = Data({})
    reply = await ProjectQueryManager(data, object()).execute(
        request(
            filters={
                "project_role": "bad",
                "created_from": "bad",
                "created_to": "bad",
                "search": "\0",
            }
        )
    )
    assert not reply.ok and reply.error.detail == "非法项目职责"
    assert data.events == []


@pytest.mark.asyncio
async def test_paged_count_precedes_rows_and_date_only_end_keeps_full_day():
    data = Data({"count_rows": {"count": "0"}, "list_rows": {"rows_json": "[]"}})
    value = request(
        "query_projects",
        filters={"created_from": "2026-01-02", "created_to": "2026-01-02"},
        pagination={"page": "9007199254740993", "page_size": 20},
    )
    reply = await ProjectQueryManager(data, object()).execute(value)
    assert data.events[0] == "transaction"
    assert [event[0] for event in data.events[1:]] == ["count_rows", "list_rows"]
    assert data.events[1][1] == data.events[2][1]
    assert data.filters[0]["created_to"] == datetime(
        2026, 1, 2, 23, 59, 59, 999999, tzinfo=UTC
    )
    assert materialize_project_query_data(reply)["page"] == 9007199254740993


@pytest.mark.asyncio
async def test_missing_first_row_precedes_inactive_account_and_missing_refresh_precedes_bad_role():
    data = Data({"get_row": {"row_json": None}})
    reply = await ProjectQueryManager(data, object()).execute(request("get_project"))
    assert reply.error.status_code == 404 and data.events == [
        "transaction",
        ("get_row", None),
    ]
    data = Data(
        {
            "get_row": [{"row_json": json.dumps(row())}, {"row_json": None}],
            "actor_status": {
                "actor": {
                    "actor_id": str(ACTOR),
                    "platform_role": "legacy",
                    "is_active": True,
                }
            },
        }
    )
    reply = await ProjectQueryManager(data, object()).execute(request("get_project"))
    assert reply.error.status_code == 404
    assert data.events[-2:] == [("actor_status", None), ("get_row", None)]


@pytest.mark.asyncio
async def test_detail_uses_current_membership_but_admitted_role_for_role_display_skip():
    facts = serialization_facts(row())
    facts.update(
        actor_status={
            "actor": {
                "actor_id": str(ACTOR),
                "platform_role": "employee",
                "is_active": True,
            }
        },
        member_access={
            "member": {
                "id": str(uuid.UUID(int=7)),
                "project_id": str(PROJECT),
                "user_id": str(ACTOR),
                "role": "viewer",
            }
        },
    )
    data = Data(facts)
    reply = await ProjectQueryManager(data, Profiles(data.events)).execute(
        request("get_project", role="super_admin")
    )
    assert reply.ok and materialize_project_query_data(reply)["my_project_role"] is None
    assert [event[0] for event in data.events[1:]] == [
        "get_row",
        "actor_status",
        "get_row",
        "member_access",
        "profiles",
        "member_count",
        "ai_counts",
    ]


@pytest.mark.asyncio
async def test_profile_failure_keeps_identity_error_and_stops_before_per_row_reads():
    data = Data(serialization_facts(row()))
    reply = await ProjectQueryManager(data, Profiles(data.events, fail=True)).execute(
        request()
    )
    assert not reply.ok and reply.error.status_code == 503
    assert reply.error.detail == "身份资料服务暂不可用"
    assert data.events[-1] == ("profiles", {ACTOR})


@pytest.mark.asyncio
async def test_stats_keeps_signed_persisted_totals_and_clock_after_separate_annotation_counts():
    data = Data(
        {
            "visible_rows": {"rows_json": json.dumps([row(total_tasks=-3)])},
            "annotation_count": [{"count": "1"}, {"count": "2"}],
            "task_series": {
                "total_data": ["9007199254740993"] * 12,
                "completed": ["1"] * 12,
                "pending_review": ["0"] * 12,
            },
            "annotation_series": {"total": ["2"] * 12, "ai_derived": ["1"] * 12},
        }
    )

    def clock():
        data.events.append("clock")
        return datetime(2026, 1, 15, tzinfo=UTC)

    reply = await ProjectQueryManager(data, object(), clock=clock).execute(
        request("project_stats")
    )
    public = materialize_project_query_data(reply)
    assert public["total_data"] == -3 and public["ai_rate"] == 200.0
    assert public["total_data_series"] == [9007199254740993] * 12
    assert public["ai_rate_series"] == [50.0] * 12
    assert data.events[2:5] == [
        ("annotation_count", {"project_ids": [str(PROJECT)], "ai_only": False}),
        ("annotation_count", {"project_ids": [str(PROJECT)], "ai_only": True}),
        "clock",
    ]


@pytest.mark.asyncio
async def test_readiness_reuses_captured_dataset_ids_and_checks_actual_assignees():
    annotator, reviewer, wrong_reviewer = (
        uuid.UUID(int=value) for value in (10, 11, 12)
    )
    batch1, batch2 = (uuid.UUID(int=value) for value in (20, 21))
    facts = serialization_facts(row())
    facts.update(
        {
            "dataset_ids": {"dataset_ids": [str(DATASET)]},
            "dataset_item_count": {"count": "5"},
            "linked_task_count": {"count": "2"},
            "task_count": {"count": "3"},
            "job_counts": {"statuses": [{"status": "running", "count": "1"}]},
            "latest_job_status": {"status": "failed"},
            "batches": {
                "batches": [
                    {
                        "id": str(batch),
                        "status": "active",
                        "annotator_id": str(annotator),
                        "reviewer_id": str(receiver),
                    }
                    for batch, receiver in (
                        (batch1, reviewer),
                        (batch2, wrong_reviewer),
                    )
                ]
            },
            "batch_task_counts": {
                "batch_task_counts": [
                    {"batch_id": str(batch), "count": "1"} for batch in (batch1, batch2)
                ]
            },
            "active_members": {
                "members": [
                    {"user_id": str(annotator), "project_role": "annotator"},
                    {"user_id": str(reviewer), "project_role": "reviewer"},
                ]
            },
        }
    )
    data = Data(facts)
    reply = await ProjectQueryManager(data, object()).execute(
        request("project_readiness")
    )
    public = materialize_project_query_data(reply)
    assert public["assigned_batch_count"] == 1 and public["executable_batch_count"] == 2
    assert public["guide_ready"] and public["classes_ready"]
    assert data.events[4:7] == [
        ("dataset_ids", None),
        ("dataset_item_count", {"dataset_ids": [str(DATASET)]}),
        ("linked_task_count", {"dataset_ids": [str(DATASET)]}),
    ]
    assert [item[0] for item in data.events[-5:]] == [
        "job_counts",
        "latest_job_status",
        "batches",
        "batch_task_counts",
        "active_members",
    ]


@pytest.mark.asyncio
async def test_accepted_class_projection_has_no_new_current_authority_read():
    evidence = {
        "source": "class_rename",
        "operation_id": uuid.UUID(int=8),
        "source_operation": "rename_class",
        "actor_id": ACTOR,
        "project_id": PROJECT,
        "owner_id": ACTOR,
        "platform_role": "project_admin",
        "is_active": True,
    }
    data = Data(serialization_facts(row()))
    reply = await ProjectQueryManager(data, Profiles(data.events)).execute(
        request("get_project_projection", projection_evidence=evidence)
    )
    assert reply.ok and [item[0] for item in data.events[1:]] == [
        "projection_row",
        "profiles",
        "member_count",
        "ai_counts",
    ]
    bad = request(
        "get_project_projection",
        projection_evidence={**evidence, "platform_role": "employee"},
    )
    data = Data({})
    with pytest.raises(ProjectQueryUnavailable):
        await ProjectQueryManager(data, object()).execute(bad)
    assert data.events == []


@pytest.mark.parametrize("field", ["project_id", "user_id"])
def test_member_authority_facts_reject_a_different_actual_project_or_user(field):
    value = request("get_project")
    member = {
        "id": str(uuid.UUID(int=7)),
        "project_id": str(PROJECT),
        "user_id": str(ACTOR),
        "role": "viewer",
    }
    member[field] = str(uuid.UUID(int=99))
    payload = {
        "schema": PROJECT_QUERY_PROTOCOL,
        "request": value.model_dump(mode="json", by_alias=True),
        "action": "member_access",
        "parameters": None,
        "facts": {"member": member},
    }
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_query_step(value, "member_access", None, payload)


@pytest.mark.asyncio
async def test_missing_configuration_projection_is_an_internal_failure_before_profiles():
    value = request(
        "get_project_projection",
        projection_evidence={
            "source": "configuration",
            "operation_id": uuid.UUID(int=8),
            "operation": "create_project",
        },
    )
    data = Data({"projection_row": {"row_json": None}})
    with pytest.raises(ProjectQueryProjectionMissing):
        await ProjectQueryManager(data, object()).execute(value)
    assert data.events == ["transaction", ("projection_row", None)]


@pytest.mark.asyncio
async def test_transfer_projection_reads_current_row_without_reauthorizing():
    evidence = {
        "source": "owner_transfer",
        "operation_id": uuid.UUID(int=8),
        "new_owner_id": ACTOR,
    }
    value = request("get_project_projection", projection_evidence=evidence)
    data = Data(serialization_facts(row(name="Changed after caller commit")))
    reply = await ProjectQueryManager(data, Profiles(data.events)).execute(value)
    assert (
        materialize_project_query_data(reply)["name"] == "Changed after caller commit"
    )
    assert "actor_status" not in [event[0] for event in data.events[1:]]
    data = Data({"projection_row": {"row_json": None}})
    with pytest.raises(ProjectQueryProjectionMissing):
        await ProjectQueryManager(data, object()).execute(value)
    assert data.events == ["transaction", ("projection_row", None)]


def test_search_reply_rejects_bad_scalar_types_and_results_beyond_its_limit():
    value = request("search_projects", filters={"q": "Project", "limit": 1})
    hit = {
        key: row()[key]
        for key in ("id", "display_id", "name", "type_key", "data_type", "type_label")
    }
    for data in ([{**hit, "name": 3}], [hit, hit]):
        with pytest.raises(ProjectQueryUnavailable):
            decode_project_query_reply(
                value,
                {
                    "schema": PROJECT_QUERY_PROTOCOL,
                    "ok": True,
                    "request": value.model_dump(mode="json", by_alias=True),
                    "data_json": json.dumps(data),
                },
            )


@pytest.mark.asyncio
async def test_parsed_date_bounds_bind_exact_microseconds_across_offset_seconds():
    data = Data({"list_rows": {"rows_json": "[]"}})
    value = request(
        filters={
            "created_from": "1970-01-01T01:02:03.000001+01:02:03",
            "created_to": "1970-01-01T00:00:00.000002Z",
        }
    )
    reply = await ProjectQueryManager(data, object()).execute(value)
    assert reply.ok
    assert data.events == [
        "transaction",
        ("list_rows", {"created_from_us": "1", "created_to_us": "2"}),
    ]

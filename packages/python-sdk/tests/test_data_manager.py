"""Data Manager wire contracts, explicit selections, and CLI trust boundaries."""

import json
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation import (
    DataManagerEntityQueryRequest,
    DataManagerMatchesRequest,
    DataManagerSummaryRequest,
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskExportRequest,
    ConflictError,
    NotFoundError,
    describe_operation,
)
from ai_annotation.cli.main import app

from .conftest import API, BASE

PROJECT, TASK, ANNOTATION, JOB, SECOND = [str(uuid4()) for _ in range(5)]
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}
FILTER = {
    "op": "and",
    "rules": [
        {"field": "task.batch_id", "op": "eq", "value": None},
        {
            "op": "or",
            "rules": [
                {
                    "field": "annotation.attribute.box.texture.color",
                    "op": "eq",
                    "value": "red",
                }
            ],
        },
    ],
}


def location(focus="annotation"):
    return {
        "project_id": PROJECT,
        "task_id": TASK,
        "task_display_id": "T-1",
        "data_type": "image",
        "focus_kind": focus,
        "annotation_id": ANNOTATION if focus == "annotation" else None,
    }


def object_row():
    return {
        "entity_key": ANNOTATION,
        "annotation_id": ANNOTATION,
        "task_id": TASK,
        "task_display_id": "T-1",
        "class_name": "car",
        "tool_unit_id": "box",
        "annotation_type": "bbox",
        "source": "manual",
        "location": location(),
        "future_field": {"kept": True},
    }


def track_row(ref):
    return {
        "entity_key": ref,
        "track_ref": ref,
        "track_kind": "scene",
        "track_id": "logical-track",
        "location": location("track"),
        "quality_issues": ["missing_frames"],
        "distinct_task_count": 2,
    }


def assignment_response():
    return {
        "task_ids": sorted([TASK, SECOND]),
        "preview_version": "a" * 64,
        "eligible_count": 1,
        "skipped_count": 1,
        "succeeded": [TASK],
        "items": [
            {
                "task_id": TASK,
                "will_change": True,
                "effective_after_annotator_id": None,
            },
            {"task_id": SECOND, "reason": "locked", "will_change": False},
        ],
    }


def test_schema_keeps_dynamic_fields_and_unknown_response_data(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PROJECT}/data-manager/schema").mock(
        return_value=httpx.Response(
            200,
            json={
                "project_kind": {
                    "data_type": "image",
                    "type_key": "image",
                    "scene_mode": False,
                },
                "filter_fields": [
                    {
                        "key": "annotation.attribute.box.texture.color",
                        "label": "Color",
                        "group": "attributes",
                        "value_type": "select",
                        "operators": ["eq", "in"],
                        "options": [{"value": "red", "label": "Red"}],
                    }
                ],
                "future_feature": True,
            },
        )
    )
    result = client.data_manager.schema(PROJECT, entity_scope="objects")
    assert route.calls.last.request.url.params["entity_scope"] == "objects"
    assert result.filter_fields[0].key.endswith("texture.color")
    assert result.filter_fields[0].operators == ["eq", "in"]
    assert result.future_feature is True


def test_summary_preserves_filter_structure_and_explicit_null(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT}/data-manager/summary").mock(
        return_value=httpx.Response(
            200,
            json={
                "scope": {"visible_task_total": 10, "matched_task_total": 3},
                "annotations": {"total": 7},
                "ai_review": {},
                "kind_metrics": {"optional": None},
            },
        )
    )
    result = client.data_manager.summary(
        PROJECT, DataManagerSummaryRequest(filter_json=FILTER, dimensions=["legacy"])
    )
    assert json.loads(route.calls.last.request.content) == {
        "filter_json": FILTER,
        "dimensions": ["legacy"],
    }
    assert result.scope.matched_task_total == 3 and result.annotations.total == 7
    assert result.kind_metrics["optional"] is None


def test_matches_uses_joint_project_task_path_and_one_window(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT}/tasks/{TASK}/data-manager/matches"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK,
                "items": [
                    {
                        "entity_kind": "prediction_shape",
                        "id": ANNOTATION,
                        "shape_index": 4,
                    }
                ],
                "total": 3,
                "limit": 1,
                "offset": 1,
            },
        )
    )
    result = client.data_manager.matches(
        PROJECT, TASK, DataManagerMatchesRequest(filter_json=FILTER, limit=1, offset=1)
    )
    assert json.loads(route.calls.last.request.content) == {
        "filter_json": FILTER,
        "limit": 1,
        "offset": 1,
    }
    assert result.task_id == UUID(TASK) and result.items[0].shape_index == 4
    assert result.total == 3 and result.offset == 1


def test_object_pages_details_and_direct_location_keep_envelopes(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT}/data-manager/objects/query"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [object_row()],
                "total": 7,
                "limit": 2,
                "next_cursor": "opaque-next",
                "facets": {"matched_total": 7, "task_total": 3},
            },
        )
    )
    body = DataManagerEntityQueryRequest(
        filter_json=FILTER,
        sort_json=[
            {"field": "annotation.updated_at", "direction": "desc"},
            {"field": "annotation.class_name"},
        ],
        columns_json=["class_name"],
        limit=2,
        cursor="opaque-current",
    )
    page = client.data_manager.query_objects(PROJECT, body)
    assert json.loads(route.calls.last.request.content) == body.model_dump(
        mode="json", exclude_unset=True
    )
    assert page.next_cursor == "opaque-next" and page.facets.matched_total == 7
    assert page.items[0].future_field == {"kept": True}
    respx_mock.get(
        f"{API}/projects/{PROJECT}/data-manager/objects/{ANNOTATION}/detail"
    ).mock(return_value=httpx.Response(200, json={"item": object_row()}))
    respx_mock.get(
        f"{API}/projects/{PROJECT}/data-manager/objects/{ANNOTATION}/location"
    ).mock(return_value=httpx.Response(200, json=location()))
    assert client.data_manager.object_detail(
        PROJECT, ANNOTATION
    ).item.annotation_id == UUID(ANNOTATION)
    assert client.data_manager.object_location(PROJECT, ANNOTATION).project_id == UUID(
        PROJECT
    )


def test_tracks_keep_ref_distinct_from_track_id_and_quote_path(client, respx_mock):
    ref = "scene:opaque/?#%"
    query = respx_mock.post(f"{API}/projects/{PROJECT}/data-manager/tracks/query").mock(
        return_value=httpx.Response(
            200, json={"items": [track_row(ref)], "total": 1, "limit": 50}
        )
    )
    detail = respx_mock.get(
        f"{API}/projects/{PROJECT}/data-manager/tracks/{quote(ref, safe='')}/detail"
    ).mock(
        return_value=httpx.Response(200, json={"track": track_row(ref), "members": []})
    )
    page = client.data_manager.query_tracks(PROJECT, DataManagerEntityQueryRequest())
    assert query.call_count == 1
    assert page.items[0].track_ref == ref and page.items[0].track_id == "logical-track"
    result = client.data_manager.track_detail(PROJECT, ref)
    assert result.track.track_ref == ref and result.members == []
    request = detail.calls.last.request
    assert (
        request.url.raw_path
        == f"/api/v1/projects/{PROJECT}/data-manager/tracks/{quote(ref, safe='')}/detail".encode()
    )
    assert not request.url.params


def test_assignment_preserves_canonical_selection_and_null_override(client, respx_mock):
    preview = respx_mock.post(
        f"{API}/projects/{PROJECT}/data-manager/tasks/assignment-preview"
    ).mock(return_value=httpx.Response(200, json=assignment_response()))
    apply = respx_mock.post(
        f"{API}/projects/{PROJECT}/data-manager/tasks/assignment-apply"
    ).mock(return_value=httpx.Response(200, json=assignment_response()))
    draft = DataManagerTaskAssignmentRequest(
        task_ids=[SECOND, TASK, TASK], annotator_id=None
    )
    planned = client.data_manager.assignment_preview(PROJECT, draft)
    expected = {"task_ids": sorted([TASK, SECOND]), "annotator_id": None}
    assert json.loads(preview.calls.last.request.content) == expected
    body = DataManagerTaskAssignmentApplyRequest(
        **expected, preview_version=planned.preview_version
    )
    result = client.data_manager.assignment_apply(PROJECT, body)
    assert json.loads(apply.calls.last.request.content) == {
        **expected,
        "preview_version": "a" * 64,
    }
    assert result.skipped_count == 1 and result.succeeded == [UUID(TASK)]
    assert result.items[1].reason == "locked"


def test_stale_assignment_never_refreshes_or_retries(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT}/data-manager/tasks/assignment-apply"
    ).mock(return_value=httpx.Response(409, json={"detail": "preview stale"}))
    with pytest.raises(ConflictError):
        client.data_manager.assignment_apply(
            PROJECT,
            DataManagerTaskAssignmentApplyRequest(
                task_ids=[TASK], reviewer_id=None, preview_version="b" * 64
            ),
        )
    assert route.call_count == 1 and len(respx_mock.calls) == 1


def test_selected_export_keeps_body_auth_key_and_replay_status(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT}/data-manager/tasks/export").mock(
        return_value=httpx.Response(202, json={"job_id": JOB, "status": "completed"})
    )
    body = DataManagerTaskExportRequest(
        task_ids=[TASK, TASK],
        targets=["aap_json"],
        include_attributes=False,
        lidar=None,
    )
    result = client.data_manager.export_tasks(
        PROJECT, body, idempotency_key="same-request"
    )
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer ak_test"
    assert request.headers["Idempotency-Key"] == "same-request"
    assert json.loads(request.content) == {
        "task_ids": [TASK],
        "targets": ["aap_json"],
        "include_attributes": False,
        "lidar": None,
    }
    assert result.job_id == UUID(JOB) and result.status == "completed"
    assert route.call_count == 1 and len(respx_mock.calls) == 1


def test_invisible_object_error_is_not_swallowed(client, respx_mock):
    respx_mock.get(
        f"{API}/projects/{PROJECT}/data-manager/objects/{ANNOTATION}/detail"
    ).mock(return_value=httpx.Response(404, json={"detail": "not available"}))
    with pytest.raises(NotFoundError):
        client.data_manager.object_detail(PROJECT, ANNOTATION)


@pytest.mark.parametrize(
    "factory,kwargs",
    [
        (DataManagerTaskAssignmentRequest, {"task_ids": [TASK]}),
        (DataManagerTaskExportRequest, {"task_ids": [], "targets": ["aap_json"]}),
        (
            DataManagerTaskExportRequest,
            {"task_ids": [TASK] * 201, "targets": ["aap_json"]},
        ),
        (DataManagerTaskExportRequest, {"task_ids": [TASK], "targets": []}),
        (
            DataManagerTaskAssignmentApplyRequest,
            {"task_ids": [TASK], "annotator_id": None, "preview_version": "short"},
        ),
        (DataManagerEntityQueryRequest, {"limit": 201}),
        (DataManagerMatchesRequest, {"offset": -1}),
    ],
)
def test_invalid_requests_never_send_http(factory, kwargs, respx_mock):
    with pytest.raises(ValidationError):
        factory(**kwargs)
    assert not respx_mock.calls


def test_task_export_rejects_video_scope(respx_mock):
    with pytest.raises(ValidationError, match="video scope"):
        DataManagerTaskExportRequest.model_validate(
            {
                "task_ids": [TASK],
                "targets": ["video_json"],
                "scope": {
                    "task_id": TASK,
                    "selection": {"kind": "frames", "from_frame": 0, "to_frame": 2},
                },
            }
        )
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "command,body",
    [
        (
            "assignment-apply",
            {"task_ids": [TASK], "annotator_id": None, "preview_version": "a" * 64},
        ),
        ("export-tasks", {"task_ids": [TASK], "targets": ["aap_json"]}),
    ],
)
def test_cli_mutation_refusal_has_zero_requests(command, body, respx_mock):
    result = CliRunner().invoke(
        app,
        [
            "--error-format=json",
            "data-manager",
            command,
            "--project",
            PROJECT,
            "--body",
            "-",
            "--json",
        ],
        input=json.dumps(body),
        env=ENV,
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["kind"] == "usage_error"
    assert not respx_mock.calls


def test_cli_preview_is_a_read_and_apply_preserves_stale_error(respx_mock):
    preview = respx_mock.post(
        f"{API}/projects/{PROJECT}/data-manager/tasks/assignment-preview"
    ).mock(return_value=httpx.Response(200, json=assignment_response()))
    args = [
        "--error-format=json",
        "data-manager",
        "assignment-preview",
        "--project",
        PROJECT,
        "--body",
        "-",
        "--json",
    ]
    draft = {"task_ids": [TASK], "reviewer_id": None}
    result = CliRunner().invoke(app, args, input=json.dumps(draft), env=ENV)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["preview_version"] == "a" * 64
    assert preview.call_count == 1
    apply = respx_mock.post(
        f"{API}/projects/{PROJECT}/data-manager/tasks/assignment-apply"
    ).mock(
        return_value=httpx.Response(409, json={"detail": {"code": "preview_changed"}})
    )
    args[2] = "assignment-apply"
    result = CliRunner().invoke(
        app,
        [*args, "--yes"],
        input=json.dumps({**draft, "preview_version": "a" * 64}),
        env=ENV,
    )
    assert result.exit_code == 1 and result.stdout == ""
    assert json.loads(result.stderr)["code"] == "preview_changed"
    assert apply.call_count == 1 and preview.call_count == 1


def test_cli_export_is_only_submission(respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT}/data-manager/tasks/export").mock(
        return_value=httpx.Response(202, json={"job_id": JOB, "status": "queued"})
    )
    result = CliRunner().invoke(
        app,
        [
            "data-manager",
            "export-tasks",
            "--project",
            PROJECT,
            "--body",
            "-",
            "--idempotency-key",
            "persisted-key",
            "--yes",
            "--json",
        ],
        input=json.dumps({"task_ids": [TASK], "targets": ["aap_json"]}),
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"job_id": JOB, "status": "queued"}
    assert route.calls.last.request.headers["Idempotency-Key"] == "persisted-key"
    assert len(respx_mock.calls) == 1


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e10000"])
def test_cli_nonfinite_filter_is_rejected_without_http(number, respx_mock):
    result = CliRunner().invoke(
        app,
        [
            "--error-format=json",
            "data-manager",
            "query-objects",
            "--project",
            PROJECT,
            "--body",
            "-",
            "--json",
        ],
        input='{"filter_json":{"field":"object.confidence","op":"gt","value":'
        + number
        + "}}",
        env=ENV,
    )
    assert result.exit_code == 1 and result.stdout == ""
    assert json.loads(result.stderr)["kind"] == "request_error"
    assert not respx_mock.calls


def test_discovery_states_real_grain_scope_and_action_limits():
    entity = describe_operation("data_manager.query_objects")
    assert any("read" in note for note in entity["domain_effects"])
    assert any("without pruning" in note for note in entity["constraints"])
    assert entity["cli"][0]["confirmation"] is False
    assignment = describe_operation("data_manager.assignment_apply")
    assert any("preview_version" in note for note in assignment["constraints"])
    assert assignment["cli"][0]["confirmation"] is True
    export = describe_operation("data_manager.export_tasks")
    assert any("never falls back" in note for note in export["constraints"])
    assert any("Idempotency-Key" in note for note in export["constraints"])
    view = describe_operation("task_views.update")
    assert any("null fields remain unchanged" in note for note in view["constraints"])


@pytest.mark.parametrize("status", [403, 422, 503])
def test_rejected_selected_export_is_not_retried_or_widened(client, respx_mock, status):
    from ai_annotation import APIStatusError

    route = respx_mock.post(f"{API}/projects/{PROJECT}/data-manager/tasks/export").mock(
        return_value=httpx.Response(
            status, json={"detail": {"code": "task_scoped_export_unsupported"}}
        )
    )
    body = DataManagerTaskExportRequest(task_ids=[TASK], targets=["voc"])
    with pytest.raises(APIStatusError) as raised:
        client.data_manager.export_tasks(PROJECT, body)
    assert raised.value.status_code == status
    assert route.call_count == 1 and len(respx_mock.calls) == 1
    assert json.loads(route.calls.last.request.content) == {
        "task_ids": [TASK],
        "targets": ["voc"],
    }

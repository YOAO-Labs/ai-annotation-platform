from uuid import uuid4

import httpx

from .conftest import API


def test_projects_list_filters_and_uuid_date_serialization(client, respx_mock):
    route = respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(200, json=[])
    )
    member_id = uuid4()
    client.projects.list(
        search="",
        type_key=["object_detection", "segment"],
        data_type=["image", "video"],
        member_id=member_id,
        project_role="reviewer",
        created_from="2026-01-01",
        created_to="2026-06-30",
    )
    params = route.calls.last.request.url.params
    assert params["search"] == ""
    assert params.get_list("type_key") == ["object_detection", "segment"]
    assert params.get_list("data_type") == ["image", "video"]
    assert params["member_id"] == str(member_id)
    assert params["project_role"] == "reviewer"
    assert params["created_from"] == "2026-01-01"
    assert params["created_to"] == "2026-06-30"


def test_dataset_has_scenes_omits_default_and_keeps_false(client, respx_mock):
    route = respx_mock.get(f"{API}/datasets").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "limit": 50, "offset": 0}
        )
    )
    client.datasets.list()
    assert "has_scenes" not in route.calls.last.request.url.params
    assert client.datasets.list(has_scenes=False).items == []
    assert route.calls.last.request.url.params["has_scenes"] == "false"


def test_task_filters_preserve_uuid_and_explicit_false(client, respx_mock):
    route = respx_mock.get(f"{API}/tasks").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "limit": 50, "offset": 0}
        )
    )
    client.tasks.list("project")
    assert "unbatched" not in route.calls.last.request.url.params

    assignee_id = uuid4()
    batch_id = uuid4()
    page = client.tasks.list(
        "project",
        assignee_id=assignee_id,
        batch_id=batch_id,
        unbatched=False,
        reject_reason_type="wrong_label",
        class_name="car",
    )
    params = route.calls.last.request.url.params
    assert page.items == []
    assert params["assignee_id"] == str(assignee_id)
    assert params["batch_id"] == str(batch_id)
    assert params["unbatched"] == "false"
    assert params["reject_reason_type"] == "wrong_label"
    assert params["class_name"] == "car"


def test_annotation_list_filters_video_segment(client, respx_mock):
    task_id = uuid4()
    route = respx_mock.get(f"{API}/tasks/{task_id}/annotations").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert client.annotations.list(task_id) == []
    assert "video_segment_id" not in route.calls.last.request.url.params

    segment_id = uuid4()
    assert client.annotations.list(task_id, video_segment_id=segment_id) == []
    assert route.calls.last.request.url.params["video_segment_id"] == str(segment_id)


def test_job_search_omits_none_and_preserves_empty_string(client, respx_mock):
    route = respx_mock.get(f"{API}/async-jobs").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    client.jobs.list()
    assert "search" not in route.calls.last.request.url.params
    assert client.jobs.list(search="").items == []
    assert route.calls.last.request.url.params["search"] == ""


def test_feedback_root_filter_counts_and_optional_count_response(client, respx_mock):
    route = respx_mock.get(f"{API}/feedbacks").mock(
        side_effect=[
            httpx.Response(200, json={"items": [], "next_cursor": None}),
            httpx.Response(200, json={"items": [], "next_cursor": None}),
            httpx.Response(
                200,
                json={
                    "items": [],
                    "next_cursor": None,
                    "total": 3,
                    "status_counts": {"open": 2, "resolved": 1},
                },
            ),
        ]
    )
    default_page = client.feedbacks.list("project")
    default_params = route.calls[-1].request.url.params
    assert default_page.total is None
    assert default_page.status_counts is None
    assert "root_only" not in default_params
    assert "include_counts" not in default_params

    client.feedbacks.list("project", root_only=False, include_counts=False)
    false_params = route.calls[-1].request.url.params
    assert false_params["root_only"] == "false"
    assert false_params["include_counts"] == "false"

    counted_page = client.feedbacks.list("project", root_only=True, include_counts=True)
    counted_params = route.calls[-1].request.url.params
    assert counted_page.total == 3
    assert counted_page.status_counts == {"open": 2, "resolved": 1}
    assert counted_params["root_only"] == "true"
    assert counted_params["include_counts"] == "true"

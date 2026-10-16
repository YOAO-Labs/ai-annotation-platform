import json
from uuid import uuid4

import httpx
import pytest
from ai_annotation.errors import APIStatusError, ConflictError, NotFoundError
from ai_annotation.learning_automation import ActiveLearning
from ai_annotation.learning_automation_models import (
    ActiveLearningDecisionCreate,
    ActiveLearningFeedbackCreate,
    ActiveLearningRoundCreate,
    ActiveLearningSelection,
)
from pydantic import ValidationError as PydanticValidationError

from .conftest import API

PROJECT_ID = uuid4()
ROUND_ID = uuid4()
ATTEMPT_ID = uuid4()
REQUEST_ID = uuid4()
DIGEST = "b" * 64
TASK_IDS = [uuid4() for _ in range(4)]


def selection():
    return ActiveLearningSelection(
        model_version_id=uuid4(),
        deployment_id=uuid4(),
        sample_task_ids=TASK_IDS,
        seed=17,
        batch_size=2,
        max_inference_dispatches=16,
        max_review_tasks=4,
    )


def round_create():
    return ActiveLearningRoundCreate(
        **selection().model_dump(mode="python", exclude_unset=True),
        request_id=REQUEST_ID,
        name="review batch",
        preview_digest=DIGEST,
    )


def round_out(*, status="queued"):
    return {
        "id": str(ROUND_ID),
        "project_id": str(PROJECT_ID),
        "name": "review batch",
        "status": status,
        "spec": {"batch_size": 2, "samples": []},
        "spec_digest": DIGEST,
        "inference_dispatches": 0,
        "accepted_tasks": 0,
        "current_attempt_id": str(ATTEMPT_ID),
        "async_job_id": str(uuid4()),
        "error_code": None,
        "error_message": None,
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
        "attempts": [
            {
                "id": str(ATTEMPT_ID),
                "ordinal": 1,
                "status": status,
                "heartbeat_at": None,
                "started_at": None,
                "completed_at": None,
                "error_code": None,
                "error_message": None,
                "has_report": False,
            }
        ],
    }


def resource(client):
    return ActiveLearning(client._http)


def test_selection_and_append_request_validation():
    values = selection().model_dump(mode="python", exclude_unset=True)
    with pytest.raises(PydanticValidationError):
        ActiveLearningSelection.model_validate({**values, "seed": True})
    with pytest.raises(PydanticValidationError):
        ActiveLearningSelection.model_validate({**values, "confidence": float("nan")})
    with pytest.raises(PydanticValidationError):
        ActiveLearningSelection.model_validate({**values, "max_review_tasks": 3})
    with pytest.raises(PydanticValidationError):
        ActiveLearningSelection.model_validate(
            {**values, "sample_task_ids": [TASK_IDS[0], TASK_IDS[0]]}
        )
    with pytest.raises(PydanticValidationError):
        ActiveLearningDecisionCreate(
            request_id=REQUEST_ID, action="reject", ordinals=[1, 1]
        )
    with pytest.raises(PydanticValidationError):
        ActiveLearningFeedbackCreate(
            request_id=REQUEST_ID,
            evaluations=[
                {"evaluation_id": TASK_IDS[0], "attempt_id": TASK_IDS[1]},
                {"evaluation_id": TASK_IDS[0], "attempt_id": TASK_IDS[1]},
            ],
        )


def test_pool_uses_bounded_page_shape(client, respx_mock):
    route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/pool"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "limit": 4,
                "offset": 2,
                "items": [
                    {
                        "task_id": str(TASK_IDS[0]),
                        "display_id": "T-1",
                        "file_name": "frame.png",
                        "dataset_item_id": None,
                        "scene_id": None,
                    }
                ],
            },
        )
    )

    result = resource(client).pool(PROJECT_ID, limit=4, offset=2)

    assert result.items[0].task_id == TASK_IDS[0]
    assert dict(route.calls.last.request.url.params) == {"limit": "4", "offset": "2"}


def test_preview_sends_only_selection_and_parses_handler_response(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds:preview"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "preview_digest": DIGEST,
                "pool_size": 10,
                "candidate_count": 4,
                "omitted": [],
                "samples": [{"ordinal": 0, "task_id": str(TASK_IDS[0])}],
                "model_ref": {"classes": ["cat"]},
                "profile": {"task": "detect"},
                "strategy": {"revision": 1},
                "exclusion_reference_count": 2,
                "batch_size": 2,
                "max_inference_dispatches": 16,
                "max_review_tasks": 4,
                "claim": "workflow_correctness_only",
            },
        )
    )

    result = resource(client).preview(PROJECT_ID, selection())

    sent = json.loads(route.calls.last.request.content)
    assert "request_id" not in sent
    assert "preview_digest" not in sent
    assert sent["sample_task_ids"] == [str(task_id) for task_id in TASK_IDS]
    assert result.candidate_count == 4


def test_create_preserves_request_id_and_stale_preview_conflict(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/active-learning-rounds").mock(
        return_value=httpx.Response(409, json={"detail": "selection changed"})
    )

    with pytest.raises(ConflictError):
        resource(client).create(PROJECT_ID, round_create())

    sent = json.loads(route.calls.last.request.content)
    assert sent["request_id"] == str(REQUEST_ID)
    assert sent["preview_digest"] == DIGEST
    assert sent["batch_size"] == 2


def test_create_parses_accepted_round(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/active-learning-rounds").mock(
        return_value=httpx.Response(202, json=round_out(status="queued"))
    )

    result = resource(client).create(PROJECT_ID, round_create())

    assert result.status == "queued"
    assert result.current_attempt_id == ATTEMPT_ID
    assert route.called


def test_list_and_get_keep_round_statuses(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/active-learning-rounds").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "items": [round_out(status="lost")],
            },
        )
    )
    result = resource(client).list(PROJECT_ID)
    assert result.items[0].status == "lost"
    assert route.called

    respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}"
    ).mock(return_value=httpx.Response(200, json=round_out(status="cancelling")))
    detail = resource(client).get(PROJECT_ID, ROUND_ID)
    assert detail.status == "cancelling"


def test_report_keeps_parent_round_attempt_path_and_404(client, respx_mock):
    route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/attempts/{ATTEMPT_ID}/report"
    ).mock(return_value=httpx.Response(404, json={"detail": "attempt missing"}))

    with pytest.raises(NotFoundError):
        resource(client).report(PROJECT_ID, ROUND_ID, ATTEMPT_ID)

    assert route.called


def test_report_parses_verified_handler_payload(client, respx_mock):
    route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/attempts/{ATTEMPT_ID}/report"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "schema": 1,
                "round_id": str(ROUND_ID),
                "attempt_id": str(ATTEMPT_ID),
                "spec_digest": DIGEST,
                "strategy": {"revision": 1},
                "ranking": [{"ordinal": 0, "recommended": True}],
                "missing_signal_count": 1,
                "inference_dispatches": 4,
                "max_review_tasks": 4,
                "claim": "workflow_correctness_only",
                "report_digest": DIGEST,
            },
        )
    )

    result = resource(client).report(PROJECT_ID, ROUND_ID, ATTEMPT_ID)

    assert result.attempt_id == ATTEMPT_ID
    assert result.schema_version == 1
    assert result.ranking[0]["recommended"] is True
    assert route.called


def test_candidate_list_has_items_only_envelope(client, respx_mock):
    respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/candidates"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "ordinal": 0,
                        "task_id": str(TASK_IDS[0]),
                        "status": "succeeded",
                        "disposition": None,
                        "decision_id": None,
                        "has_image": True,
                        "error_code": None,
                        "evidence": {"predictions": []},
                    }
                ]
            },
        )
    )

    result = resource(client).candidates(PROJECT_ID, ROUND_ID)

    assert result.items[0].ordinal == 0
    assert result.items[0].evidence == {"predictions": []}


def test_image_download_uses_authenticated_full_api_path(client, respx_mock, tmp_path):
    route = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/candidates/0/image"
    ).mock(return_value=httpx.Response(200, content=b"\x89PNG\r\n"))
    dest = tmp_path / "candidate.png"

    result = resource(client).download_image(PROJECT_ID, ROUND_ID, 0, dest)

    assert result == dest
    assert dest.read_bytes() == b"\x89PNG\r\n"
    assert route.calls.last.request.headers["Authorization"] == "Bearer ak_test"


def test_decisions_are_append_only_and_use_items_only_envelope(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/decisions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "request_id": str(REQUEST_ID),
                "action": "reject",
                "accepted_batch_id": None,
                "snapshot": {"ordinals": [1]},
                "snapshot_digest": DIGEST,
                "created_at": "2026-10-16T00:00:00Z",
            },
        )
    )
    result = resource(client).decide(
        PROJECT_ID,
        ROUND_ID,
        ActiveLearningDecisionCreate(
            request_id=REQUEST_ID, action="reject", ordinals=[1]
        ),
    )
    assert result.request_id == REQUEST_ID
    assert json.loads(route.calls.last.request.content) == {
        "request_id": str(REQUEST_ID),
        "action": "reject",
        "ordinals": [1],
    }

    respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/decisions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": str(uuid4()),
                        "request_id": str(REQUEST_ID),
                        "action": "reject",
                        "accepted_batch_id": None,
                        "snapshot": {"ordinals": [1]},
                        "snapshot_digest": DIGEST,
                        "created_at": "2026-10-16T00:00:00Z",
                    }
                ]
            },
        )
    )
    page = resource(client).decisions(PROJECT_ID, ROUND_ID)
    assert page.items[0].action == "reject"


def test_feedback_is_append_only_and_does_not_infer_causal_gain(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/feedback"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "request_id": str(REQUEST_ID),
                "snapshot": {
                    "evaluations": {
                        "comparable": False,
                        "reasons": ["different baseline"],
                    },
                    "causal_gain_established": False,
                },
                "snapshot_digest": DIGEST,
                "created_at": "2026-10-16T00:00:00Z",
            },
        )
    )
    result = resource(client).create_feedback(
        PROJECT_ID,
        ROUND_ID,
        ActiveLearningFeedbackCreate(request_id=REQUEST_ID),
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"request_id": str(REQUEST_ID)}
    assert result.snapshot["causal_gain_established"] is False

    respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/feedback"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"items": [result.model_dump(mode="json")]},
        )
    )
    page = resource(client).feedback(PROJECT_ID, ROUND_ID)
    assert page.items[0].snapshot_digest == DIGEST


@pytest.mark.parametrize(
    ("method", "status"), [("cancel", "cancelling"), ("retry", "queued")]
)
def test_cancel_and_explicit_retry_return_round_without_job_state_mapping(
    client, respx_mock, method, status
):
    path = f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/{method}"
    route = respx_mock.post(path).mock(
        return_value=httpx.Response(
            202 if method == "retry" else 200, json=round_out(status=status)
        )
    )

    result = getattr(resource(client), method)(PROJECT_ID, ROUND_ID)

    assert result.status == status
    assert route.call_count == 1


@pytest.mark.parametrize("method", ["decide", "create_feedback", "retry"])
def test_mutating_calls_are_not_automatically_retried(client, respx_mock, method):
    if method == "decide":
        path = (
            f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/decisions"
        )

        def call():
            return resource(client).decide(
                PROJECT_ID,
                ROUND_ID,
                ActiveLearningDecisionCreate(
                    request_id=REQUEST_ID, action="reject", ordinals=[0]
                ),
            )
    elif method == "create_feedback":
        path = f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/feedback"

        def call():
            return resource(client).create_feedback(
                PROJECT_ID,
                ROUND_ID,
                ActiveLearningFeedbackCreate(request_id=REQUEST_ID),
            )
    else:
        path = f"{API}/projects/{PROJECT_ID}/active-learning-rounds/{ROUND_ID}/retry"

        def call():
            return resource(client).retry(PROJECT_ID, ROUND_ID)

    route = respx_mock.post(path).mock(
        return_value=httpx.Response(503, json={"detail": "temporarily unavailable"})
    )

    with pytest.raises(APIStatusError):
        call()

    assert route.call_count == 1

import json
from uuid import uuid4

import httpx
import pytest
from ai_annotation.errors import AAPError, APIStatusError, ConflictError
from ai_annotation.learning_automation import TrainingStudies
from ai_annotation.learning_automation_models import (
    StudyBudgets,
    TrainingStudyCreate,
    TrainingStudyDetail,
    TrainingStudyPage,
    TrainingStudyPreview,
)
from ai_annotation.training_models import TrainingRunCreate
from pydantic import ValidationError as PydanticValidationError

from .conftest import API

PROJECT_ID = uuid4()
STUDY_ID = uuid4()
REQUEST_ID = uuid4()
DIGEST = "a" * 64


def body(*, preview_digest=None):
    values = dict(
        base=TrainingRunCreate(
            name="grid search",
            dataset_id=uuid4(),
            configuration={
                "schema_id": "schema-v1",
                "schema_digest": DIGEST,
                "values": {"epochs": 10},
            },
            request_id=REQUEST_ID,
        ),
        grid={"batch": [8, 16]},
        objective="metrics/mAP50-95",
        direction="maximize",
        budgets={
            "max_trials": 2,
            "max_planned_epochs": 20,
            "max_native_seconds": 3600.0,
        },
    )
    if preview_digest is not None:
        values["preview_digest"] = preview_digest
    return TrainingStudyCreate(**values)


def detail(*, status="active"):
    return {
        "id": str(STUDY_ID),
        "project_id": str(PROJECT_ID),
        "name": "grid search",
        "status": status,
        "scheduling": status == "active",
        "message": None if status == "active" else "stopping after current trial",
        "plan_digest": DIGEST,
        "plan": {"objective": "metrics/mAP50-95", "trials": []},
        "accounting": {"max_trials": 2, "unknown_attempt_ids": []},
        "trials": [
            {
                "id": str(uuid4()),
                "ordinal": 0,
                "values": {"batch": 8},
                "config_digest": DIGEST,
                "planned_epochs": 10,
                "run_id": None,
                "status": "pending",
                "result": None,
                "result_error": None,
                "actual_epochs": None,
                "completion": None,
                "queue_seconds": None,
                "lease_seconds": None,
                "native_seconds": 0.0,
                "native_time_unknown": False,
            }
        ],
        "rankings": [],
        "created_at": "2026-10-16T00:00:00Z",
    }


def resource(client):
    return TrainingStudies(client._http)


def test_study_budget_bounds_and_finite_numbers():
    with pytest.raises(PydanticValidationError):
        StudyBudgets(max_trials=True, max_planned_epochs=20, max_native_seconds=1.0)
    with pytest.raises(PydanticValidationError):
        StudyBudgets(
            max_trials=1, max_planned_epochs=20, max_native_seconds=float("nan")
        )
    with pytest.raises(PydanticValidationError):
        StudyBudgets(max_trials=21, max_planned_epochs=20, max_native_seconds=1.0)
    with pytest.raises(PydanticValidationError):
        StudyBudgets(max_trials=1, max_planned_epochs=20_001, max_native_seconds=1.0)


def test_study_requires_complete_configuration_and_bounded_grid():
    data = body().model_dump(mode="python", exclude_unset=True)
    data["base"].pop("configuration")
    with pytest.raises(PydanticValidationError):
        TrainingStudyCreate.model_validate(data)

    invalid = body().model_dump(mode="python", exclude_unset=True)
    invalid["grid"] = {"batch": []}
    with pytest.raises(PydanticValidationError):
        TrainingStudyCreate.model_validate(invalid)


def test_preview_round_trips_typed_body_and_response(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/training-studies:preview"
    ).mock(
        return_value=httpx.Response(
            200, json={"plan": {"trials": [{"ordinal": 0}]}, "preview_digest": DIGEST}
        )
    )

    result = resource(client).preview(PROJECT_ID, body())

    assert isinstance(result, TrainingStudyPreview)
    assert result.plan["trials"][0]["ordinal"] == 0
    sent = json.loads(route.calls.last.request.content)
    assert sent["base"]["request_id"] == str(REQUEST_ID)
    assert sent["grid"] == {"batch": [8, 16]}
    assert sent["budgets"]["max_trials"] == 2
    assert "preview_digest" not in sent


def test_create_requires_preview_without_sending_request(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/training-studies").mock(
        return_value=httpx.Response(202, json=detail())
    )

    with pytest.raises(AAPError, match="requires a preview_digest"):
        resource(client).create(PROJECT_ID, body())

    assert not route.called


def test_create_preserves_request_id_and_propagates_stale_preview_conflict(
    client, respx_mock
):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/training-studies").mock(
        return_value=httpx.Response(409, json={"detail": "search plan changed"})
    )

    with pytest.raises(ConflictError):
        resource(client).create(PROJECT_ID, body(preview_digest=DIGEST))

    sent = json.loads(route.calls.last.request.content)
    assert sent["base"]["request_id"] == str(REQUEST_ID)
    assert sent["preview_digest"] == DIGEST
    assert sent["base"].get("request_id") == str(REQUEST_ID)


def test_create_parses_accepted_study(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/training-studies").mock(
        return_value=httpx.Response(202, json=detail())
    )

    result = resource(client).create(PROJECT_ID, body(preview_digest=DIGEST))

    assert isinstance(result, TrainingStudyDetail)
    assert result.id == STUDY_ID
    assert route.called


def test_service_owns_grid_cardinality_budget(client, respx_mock):
    values = body().model_dump(mode="python", exclude_unset=True)
    values["budgets"]["max_trials"] = 1
    request = TrainingStudyCreate.model_validate(values)
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/training-studies:preview"
    ).mock(return_value=httpx.Response(422, json={"detail": "trial budget exceeded"}))

    with pytest.raises(APIStatusError) as error:
        resource(client).preview(PROJECT_ID, request)

    assert error.value.status_code == 422
    sent = json.loads(route.calls.last.request.content)
    assert len(sent["grid"]["batch"]) == 2
    assert sent["budgets"]["max_trials"] == 1


def test_list_uses_real_total_items_envelope(client, respx_mock):
    route = respx_mock.get(f"{API}/projects/{PROJECT_ID}/training-studies").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "items": [
                    {
                        "id": str(STUDY_ID),
                        "name": "grid search",
                        "status": "active",
                        "scheduling": True,
                        "message": None,
                        "created_at": "2026-10-16T00:00:00Z",
                        "trial_count": 2,
                    }
                ],
            },
        )
    )

    result = resource(client).list(PROJECT_ID, limit=4, offset=3)

    assert isinstance(result, TrainingStudyPage)
    assert result.total == 1
    assert result.items[0].trial_count == 2
    assert dict(route.calls.last.request.url.params) == {"limit": "4", "offset": "3"}


@pytest.mark.parametrize(
    ("method", "status"), [("get", "active"), ("cancel", "cancelling")]
)
def test_detail_and_cancel_keep_study_domain_status(client, respx_mock, method, status):
    route = getattr(respx_mock, "get" if method == "get" else "post")(
        f"{API}/projects/{PROJECT_ID}/training-studies/{STUDY_ID}"
        if method == "get"
        else f"{API}/projects/{PROJECT_ID}/training-studies/{STUDY_ID}/cancel"
    ).mock(return_value=httpx.Response(200, json=detail(status=status)))

    result = getattr(resource(client), method)(PROJECT_ID, STUDY_ID)

    assert isinstance(result, TrainingStudyDetail)
    assert result.status == status
    assert route.called

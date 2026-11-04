"""Phase 3 生产工作流 CLI 命令。"""

import json
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from yoao.cli.main import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "YOAO_BASE_URL": BASE,
    "YOAO_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}
PROJECT_ID, BATCH_ID, TASK_ID = (str(uuid4()) for _ in range(3))


def _batch(status: str = "active") -> dict:
    return {
        "id": BATCH_ID,
        "project_id": PROJECT_ID,
        "display_id": "B-1",
        "name": "batch",
        "status": status,
        "total_tasks": 2,
    }


def test_batch_transition_distribute_and_export(respx_mock):
    transition = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/transition"
    ).mock(return_value=httpx.Response(200, json=_batch("reviewing")))
    result = runner.invoke(
        app,
        [
            "batches",
            "transition",
            PROJECT_ID,
            BATCH_ID,
            "--status",
            "reviewing",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(transition.calls.last.request.content) == {
        "target_status": "reviewing"
    }

    annotator_id = str(uuid4())
    distribute = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/distribute-batches"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "distributed_batches": 1,
                "annotator_per_batch": {BATCH_ID: annotator_id},
                "reviewer_per_batch": {},
            },
        )
    )
    result = runner.invoke(
        app,
        [
            "batches",
            "distribute",
            PROJECT_ID,
            "--annotator-id",
            annotator_id,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(distribute.calls.last.request.content)["annotator_ids"] == [
        annotator_id
    ]

    job_id = str(uuid4())
    export = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/export"
    ).mock(return_value=httpx.Response(202, json={"job_id": job_id}))
    result = runner.invoke(
        app,
        [
            "batches",
            "export",
            PROJECT_ID,
            BATCH_ID,
            "--target",
            "coco",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["job_id"] == job_id
    assert export.calls.last.request.url.params["targets"] == "coco"


def test_bulk_batch_partial_failure_outputs_complete_json_then_exit_1(respx_mock):
    failed_id = str(uuid4())
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/batches/bulk-activate").mock(
        return_value=httpx.Response(
            200,
            json={
                "succeeded": [BATCH_ID],
                "skipped": [],
                "failed": [{"batch_id": failed_id, "reason": "wrong_status"}],
            },
        )
    )
    result = runner.invoke(
        app,
        [
            "batches",
            "bulk-activate",
            PROJECT_ID,
            "--id",
            BATCH_ID,
            "--id",
            failed_id,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["succeeded"] == [BATCH_ID]
    assert data["failed"][0]["batch_id"] == failed_id
    assert json.loads(route.calls.last.request.content)["batch_ids"] == [
        BATCH_ID,
        failed_id,
    ]


def test_task_submit_skip_and_review_commands(respx_mock):
    submit = respx_mock.post(f"{API}/tasks/{TASK_ID}/submit").mock(
        return_value=httpx.Response(
            200, json={"status": "submitted", "task_id": TASK_ID}
        )
    )
    result = runner.invoke(app, ["tasks", "submit", TASK_ID, "--json"], env=ENV)
    assert result.exit_code == 0 and submit.called

    skip = respx_mock.post(f"{API}/tasks/{TASK_ID}/skip").mock(
        return_value=httpx.Response(200, json={"status": "skipped", "task_id": TASK_ID})
    )
    result = runner.invoke(
        app,
        [
            "tasks",
            "skip",
            TASK_ID,
            "--reason",
            "unclear",
            "--note",
            "blurred",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(skip.calls.last.request.content) == {
        "reason": "unclear",
        "note": "blurred",
    }

    reject = respx_mock.post(f"{API}/tasks/{TASK_ID}/review/reject").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "rejected",
                "task_id": TASK_ID,
                "reason_type": "wrong_label",
                "reason": "fix class",
            },
        )
    )
    result = runner.invoke(
        app,
        [
            "tasks",
            "review-reject",
            TASK_ID,
            "--reason-type",
            "wrong_label",
            "--reason",
            "fix class",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(reject.calls.last.request.content)["reason_type"] == "wrong_label"


def test_annotation_bulk_update_command(respx_mock):
    annotation_ids = [str(uuid4()), str(uuid4())]
    route = respx_mock.post(f"{API}/annotations/bulk-update").mock(
        return_value=httpx.Response(
            200,
            json={"updated_ids": annotation_ids, "updated_count": 2},
        )
    )
    result = runner.invoke(
        app,
        [
            "annotations",
            "bulk-update",
            "--id",
            annotation_ids[0],
            "--id",
            annotation_ids[1],
            "--class-name",
            "car",
            "--attributes-json",
            '{"occluded": true}',
            "--locked",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    body = json.loads(route.calls.last.request.content)
    assert body["ids"] == annotation_ids
    assert body["patch"] == {
        "class_name": "car",
        "attributes": {"occluded": True},
        "is_locked": True,
    }


def test_jobs_retry_failed_command(respx_mock):
    job_id = str(uuid4())
    respx_mock.post(f"{API}/async-jobs/{job_id}/retry-failed").mock(
        return_value=httpx.Response(
            202,
            json={"status": "queued", "job_id": job_id, "queued": 2, "skipped": 1},
        )
    )
    guarded = runner.invoke(app, ["jobs", "retry-failed", job_id, "--json"], env=ENV)
    assert guarded.exit_code == 2
    assert "--yes" in guarded.output

    result = runner.invoke(
        app, ["jobs", "retry-failed", job_id, "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["queued"] == 2

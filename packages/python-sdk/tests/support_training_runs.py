from uuid import uuid4


def _run(project_id, dataset_id):
    return {
        "id": str(uuid4()),
        "project_id": project_id,
        "creator_id": None,
        "dataset_id": dataset_id,
        "async_job_id": None,
        "name": "run 1",
        "status": "succeeded",
        "task": "detect",
        "media_type": "image",
        "config": {"framework": "ultralytics"},
        "config_digest": "a" * 64,
        "current_attempt_id": None,
        "metrics": {"map50": 0.7},
        "error_message": None,
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
    }

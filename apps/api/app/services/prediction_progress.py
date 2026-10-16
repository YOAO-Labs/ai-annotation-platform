import json

import redis

from app.config import settings


def publish_progress(
    project_id: str,
    current: int,
    total: int,
    status: str = "running",
    error: str | None = None,
    job_meta: dict | None = None,
    pipeline_stages: list[dict] | None = None,
):
    """Publish preannotation progress to the project channel and terminal job events globally.

    ``job_meta`` is included only in the global channel payload. Callers control throttling
    and send it only at job start/end/failure to avoid flooding that channel.
    """
    payload = {
        "current": current,
        "total": total,
        "status": status,
        "error": error,
    }
    if pipeline_stages is not None:
        payload["pipeline_stages"] = pipeline_stages
    r = redis.from_url(settings.redis_url)
    try:
        r.publish(f"project:{project_id}:preannotate", json.dumps(payload))
        if job_meta is not None:
            global_payload = {**payload, "project_id": project_id, **job_meta}
            r.publish("global:prediction-jobs", json.dumps(global_payload))
    finally:
        r.close()

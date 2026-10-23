from sqlalchemy.engine import make_url

from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker


def test_diagnostics_redact_tracker_media_and_database_credentials(monkeypatch):
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)

    media_token = "fixture-media-control-token"
    routing_url = "postgresql+asyncpg://routing:routing-password@worker-postgres/test"
    writer_url = "postgresql+asyncpg://writer:writer-password@worker-postgres/test"
    worker = object.__new__(WorkspaceCeleryWorker)
    worker.backend_token = "fixture-backend-token"
    worker.secret_key = "fixture-worker-secret"
    worker.object_store_access_key = "fixture-s3-access-key"
    worker.object_store_secret_key = "fixture-s3-secret-key"
    worker._api_database_url = make_url(
        "postgresql+asyncpg://api:api-password@worker-postgres/test"
    )
    worker._result_database_url = make_url(
        "postgresql+asyncpg://results:results-password@worker-postgres/test"
    )
    worker._required = lambda _name: "redis://localhost:6379/0"
    worker._tracker_environment = {
        "ASSETS_MEDIA_TOKEN": media_token,
        "INFERENCE_ROUTING_DATABASE_URL": routing_url,
        "LABELING_VIDEO_WRITER_DATABASE_URL": writer_url,
    }

    log = (
        f"Bearer {media_token}; routing={routing_url}; "
        "routing password=routing-password; "
        f"writer={writer_url}; writer password=writer-password"
    )

    redacted = worker.redact(log)

    for secret in (
        media_token,
        routing_url,
        "routing-password",
        writer_url,
        "writer-password",
    ):
        assert secret not in redacted

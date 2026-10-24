"""Publication assembly gives API only its selected transport credential."""

from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from tests.identity_module_assembly_helpers import IdentityModuleStack


@pytest.mark.parametrize("selected", [False, True])
def test_publication_api_environment_keeps_file_token_and_scoped_redis(selected):
    source = {
        "REDIS_URL": "redis://cache:offline@owned-redis:6379/0",
        "MINIO_ENDPOINT": "owned-storage:9000",
        "AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY": "offline-access",
        "AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY": "offline-secret",
    }
    scoped = "redis://api:offline-api@owned-redis:6379/0"
    stack = SimpleNamespace(
        _api_database_url=make_url(
            "postgresql://api:offline@owned-postgres/probe_test"
        ),
        _api_secret="offline-api-secret",
        _control="offline-control",
        _required_environment=source.__getitem__,
        _docker_url=lambda value: value,
        _docker_endpoint=lambda value: value,
        _docker_http_url=lambda value: value,
        _notifications_socket_enabled=False,
        _notification_publication_enabled=selected,
        _notification_api_redis_url=scoped,
    )

    values = IdentityModuleStack._api_environment(stack)

    if selected:
        assert "NOTIFICATION_PUBLICATION_TOKEN" not in values
        assert values["NOTIFICATION_PUBLICATION_TOKEN_FILE"] == (
            "/run/secrets/notification_publication_token"
        )
        assert values["NOTIFICATION_PUBLICATION_URL"] == (
            "http://identity-notification-publisher:8181"
        )
        assert values["REDIS_URL"] == values["CELERY_BROKER_URL"] == scoped
    else:
        assert values["NOTIFICATION_PUBLICATION_URL"] == ""
        assert values["NOTIFICATION_PUBLICATION_TOKEN"] == ""
        assert values["NOTIFICATION_PUBLICATION_TOKEN_FILE"] == ""
        assert values["REDIS_URL"] == source["REDIS_URL"]

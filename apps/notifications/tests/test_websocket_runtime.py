from __future__ import annotations

import sys

from pydantic import SecretStr

from aap_notifications.websocket_config import NotificationSocketSettings
from aap_notifications.websocket_runtime import create_app


def test_standalone_app_factory_does_not_import_sibling_identity_client(monkeypatch):
    admission_before = sys.modules.get("aap_identity_admission.client")
    monkeypatch.delenv("NOTIFICATION_SOCKET_DATABASE_URL", raising=False)
    settings = NotificationSocketSettings(
        database_url=SecretStr("postgresql://socket:secret@127.0.0.1/socket"),
        redis_url=SecretStr("redis://socket:secret@127.0.0.1/0"),
        identity_url="http://127.0.0.1:8170",
        identity_token=SecretStr("identity-service-secret"),
    )

    app = create_app(settings)

    assert [(route.path, type(route).__name__) for route in app.routes] == [
        ("/health/live", "Route"),
        ("/health/ready", "Route"),
        ("/ws/notifications", "WebSocketRoute"),
    ]
    assert sys.modules.get("aap_identity_admission.client") is admission_before

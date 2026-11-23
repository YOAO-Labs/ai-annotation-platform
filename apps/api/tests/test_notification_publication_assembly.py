"""The isolated LAN overlay never mounts publisher credentials in producers."""

from pathlib import Path

import yaml


def test_isolated_publication_overlay_wires_producers_and_only_one_relay():
    root = Path(__file__).resolve().parents[3]
    assert not (root / "docker-compose.notifications.yml").exists()
    overlay = yaml.safe_load(
        (root / "docker-compose.notifications-publisher.yml").read_text()
    )
    services = overlay["services"]
    assert set(services) == {
        "api",
        "celery-beat",
        "celery-worker",
        "celery-worker-export",
        "notifications-publisher",
    }
    origin = "http://notifications-publisher:8181"
    api = services["api"]
    assert api["environment"]["NOTIFICATION_SUBSCRIBE_REDIS_URL_FILE"] == (
        "/run/secrets/notification_subscribe_redis_url"
    )
    assert services["celery-beat"]["environment"] == {
        "NOTIFICATION_PUBLICATION_URL": origin
    }
    publish_targets = {
        "/run/secrets/notification_publish_redis_url",
        "/run/secrets/notification_publish_database_url",
    }
    for name in ("api", "celery-worker", "celery-worker-export"):
        producer = services[name]
        assert producer["environment"]["NOTIFICATION_PUBLICATION_URL"] == origin
        assert producer["environment"]["NOTIFICATION_PUBLICATION_TOKEN_FILE"] == (
            "/run/secrets/notification_publication_token"
        )
        assert not producer["environment"]["NOTIFICATION_PUBLICATION_TOKEN"]
        assert all(
            value == ""
            for key, value in producer["environment"].items()
            if key.startswith("NOTIFICATION_PUBLISH_")
        )
        assert all(
            mount["source"] == "/dev/null"
            for mount in producer["volumes"]
            if mount["target"] in publish_targets
        )
    publisher = services["notifications-publisher"]
    assert set(publisher["secrets"]) == {
        "notification_publish_database_url",
        "notification_publish_redis_url",
        "notification_publication_token",
    }
    assert "ports" not in publisher

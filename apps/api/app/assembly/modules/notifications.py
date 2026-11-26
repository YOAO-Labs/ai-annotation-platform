"""Notifications built-in module manifest."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    MigrationSurface,
    RouterMount,
)


def _embedded_notification_relay(settings: Any) -> bool:
    return not settings.notification_publication_url


def _channel_sweep_interval(settings: Any) -> timedelta:
    return timedelta(seconds=settings.notification_channel_sweep_interval_seconds)


MANIFEST = BuiltinModuleManifest(
    module_id="notifications",
    owner_packages=("yoao-notifications",),
    routers=(
        RouterMount(module_path="app.api.v1.notifications", tags=("notifications",)),
        RouterMount(
            module_path="app.api.v1.notification_channels",
            tags=("notification-channels",),
        ),
        RouterMount(module_path="app.api.v1.ws", surface="root"),
    ),
    celery_includes=(
        "app.workers.notification_delivery",
        "app.workers.notification_channel_delivery",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="relay-notification-channel-deliveries",
            task="app.workers.notification_channel_delivery.relay_notification_channel_deliveries",
            schedule=_channel_sweep_interval,
        ),
        BeatScheduleSpec(
            name="relay-notification-deliveries",
            task="app.workers.notification_delivery.relay_identity_notifications",
            schedule=timedelta(seconds=10),
            when=_embedded_notification_relay,
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "notification",
            "notification_preference",
        ),
        native_managed_tables=(("public", "notification_delivery_outbox"),),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.notifications-publisher.yml",
            tier="optional_split",
            services=("notifications-publisher",),
            overrides=(
                "api",
                "celery-beat",
                "celery-worker",
                "celery-worker-export",
            ),
        ),
    ),
)

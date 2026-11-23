"""Bind Notifications immediate publication to Redis or the isolated publisher."""

from __future__ import annotations

from yoao_notifications.delivery_config import optional_notification_secret
from yoao_notifications.publication_client import RemotePublicationClient
from yoao_notifications.publication_contract import ImmediatePublicationUnavailable

from app.config import settings


def _service_token() -> str:
    try:
        token = optional_notification_secret(
            settings.notification_publication_token,
            settings.notification_publication_token_file,
        )
    except ValueError:
        raise ImmediatePublicationUnavailable() from None
    if token is None:
        raise ImmediatePublicationUnavailable()
    return token


async def publish_notification_request(request, *, embedded) -> bool:
    origin = settings.notification_publication_url
    if origin:
        return await RemotePublicationClient(
            origin=origin, service_token=_service_token()
        ).publish(request)
    await embedded(user_id=request.user_id, message=request.message)
    return True

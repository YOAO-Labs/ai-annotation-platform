"""Bind ordinary publication to the selected Notifications owner transport."""

from __future__ import annotations

from pathlib import Path

from aap_notifications.publication_client import RemotePublicationClient
from aap_notifications.publication_contract import ImmediatePublicationUnavailable

from app.config import settings


def _service_token() -> str:
    token = settings.notification_publication_token
    path = settings.notification_publication_token_file
    if bool(token) == bool(path):
        raise ImmediatePublicationUnavailable()
    if path:
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError
            token = raw.decode("utf-8").strip()
        except (OSError, ValueError, UnicodeError):
            raise ImmediatePublicationUnavailable() from None
    if not token.strip():
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

"""Same-origin transport for verified managed originals."""

from urllib.parse import urlsplit

import boto3
from botocore.config import Config

from app.config import settings
from app.services import storage_access
from app.services.content_snapshot import ContentError


def create_client(*, timeout_seconds=60):
    """Each physical operation owns one nonretrying, same-origin client."""
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    target = urlsplit(endpoint)

    def guard(request, **kwargs):
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) != (target.scheme, target.netloc):
            raise ContentError("managed_original_redirect_denied")

    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            config=Config(
                connect_timeout=2,
                read_timeout=max(10, min(timeout_seconds, 3600)),
                retries={"total_max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
                proxies={},
                signature_version="s3v4",
            ),
        )
        client.meta.events.register("before-send.s3", guard)
        return client
    except BaseException:
        raise storage_access.mark_unsettled(
            ContentError("managed_original_client_unknown", 503)
        ) from None

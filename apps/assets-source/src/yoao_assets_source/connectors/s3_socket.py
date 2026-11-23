"""Boto S3 with a registered, DNS-checked HTTP/TLS peer and no provider retries."""

from __future__ import annotations

import re
from time import monotonic
from urllib.parse import urlsplit

import boto3
from botocore.awsrequest import (
    AWSHTTPConnection,
    AWSHTTPConnectionPool,
    AWSHTTPSConnection,
    AWSHTTPSConnectionPool,
)
from botocore.config import Config
from botocore.utils import S3RegionRedirector, S3RegionRedirectorv2
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from .host_guard import ConnectorHostDenied, resolve_allowed_ips
from .registry import validate_secret
from .s3 import validate_s3_endpoint


def registered_endpoint(config: dict) -> str:
    endpoint = validate_s3_endpoint(config)
    parsed = urlsplit(endpoint)
    if (
        parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "\\" in endpoint
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", config.get("bucket", "")
        )
    ):
        raise ValueError("Invalid registered S3 endpoint or bucket")
    return endpoint.rstrip("/")


def _pin_socket(client, endpoint: str, addresses: list[str]) -> None:
    origin = urlsplit(endpoint)

    def guard(request, **_):
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) != (origin.scheme, origin.netloc):
            raise ConnectorHostDenied("Registered S3 request changed origin")

    def connection_type(base):
        class PinnedConnection(base):
            def _new_conn(self):
                host = self._dns_host
                timeout = self.timeout
                deadline = monotonic() + timeout
                failure = ConnectTimeoutError(
                    self, "Registered S3 connection timed out"
                )
                try:
                    for address in addresses:
                        self.timeout = deadline - monotonic()
                        if self.timeout <= 0:
                            break
                        self._dns_host = address
                        try:
                            return super()._new_conn()
                        except (ConnectTimeoutError, NewConnectionError) as exc:
                            failure = exc
                    raise failure
                finally:
                    self._dns_host = host
                    self.timeout = timeout

        return PinnedConnection

    class HttpPool(AWSHTTPConnectionPool):
        ConnectionCls = connection_type(AWSHTTPConnection)

    class HttpsPool(AWSHTTPSConnectionPool):
        ConnectionCls = connection_type(AWSHTTPSConnection)

    client._endpoint.http_session._manager.pool_classes_by_scheme = {
        "http": HttpPool,
        "https": HttpsPool,
    }
    client.meta.events.register_first("before-send.s3", guard)


def open_pinned_s3(config, secret, allowlist, *, connect_timeout, read_timeout):
    """Resolve once, validate all DNS answers, and connect only to those IPs."""
    validate_secret("s3", secret, config)
    endpoint = registered_endpoint(config)
    addresses = resolve_allowed_ips(endpoint, list(allowlist))
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=secret["access_key"],
        aws_secret_access_key=secret["secret_key"],
        region_name=config.get("region") or "us-east-1",
        config=Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    try:
        events = client.meta.events
        handlers = events._emitter._handlers.prefix_search("needs-retry.s3")
        for handler in handlers:
            if isinstance(
                getattr(handler, "__self__", None),
                (S3RegionRedirector, S3RegionRedirectorv2),
            ):
                events.unregister("needs-retry.s3", handler)
        _pin_socket(client, endpoint, addresses)
        return client
    except BaseException:
        client.close()
        raise

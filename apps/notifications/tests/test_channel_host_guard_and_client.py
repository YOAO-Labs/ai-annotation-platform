from __future__ import annotations

import uuid

import httpx
import pytest

from yoao_notifications.channel_contract import (
    ChannelDeliverRequest,
    ChannelVerifyAddressRequest,
    NotificationChannelEnvelope,
)
from yoao_notifications.channel_host_guard import (
    ChannelHostDenied,
    resolve_pinned_channel_ips,
    validate_channel_endpoint_url,
)
from yoao_notifications.channel_http_client import (
    ChannelSetupError,
    NotificationChannelHttpClient,
    classify_delivery_response,
)


def test_host_guard_blocks_loopback_link_local_ipv4_mapped_and_unlisted_private():
    with pytest.raises(ChannelHostDenied):
        validate_channel_endpoint_url("http://channel.example.com")
    with pytest.raises(ChannelHostDenied):
        validate_channel_endpoint_url("https://user:pass@channel.example.com")
    with pytest.raises(ChannelHostDenied):
        validate_channel_endpoint_url("https://channel.example.com?q=1")
    with pytest.raises(ChannelHostDenied):
        validate_channel_endpoint_url("https://channel.example.com#frag")

    assert (
        validate_channel_endpoint_url(
            "http://172.18.0.5:8080/",
            allow_insecure_http_hosts=("172.18.0.0/16",),
        )
        == "http://172.18.0.5:8080"
    )

    for forbidden_ip in (
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
    ):
        with pytest.raises(ChannelHostDenied):
            resolve_pinned_channel_ips(
                "https://rebind.example.com",
                host_allowlist=("rebind.example.com", "127.0.0.0/8"),
                resolver=lambda _h, ip=forbidden_ip: [ip],
            )

    with pytest.raises(ChannelHostDenied):
        resolve_pinned_channel_ips(
            "https://internal.example.com",
            resolver=lambda _h: ["10.20.30.40"],
        )

    host, ips = resolve_pinned_channel_ips(
        "https://internal.example.com",
        host_allowlist=("internal.example.com",),
        resolver=lambda _h: ["10.20.30.40"],
    )
    assert host == "internal.example.com"
    assert ips == ["10.20.30.40"]

    host_pub, ips_pub = resolve_pinned_channel_ips(
        "https://plugin.public.example",
        resolver=lambda _h: ["93.184.216.34"],
    )
    assert host_pub == "plugin.public.example"
    assert ips_pub == ["93.184.216.34"]


@pytest.mark.parametrize(
    "status,body,header,expected_outcome,expected_retry",
    [
        (
            200,
            b'{"status":"delivered","provider_message_id":"om_123"}',
            None,
            "delivered",
            None,
        ),
        (
            200,
            b'{"status":"permanent","reason":"invalid_open_id"}',
            None,
            "permanent",
            None,
        ),
        (
            422,
            b'{"status":"permanent","reason":"bad_address"}',
            None,
            "permanent",
            None,
        ),
        (404, b"not found", None, "permanent", None),
        (413, b"too large", None, "permanent", None),
        (
            200,
            b'{"status":"retryable","reason":"rate_limit","retry_after_seconds":1}',
            None,
            "retryable",
            5,
        ),
        (
            429,
            b'{"status":"retryable","reason":"busy","retry_after_seconds":99999}',
            None,
            "retryable",
            3600,
        ),
        (503, b"unavailable", "45", "retryable", 45),
        (302, b"", None, "retryable", None),
        (200, b"not-json", None, "retryable", None),
        (401, b'{"detail":"bad token"}', None, "auth_error", 60),
        (403, b'{"detail":"forbidden"}', None, "auth_error", 60),
    ],
)
def test_classify_delivery_response_matrix(
    status, body, header, expected_outcome, expected_retry
):
    result = classify_delivery_response(status, body, header)
    assert result.outcome == expected_outcome
    if expected_retry is not None:
        assert result.retry_after_seconds == expected_retry


@pytest.mark.asyncio
async def test_channel_http_client_setup_deliver_verify_and_oversize_guard():
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/setup":
            return httpx.Response(
                200,
                json={
                    "protocol_version": "yoao-notification-channel/v1",
                    "channel_id": "feishu_dm",
                    "display_name": "飞书私信",
                    "capabilities": ["deliver", "verify_address"],
                    "address_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["open_id"],
                        "properties": {"open_id": {"type": "string", "minLength": 4}},
                    },
                },
            )
        if request.url.path == "/deliver":
            return httpx.Response(
                200,
                json={"status": "delivered", "provider_message_id": "om_abc"},
            )
        if request.url.path == "/verify-address":
            return httpx.Response(200, json={"status": "delivered"})
        return httpx.Response(404)

    client = NotificationChannelHttpClient(
        resolver=lambda _h: ["93.184.216.34"],
        transport=httpx.MockTransport(handler),
    )
    assert await client.health("https://channel.example.com") is True
    setup = await client.setup("https://channel.example.com", "secret-token")
    assert setup.channel_id == "feishu_dm"

    delivery_id = uuid.uuid4()
    outcome = await client.deliver(
        "https://channel.example.com",
        "secret-token",
        ChannelDeliverRequest(
            channel_id="feishu_dm",
            envelope=NotificationChannelEnvelope(
                event="task.rejected",
                delivery_id=delivery_id,
                occurred_at="2026-11-24T07:00:00Z",
                data={"target_type": "task", "target_id": str(uuid.uuid4())},
            ),
            recipient_address={"open_id": "ou_12345"},
        ),
    )
    assert outcome.outcome == "delivered"
    assert outcome.provider_message_id == "om_abc"
    assert seen_headers[-1]["x-yoao-delivery-id"] == str(delivery_id)
    assert seen_headers[-1]["authorization"] == "Bearer secret-token"

    verified = await client.verify_address(
        "https://channel.example.com",
        "secret-token",
        ChannelVerifyAddressRequest(
            channel_id="feishu_dm",
            verification_id=uuid.uuid4(),
            recipient_address={"open_id": "ou_12345"},
        ),
    )
    assert verified.outcome == "delivered"

    oversize_client = NotificationChannelHttpClient(
        resolver=lambda _h: ["93.184.216.34"],
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, content=b"x" * 20000)
        ),
    )
    with pytest.raises(ChannelSetupError, match="16 KiB"):
        await oversize_client.setup("https://channel.example.com", "secret-token")

    oversize_deliver = await oversize_client.deliver(
        "https://channel.example.com",
        "secret-token",
        ChannelDeliverRequest(
            channel_id="feishu_dm",
            envelope=NotificationChannelEnvelope(
                event="task.rejected",
                delivery_id=uuid.uuid4(),
                occurred_at="2026-11-24T07:00:00Z",
                data={"target_type": "task", "target_id": str(uuid.uuid4())},
            ),
            recipient_address={"open_id": "ou_12345"},
        ),
    )
    assert oversize_deliver.outcome == "retryable"
    assert oversize_deliver.reason == "response_too_large"

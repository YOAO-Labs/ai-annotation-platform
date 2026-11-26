from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from yoao_notifications.channel_contract import (
    ChannelDeliverRequest,
    ChannelSetupResponse,
    ExtensionManifestV1,
    NotificationChannelEnvelope,
    project_external_envelope_data,
    validate_address_schema,
    validate_homepage_url,
    validate_recipient_address,
)
from yoao_notifications.channel_crypto import (
    NotificationChannelCryptoError,
    NotificationChannelKeyNotConfigured,
    channel_token_sha256,
    decrypt_channel_token,
    encrypt_channel_token,
)


def _feishu_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["open_id"],
        "properties": {
            "open_id": {
                "type": "string",
                "title": "飞书 Open ID",
                "minLength": 4,
                "maxLength": 64,
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
            },
            "mute_weekend": {"type": "boolean"},
        },
    }


def _manifest_payload() -> dict:
    return {
        "manifest_schema": "yoao-extension-manifest/v1",
        "extension_point": "notification_channel",
        "plugin_id": "yoao.feishu-dm",
        "version": "1.0.0",
        "display_name": "飞书私信",
        "description": "向飞书用户发送脱敏通知卡片",
        "homepage": "https://github.com/YOAO-Labs/feishu-channel",
        "publisher": "YOAO Labs",
        "license": "Apache-2.0",
        "protocol_version": "yoao-notification-channel/v1",
        "entrypoint": ["feishu-channel", "serve", "--port", "8080"],
        "capabilities": ["deliver", "verify_address"],
        "resource": {"memory_mb": 256},
        "extension": {
            "channel_id": "feishu_dm",
            "address_schema": _feishu_schema(),
            "allowed_egress_hosts": ["open.feishu.cn"],
        },
    }


def test_extension_manifest_v1_and_marketplace_display_fields():
    manifest = ExtensionManifestV1.model_validate(_manifest_payload())
    assert manifest.display_name == "飞书私信"
    assert manifest.description == "向飞书用户发送脱敏通知卡片"
    assert manifest.homepage == "https://github.com/YOAO-Labs/feishu-channel"
    bound = manifest.bind_artifact("registry.example/feishu@sha256:" + "a" * 64)
    assert bound.artifact.endswith("@sha256:" + "a" * 64)

    minimal = _manifest_payload()
    for optional_key in ("display_name", "description", "homepage"):
        minimal.pop(optional_key)
    parsed_min = ExtensionManifestV1.model_validate(minimal)
    assert parsed_min.display_name is None
    assert parsed_min.description is None
    assert parsed_min.homepage is None

    for forbidden_extra in (
        {"source_tier": "official"},
        {"artifact": "registry.example/feishu@sha256:" + "a" * 64},
    ):
        with pytest.raises(ValidationError):
            ExtensionManifestV1.model_validate(
                {**_manifest_payload(), **forbidden_extra}
            )

    for bad_homepage in (
        "http://example.com",
        "javascript:alert(1)",
        "data:text/html,hi",
        "https://user:pass@example.com",
        "https://127.0.0.1/docs",
        "https://[::ffff:127.0.0.1]/docs",
        "https://10.0.0.5/internal",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/app",
    ):
        with pytest.raises(ValueError):
            validate_homepage_url(bad_homepage)

    for bad_entry in (["sh", "-c", "id"], ["python3", "-Ic", "print(1)"]):
        with pytest.raises(ValidationError):
            ExtensionManifestV1.model_validate(
                {**_manifest_payload(), "entrypoint": bad_entry}
            )


def test_setup_response_rejects_source_tier_and_reserved_channel_ids():
    setup = ChannelSetupResponse.model_validate(
        {
            "protocol_version": "yoao-notification-channel/v1",
            "channel_id": "feishu_dm",
            "display_name": "飞书私信",
            "description": "个人消息提醒",
            "homepage": "https://open.feishu.cn/document",
            "capabilities": ["deliver", "verify_address"],
            "address_schema": _feishu_schema(),
        }
    )
    assert setup.channel_id == "feishu_dm"

    with pytest.raises(ValidationError):
        ChannelSetupResponse.model_validate(
            {
                "protocol_version": "yoao-notification-channel/v1",
                "channel_id": "feishu_dm",
                "display_name": "飞书私信",
                "source_tier": "official",
                "capabilities": ["deliver"],
                "address_schema": _feishu_schema(),
            }
        )

    for reserved in ("in_app", "toast", "email", "external"):
        with pytest.raises(ValidationError):
            ChannelSetupResponse.model_validate(
                {
                    "protocol_version": "yoao-notification-channel/v1",
                    "channel_id": reserved,
                    "display_name": "Bad",
                    "capabilities": ["deliver"],
                    "address_schema": _feishu_schema(),
                }
            )


@pytest.mark.parametrize(
    "bad_schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {
            "type": "object",
            "additionalProperties": True,
            "properties": {"a": {"type": "string"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"open_id": {"type": "string", "pattern": "^(a+)+$"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "$defs": {"x": {"type": "string"}},
            "properties": {"open_id": {"$ref": "#/$defs/x"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "anyOf": [{"required": ["open_id"]}],
            "properties": {"open_id": {"type": "string"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"nested": {"type": "object"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {f"k{i}": {"type": "string"} for i in range(9)},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "open_id": {"type": "string", "enum": [f"v{i}" for i in range(33)]}
            },
        },
    ],
)
def test_address_schema_blocks_redos_and_unbounded_constructs(bad_schema):
    with pytest.raises(ValueError):
        validate_address_schema(bad_schema)


def test_recipient_address_validation_enforces_types_bounds_and_2kib_cap():
    schema = validate_address_schema(_feishu_schema())
    valid = validate_recipient_address(
        {"open_id": "ou_12345678", "priority": 2, "mute_weekend": False}, schema
    )
    assert valid["open_id"] == "ou_12345678"

    with pytest.raises(ValueError):
        validate_recipient_address({"open_id": "ou_12345678", "priority": True}, schema)
    with pytest.raises(ValueError):
        validate_recipient_address({"open_id": "ou_12345678", "extra": "x"}, schema)
    with pytest.raises(ValueError):
        validate_recipient_address({"open_id": "abc"}, schema)

    wide_schema = validate_address_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["blob"],
            "properties": {"blob": {"type": "string", "maxLength": 4096}},
        }
    )
    with pytest.raises(ValueError, match="2048"):
        validate_recipient_address({"blob": "x" * 2040}, wide_schema)


def test_external_payload_projector_scrubs_all_free_text_and_secrets():
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    notif_id = uuid.uuid4()

    rejected = project_external_envelope_data(
        notification_type="task.rejected",
        notification_id=notif_id,
        target_type="task",
        target_id=task_id,
        project_id=project_id,
        task_id=task_id,
        payload={
            "task_display_id": 42,
            "reject_reason_type": "missing_label",
            "reject_reason": "SECRET FREE TEXT COMMENT MUST NOT LEAK",
            "actor_name": "Alice",
            "email": "alice@example.com",
        },
    )
    assert rejected["task_display_id"] == 42
    assert rejected["reject_reason_type"] == "missing_label"
    assert rejected["action_url"] == f"/projects/{project_id}/workbench/{task_id}"
    assert "reject_reason" not in rejected
    assert "email" not in rejected

    batch_rej = project_external_envelope_data(
        notification_type="batch.rejected",
        notification_id=None,
        target_type="batch",
        target_id=uuid.uuid4(),
        project_id=project_id,
        payload={
            "batch_display_id": 7,
            "batch_name": "Round 1",
            "feedback": "PRIVATE BATCH FEEDBACK",
            "reason": "PRIVATE LOCK REASON",
            "affected_tasks": 12,
        },
    )
    assert "feedback" not in batch_rej
    assert "reason" not in batch_rej
    assert batch_rej["affected_tasks"] == 12

    bug = project_external_envelope_data(
        notification_type="bug_report.commented",
        notification_id=notif_id,
        target_type="bug_report",
        target_id=uuid.uuid4(),
        payload={
            "display_id": 9,
            "title": "Sensitive title text",
            "snippet": "SECRET COMMENT SNIPPET",
            "resolution": "SECRET RESOLUTION",
            "actor_name": "Bob",
        },
    )
    assert "snippet" not in bug
    assert "resolution" not in bug
    assert "title" not in bug
    assert bug["display_id"] == 9

    exp = project_external_envelope_data(
        notification_type="export.failed",
        notification_id=notif_id,
        target_type="export",
        target_id=uuid.uuid4(),
        project_id=project_id,
        payload={
            "project_display_id": 3,
            "targets": ["coco"],
            "format": "zip",
            "download_url": "https://minio.internal/signed?X-Amz-Signature=secret",
            "error": "Traceback SQL leak",
        },
    )
    assert "download_url" not in exp
    assert "error" not in exp
    assert exp["targets"] == ["coco"]

    deliver_req = ChannelDeliverRequest(
        channel_id="feishu_dm",
        envelope=NotificationChannelEnvelope(
            event="task.rejected",
            delivery_id=uuid.uuid4(),
            occurred_at="2026-11-24T07:00:00Z",
            data=rejected,
        ),
        recipient_address={"open_id": "ou_12345678"},
    )
    assert deliver_req.envelope.event_version == "1.0"


def test_channel_crypto_is_fail_closed_and_verifies_digest():
    with pytest.raises(NotificationChannelKeyNotConfigured):
        encrypt_channel_token("tok_secret_123", "")
    with pytest.raises(NotificationChannelKeyNotConfigured):
        encrypt_channel_token("tok_secret_123", "not-a-fernet-key")

    key = Fernet.generate_key().decode("ascii")
    other_key = Fernet.generate_key().decode("ascii")
    ct = encrypt_channel_token("tok_secret_123", key)
    assert "tok_secret_123" not in ct
    assert decrypt_channel_token(ct, key) == "tok_secret_123"
    assert len(channel_token_sha256("tok_secret_123")) == 64

    with pytest.raises(NotificationChannelCryptoError):
        decrypt_channel_token(ct, other_key)
    with pytest.raises(NotificationChannelCryptoError):
        decrypt_channel_token("tampered", key)
